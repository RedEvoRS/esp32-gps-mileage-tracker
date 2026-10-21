"""Pair uploaded history by device UTC, never by arrival order."""
import contextlib
import copy
import io
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from parked_drain import main, pair_events, read_events
from test_diagnostics_catalog import event
from test_ingest_diagnostics import load_ingest, HEADERS, batch


def history(device="tracker-1", offset=0):
    a, b = f"{offset + 1:032x}", f"{offset + 2:032x}"
    def row(code, boot, counter, seq, utc, context):
        return device, event(code, sequence=seq, transition="notice", episode_id=None,
                             catalog_version=6, boot_id=boot, boot_counter=counter,
                             utc_ms=utc, time_source="rtc", context=context)
    return [row(1000, a, 10, 1, 1000, {"reset_reason": "power_on"}),
            row(2009, a, 10, 2, 3600000, {"battery_mv": 4000, "battery_pct": 80,
                                         "reason": "idle_battery"}),
            row(1000, b, 11, 1, 39600000, {"battery_mv": 3900, "battery_pct": 75,
                                         "reset_reason": "power_on"})]


class ParkedDrainTests(unittest.TestCase):
    def test_delayed_uploads_multiple_devices(self):
        rows = history() + history("tracker-2", 10)
        pairs = pair_events(list(reversed(rows)))
        self.assertEqual([p["device_id"] for p in pairs], ["tracker-1", "tracker-2"])
        for pair in pairs:
            self.assertEqual(pair["hours_parked"], 10)
            self.assertEqual(pair["mv_drop"], 100)
            self.assertEqual(pair["mv_per_hour"], 10)
            self.assertEqual(pair["percentage_points_drop"], 5)

    def test_unknown_measurements_are_independent_and_zero_percent_is_valid(self):
        rows = history()
        del rows[1][1]["context"]["battery_mv"]
        rows[2][1]["context"]["battery_pct"] = 0
        pair = pair_events(rows)[0]
        self.assertIsNone(pair["mv_drop"])
        self.assertIsNone(pair["mv_per_hour"])
        self.assertEqual(pair["percentage_points_drop"], 80)
        del rows[2][1]["context"]["battery_pct"]
        self.assertIsNone(pair_events(rows)[0]["percentage_points_drop"])

    def test_voltage_rebound_is_kept_negative(self):
        rows = history(); rows[2][1]["context"]["battery_mv"] = 4050
        self.assertEqual(pair_events(rows)[0]["mv_per_hour"], -5)

    def test_ambiguous_missing_or_reversed_history_is_omitted(self):
        for change in (
            lambda r: r.pop(0),
            lambda r: r.pop(),
            lambda r: r[1][1].update(utc_ms=None, time_source="unknown"),
            lambda r: r[2][1].update(utc_ms=None, time_source="unknown"),
            lambda r: r[2][1].update(utc_ms=3600000),
            lambda r: r[2][1].update(utc_ms=500),
            lambda r: r[2][1].update(boot_counter=12),
            lambda r: r[0][1].update(boot_counter=0),
            lambda r: r[2][1]["context"].update(reset_reason="software"),
            lambda r: r.append((r[0][0], dict(r[0][1], boot_id="f" * 32))),
            lambda r: r.append((r[0][0], dict(r[0][1], code=1003, sequence=3))),
        ):
            rows = history(); change(rows)
            self.assertEqual(pair_events(rows), [])

    def test_latest_shutdown_attempt_in_same_boot_only(self):
        rows = history(); later = copy.deepcopy(rows[1])
        later[1].update(sequence=3, utc_ms=7200000)
        rows.append(later)
        self.assertEqual(len(pair_events(rows)), 1)
        self.assertEqual(pair_events(rows)[0]["hours_parked"], 9)

    def test_real_ingest_read_only_and_csv(self):
        with tempfile.TemporaryDirectory() as temp:
            ing = load_ingest(temp)
            # Reverse arrival: the future boot is received before old segments.
            for i, (device, row) in enumerate(reversed(history())):
                body = batch(i + 1); body["events"] = [row]
                response = ing.app.test_client().post("/v1/device-events", json=body, headers=HEADERS)
                self.assertEqual(response.status_code, 200)
            path = ing.DIAGNOSTICS_STORE.path
            before = path.read_bytes()
            self.assertEqual(len(pair_events(read_events(path))), 1)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main([str(path)]), 0)
            self.assertIn("10.000,100.000,10.000,5.000", output.getvalue())
            self.assertEqual(path.read_bytes(), before)

    def test_missing_database_is_not_created_and_bad_schema_is_not_migrated(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "events.db"
            with self.assertRaises(sqlite3.Error):
                read_events(path)
            self.assertFalse(path.exists())
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE schema_version(singleton, version)")
            db.execute("INSERT INTO schema_version VALUES(1, 2)"); db.commit(); db.close()
            before = path.read_bytes()
            with self.assertRaises(ValueError):
                read_events(path)
            self.assertEqual(path.read_bytes(), before)

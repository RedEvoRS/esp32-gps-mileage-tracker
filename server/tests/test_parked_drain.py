"""Pair uploaded history by device UTC, never by arrival order."""
import contextlib
import copy
import csv
import io
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from parked_drain import correct_pairs, main, pair_events, read_events
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


def parked_history(parks, device="tracker-1"):
    """Build consecutive boots from (seconds parked, off mV, boot mV) samples."""
    start, off_template, boot_template = history(device)
    rows = [start]
    utc = start[1]["utc_ms"]
    for i, (seconds, before, after) in enumerate(parks):
        off, boot = copy.deepcopy(off_template[1]), copy.deepcopy(boot_template[1])
        utc += 3600000
        off.update(boot_id=f"{i + 1:032x}", boot_counter=10 + i, utc_ms=utc)
        off["context"]["battery_mv"] = before
        utc += seconds * 1000
        boot.update(boot_id=f"{i + 2:032x}", boot_counter=11 + i, utc_ms=utc)
        boot["context"]["battery_mv"] = after
        rows.extend([(device, off), (device, boot)])
    return rows


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

    def test_short_pair_median_corrects_long_pairs_and_preserves_raw_values(self):
        rows = parked_history([(8, 4137, 4152), (60, 4000, 4013), (120, 4000, 4100),
                               (48 * 3600, 4000, 4005)])
        raw = pair_events(list(reversed(rows)))
        original = copy.deepcopy(raw)
        pairs = correct_pairs(raw)
        self.assertEqual(raw, original)
        for pair in pairs:
            self.assertEqual(pair["rebound_pairs"], 3)
            self.assertEqual(pair["rebound_offset_mv"], 15)  # Median, not mean (42.667).
            self.assertEqual(pair["rebound_spread_mv"], 85)
            self.assertEqual(pair["rebound_status"], "calibrated")
        for pair in pairs[:3]:
            self.assertEqual(pair["pair_kind"], "short")
            self.assertEqual(pair["drain_status"], "calibration")
            self.assertIsNone(pair["corrected_mv_drop"])
            self.assertIsNone(pair["corrected_mv_per_hour"])
        long = pairs[-1]
        self.assertEqual(long["pair_kind"], "long")
        self.assertEqual(long["mv_drop"], -5)
        self.assertAlmostEqual(long["mv_per_hour"], -5 / 48)
        self.assertEqual(long["corrected_mv_drop"], 10)
        self.assertAlmostEqual(long["corrected_mv_per_hour"], 10 / 48)
        self.assertEqual(long["percentage_points_drop"], 5)
        self.assertEqual(long["drain_status"], "below resolution")

    def test_even_short_pair_median_is_not_rounded(self):
        rows = parked_history([(8, 4000, 4012), (8, 4000, 4015), (36000, 4000, 3990)])
        pair = correct_pairs(pair_events(rows))[-1]
        self.assertEqual(pair["rebound_offset_mv"], 13.5)
        self.assertEqual(pair["rebound_spread_mv"], 1.5)
        self.assertEqual(pair["corrected_mv_drop"], 23.5)
        self.assertEqual(pair["corrected_mv_per_hour"], 2.35)
        self.assertEqual(pair["drain_status"], "estimate")

    def test_no_short_pairs_leaves_long_pairs_uncorrected(self):
        pair = correct_pairs(pair_events(history()))[0]
        self.assertEqual(pair["rebound_pairs"], 0)
        self.assertEqual(pair["rebound_offset_mv"], 0)
        self.assertIsNone(pair["rebound_spread_mv"])
        self.assertEqual(pair["rebound_status"], "no short voltage pairs; uncorrected")
        self.assertEqual(pair["corrected_mv_drop"], 100)
        self.assertEqual(pair["corrected_mv_per_hour"], 10)
        self.assertEqual(pair["drain_status"], "estimate")

    def test_calibration_is_per_device(self):
        rows = (parked_history([(8, 4000, 4015), (36000, 4000, 3990)])
                + parked_history([(8, 4000, 4025), (36000, 4000, 3990)], "tracker-2")
                + history("tracker-3"))
        pairs = correct_pairs(pair_events(list(reversed(rows))))
        longs = {p["device_id"]: p for p in pairs if p["pair_kind"] == "long"}
        for device, offset, drop in (("tracker-1", 15, 25), ("tracker-2", 25, 35),
                                     ("tracker-3", 0, 100)):
            self.assertEqual(longs[device]["rebound_offset_mv"], offset)
            self.assertEqual(longs[device]["corrected_mv_drop"], drop)
        self.assertEqual(longs["tracker-3"]["rebound_pairs"], 0)

    def test_spread_flags_small_zero_and_negative_corrected_drops(self):
        rows = parked_history([(8, 4000, 4005), (8, 4000, 4015), (8, 4000, 4025)]
                              + [(36000, 4000, after) for after in (4013, 4005, 4004, 4030, 4015)])
        pairs = correct_pairs(pair_events(rows))[-5:]
        self.assertEqual([p["corrected_mv_drop"] for p in pairs], [2, 10, 11, -15, 0])
        self.assertEqual([p["drain_status"] for p in pairs],
                         ["below resolution", "below resolution", "estimate",
                          "below resolution", "below resolution"])
        self.assertTrue(all(p["resolution_mv"] == 10 for p in pairs))

    def test_small_reporting_floor_with_and_without_calibration(self):
        for shorts in ([], [(8, 4000, 4015)]):
            offset = 15 if shorts else 0
            rows = parked_history(shorts + [(36000, 4000, 4000 + offset - drop)
                                            for drop in (-5, 0, 2, 3, 4)])
            pairs = correct_pairs(pair_events(rows))[-5:]
            self.assertEqual([p["drain_status"] for p in pairs],
                             ["below resolution"] * 4 + ["estimate"])
            self.assertTrue(all(p["resolution_mv"] == 3 for p in pairs))

    def test_measured_eight_second_rebound_calibrates_fifteen_mv(self):
        rows = parked_history([(8, 4137, 4152), (48 * 3600, 4137, 4122)])
        short, long = correct_pairs(pair_events(rows))
        self.assertEqual(short["mv_drop"], -15)
        self.assertEqual(short["rebound_offset_mv"], 15)
        self.assertEqual(short["drain_status"], "calibration")
        self.assertEqual(long["corrected_mv_drop"], 30)
        self.assertEqual(long["corrected_mv_per_hour"], 0.625)
        self.assertEqual(long["drain_status"], "estimate")

    def test_cutoff_is_strict_and_configurable(self):
        rows = parked_history([(599, 4000, 4015), (600, 4000, 4010)])
        pairs = pair_events(rows)
        self.assertEqual([p["pair_kind"] for p in correct_pairs(pairs)], ["short", "long"])
        self.assertEqual([p["rebound_pairs"] for p in correct_pairs(pairs, 11)], [2, 2])
        self.assertEqual([p["rebound_pairs"] for p in correct_pairs(pairs, 1)], [0, 0])

    def test_missing_voltage_is_neither_calibration_nor_zero_drain(self):
        for index in (1, 2):
            rows = parked_history([(8, 4000, 4015), (36000, 4000, 3990)])
            del rows[index][1]["context"]["battery_mv"]
            short, long = correct_pairs(pair_events(rows))
            self.assertEqual(short["rebound_pairs"], 0)
            self.assertEqual(long["corrected_mv_drop"], 10)
            self.assertEqual(long["rebound_status"], "no short voltage pairs; uncorrected")
        rows = history()
        del rows[1][1]["context"]["battery_mv"]
        pair = correct_pairs(pair_events(rows))[0]
        self.assertIsNone(pair["corrected_mv_drop"])
        self.assertIsNone(pair["corrected_mv_per_hour"])
        self.assertEqual(pair["drain_status"], "unknown")
        self.assertEqual(pair["percentage_points_drop"], 5)

    def test_cli_appends_csv_columns_and_honors_cutoff(self):
        rows = parked_history([(8, 4137, 4152), (600, 4137, 4147)])
        for flags, kind, count in (([], "long", "1"), (["--short-minutes", "11"], "short", "2")):
            with mock.patch("parked_drain.read_events", return_value=rows):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(main(["events.db"] + flags), 0)
            reader = csv.DictReader(io.StringIO(output.getvalue()))
            self.assertEqual(reader.fieldnames[:7],
                             ["device_id", "off_utc", "boot_utc", "hours_parked", "mv_drop",
                              "mv_per_hour", "percentage_points_drop"])
            short, second = list(reader)
            self.assertEqual(short["mv_drop"], "-15.000")
            self.assertEqual(short["corrected_mv_drop"], "unknown")
            self.assertEqual(short["drain_status"], "calibration")
            self.assertEqual(second["pair_kind"], kind)
            self.assertEqual(second["rebound_pairs"], count)
            if not flags:
                self.assertEqual(second["corrected_mv_drop"], "5.000")
                self.assertEqual(second["corrected_mv_per_hour"], "30.000")

    def test_invalid_cutoff_is_rejected_before_database_read(self):
        for value in ("0", "-1", "nan", "inf", "-inf"):
            with self.subTest(value=value), mock.patch("parked_drain.read_events") as read:
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                    main(["events.db", "--short-minutes=" + value])
                self.assertEqual(error.exception.code, 2)
                read.assert_not_called()

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
            for i, (_device, row) in enumerate(reversed(history())):
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
            self.assertIn("no short voltage pairs; uncorrected", output.getvalue())
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

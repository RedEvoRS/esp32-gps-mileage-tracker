"""Catalog and wire-contract regressions; no Flask, database or ESP32 needed."""

import copy
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))
from common.diagnostics import (CatalogError, ProtocolError, load_catalog, parse_request,
                                validate_catalog, validate_device_id, validate_request)
from common.diagnostics.catalog import MAX_INTEGER, SEVERITIES, SUBSYSTEMS
from common.diagnostics.protocol import MAX_EVENT_BYTES, MAX_REQUEST_BYTES
from tools import generate_error_catalog as generator

BOOT = "0" * 31 + "1"
OTHER_BOOT = "0" * 31 + "2"
# Append a new version/fingerprint when intentionally changing published entries.
# Keeping prior fingerprints makes silent edits without a version bump fail.
PUBLISHED_CATALOGS = {
    1: "d908fd028755d8a1eee07f0ee130ad12291f21e201ddc13214bde24212c393e0",
    2: "1f7bfdb3135fc7c92d17013d3c271d8514f5ffd692e81223ad85877dd2dcd3cb",
    3: "0a1c02d50c538e8a4b65c0cfc68ea82126fade31957e2f4227f465113a4c009f",
    4: "6454b5e2c5958a8552ef5a04864cbae2ac0d02998f5fc78d514b8bfec533fb93",
    5: "061911d5a8a96724f2ef5aa9dd9938cc7419812145eadc9bf293d089434cca5c",
    6: "5f442ea44577763d8852e48e185a25564e98ae1b3a578ab5292fc1296874b4f0",
}


def event(code=4005, sequence=1, transition="raise", **overrides):
    value = dict(schema_version=1, catalog_version=1, firmware_version="test-1.0",
                 boot_id=BOOT, boot_counter=1, sequence=sequence, code=code,
                 transition=transition, episode_id=f"{BOOT}:1", uptime_ms=1200,
                 utc_ms=None, time_source="unknown", sync_age_ms=None, context={})
    value.update(overrides)
    return value


def request(events=None):
    return {"schema_version": 1,
            "checkin": {"boot_id": BOOT, "status_sequence": 1, "uptime_ms": 1500,
                        "active": [{"code": 4005, "episode_id": f"{BOOT}:1"}]},
            "events": [event()] if events is None else events}


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.catalog = load_catalog()

    def test_catalog_unique_codes_and_symbols(self):
        rows = self.catalog["codes"]
        self.assertEqual(len(rows), 48)
        self.assertEqual(len({r["code"] for r in rows}), len(rows))
        self.assertEqual(len({r["symbol"] for r in rows}), len(rows))

    def test_severities_subsystems_oled_and_context(self):
        for row in self.catalog["codes"]:
            with self.subTest(code=row["code"]):
                self.assertIn(row["code"] // 1000, SUBSYSTEMS)
                self.assertIn(row["severity"], SEVERITIES)
                self.assertTrue(1 <= len(row["oled"]) <= 3)
                self.assertTrue(all(0 < len(line) <= 21 and line.isascii() for line in row["oled"]))
                self.assertLessEqual(len(row["context"]), 8)
                for spec in row["context"].values():
                    self.assertIn(spec["type"], ("integer", "enum", "boolean"))

    def test_stable_code_subsystem_assignments(self):
        prefixes = {1: ("BOOT", "FS_", "NET_", "WATCHDOG_", "BROWNOUT_", "PANIC_", "ID_"),
                    2: ("PMU_", "BATTERY_", "CHARGE_"), 3: ("GPS_", "UTC_", "RTC_"),
                    4: ("SD_", "LITTLEFS_", "NO_RECORDING_", "STORAGE_", "TRIP_", "EVENT"),
                    5: ("HOME_", "WIFI_", "UPLOAD_", "EVENT_ACK_"),
                    6: ("DISPLAY_",), 7: ("DEVICE_",)}
        for row in self.catalog["codes"]:
            self.assertTrue(row["symbol"].startswith(prefixes[row["code"] // 1000]))

    def test_noisy_scan_is_counter_charge_warnings_do_not_notify(self):
        rows = {r["code"]: r for r in self.catalog["codes"]}
        self.assertEqual(rows[5001]["journal"], "counter")
        self.assertFalse(rows[5001]["notify"])
        for code in (2004, 2005, 3002):
            self.assertFalse(rows[code]["notify"])
        self.assertTrue(rows[7001]["notify"])

    def test_invalid_catalog_fixtures(self):
        changes = [lambda c: c.update(catalog_version=True),
                   lambda c: c.update(catalog_version=0),
                   lambda c: c["codes"].append(copy.deepcopy(c["codes"][0])),
                   lambda c: c["codes"][0].update(code=8000),
                   lambda c: c["codes"][0].update(code=True),
                   lambda c: c["codes"][1].update(symbol="BOOT"),
                   lambda c: c["codes"][0].update(symbol="bad symbol"),
                   lambda c: c["codes"][0].update(severity="fatal"),
                   lambda c: c["codes"][0].update(notify=1),
                   lambda c: c["codes"][0].update(journal="always"),
                   lambda c: c["codes"][0].update(oled=["x" * 22]),
                   lambda c: c["codes"][0].update(oled=["x"] * 4),
                   lambda c: c["codes"][0].update(oled=["\n"]),
                   lambda c: c["codes"][0].update(context={"text": {"type": "string"}}),
                   lambda c: c["codes"][0].update(context={"n": {"type": "integer", "min": 2, "max": 1}}),
                   lambda c: c["codes"][0].update(context={"n": {"type": "integer", "min": False, "max": 1}}),
                   lambda c: c["codes"][0].update(context={"s": {"type": "enum", "values": ["a", "a"]}}),
                   lambda c: c["codes"][0].update(context={"s": {"type": "enum", "values": ["x" * 33]}})]
        for index, change in enumerate(changes):
            with self.subTest(fixture=index):
                bad = copy.deepcopy(self.catalog)
                change(bad)
                with self.assertRaises(CatalogError):
                    validate_catalog(bad)

    def test_pmu_startup_recovery_is_non_notifying_boot_history(self):
        row = next(row for row in self.catalog["codes"] if row["code"] == 2007)
        self.assertEqual(row["symbol"], "PMU_STARTUP_RECOVERY")
        self.assertEqual(row["severity"], "warning")
        self.assertFalse(row["notify"])
        self.assertEqual(row["journal"], "per_occurrence")

    def test_charge_control_failure_and_pause_wording(self):
        rows = {row["code"]: row for row in self.catalog["codes"]}
        self.assertEqual(self.catalog["catalog_version"], 6)
        self.assertEqual(rows[2008]["symbol"], "CHARGE_CONTROL_FAILED")
        self.assertEqual(rows[2008]["severity"], "error")
        self.assertTrue(rows[2008]["notify"])
        self.assertEqual(rows[2008]["journal"], "transitions")
        self.assertEqual(rows[2008]["context"], {})
        for code in (2004, 2005):
            self.assertEqual(rows[code]["oled"][0], "Charge pause needed")

    def test_loader_fails_on_invalid_and_duplicate_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog.json"
            for raw in ('{', '{"catalog_version":1,"catalog_version":2,"codes":[]}'):
                path.write_text(raw, encoding="utf-8")
                with self.assertRaises(CatalogError):
                    load_catalog(path)

    def test_load_returns_independent_copies(self):
        self.catalog["codes"][0]["title"] = "Changed"
        self.assertNotEqual(load_catalog()["codes"][0]["title"], "Changed")

    def test_generator_check_and_version_agreement(self):
        result = subprocess.run([sys.executable, str(ROOT / "tools/generate_error_catalog.py"), "--check"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        header = generator.OUTPUT.read_text(encoding="utf-8")
        version = int(re.search(r"kCatalogVersion = (\d+);", header)[1])
        self.assertEqual(version, self.catalog["catalog_version"])
        for row in self.catalog["codes"]:
            self.assertIn(f'{row["symbol"]} = {row["code"]},', header)

    def test_check_detects_drift_and_does_not_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "diag_catalog.h"
            with mock.patch.object(generator, "OUTPUT", path):
                self.assertEqual(generator.main(["--check"]), 1)
                self.assertFalse(path.exists())
                self.assertEqual(generator.main([]), 0)
                self.assertEqual(generator.main(["--check"]), 0)
                path.write_text("stale", encoding="utf-8")
                self.assertEqual(generator.main(["--check"]), 1)
                self.assertEqual(path.read_text(), "stale")

    def test_catalog_edits_require_version_bump(self):
        self.assertEqual(generator.catalog_digest(self.catalog),
                         PUBLISHED_CATALOGS[self.catalog["catalog_version"]])
        changed = copy.deepcopy(self.catalog)
        changed["codes"][0]["title"] += " changed"
        self.assertNotEqual(generator.catalog_digest(changed),
                            PUBLISHED_CATALOGS[changed["catalog_version"]])


class ProtocolTests(unittest.TestCase):
    def test_battery_history_context_limits_and_rolling_upgrade(self):
        catalog = load_catalog()
        row = next(r for r in catalog["codes"] if r["code"] == 2009)
        self.assertEqual((row["severity"], row["notify"], row["journal"]),
                         ("info", False, "per_occurrence"))
        for code in (1000, 2009):
            # Combined largest battery fields still fit the 502-byte frame,
            # even at maximum counters and a full 32-character version token.
            combined = {"battery_mv": 6000, "battery_pct": 100}
            combined.update({"reset_reason": "deep_sleep"} if code == 1000
                            else {"reason": "battery_critical"})
            validate_request(request([event(code, transition="notice", episode_id=None,
                firmware_version="v" * 32, boot_counter=MAX_INTEGER, sequence=MAX_INTEGER,
                uptime_ms=4294967295, utc_ms=253402300799999, time_source="gps",
                sync_age_ms=MAX_INTEGER, context=combined)]))
            for context in ({}, {"battery_mv": 4000, "battery_pct": 0},
                            {"battery_mv": 6000, "battery_pct": 100}):
                value = request([event(code, transition="notice", episode_id=None,
                                       catalog_version=6, context=context)])
                validate_request(value)
            for context in ({"battery_mv": 0}, {"battery_mv": -1}, {"battery_mv": 6001},
                            {"battery_pct": -1}, {"battery_pct": 101}, {"battery_pct": True}):
                self.invalid(request([event(code, transition="notice", episode_id=None, context=context)]))
        old = copy.deepcopy(catalog)
        old["catalog_version"] = 5
        old["codes"] = [r for r in old["codes"] if r["code"] != 2009]
        for r in old["codes"]:
            if r["code"] == 1000:
                r["context"] = {"reset_reason": r["context"]["reset_reason"]}
        for code in (1000, 2009):
            with self.assertRaises(ProtocolError):
                validate_request(request([event(code, transition="notice", episode_id=None,
                    context={"battery_mv": 4000})]), catalog=old)
        # The new catalog still accepts old firmware's BOOT context.
        validate_request(request([event(1000, transition="notice", episode_id=None,
                                       context={"reset_reason": "power_on"})]))

    def invalid(self, value):
        with self.assertRaises(ProtocolError):
            validate_request(value)

    def test_valid_request_roundtrip_and_no_mutation(self):
        value = request()
        before = copy.deepcopy(value)
        self.assertIs(validate_request(value), value)
        self.assertEqual(parse_request(json.dumps(value).encode()), value)
        self.assertEqual(value, before)

    def test_checkin_only_and_optional_health_fields(self):
        value = request([])
        value["checkin"].update(firmware_version="v1.0+test", boot_counter=5,
                               storage_backend="sd", pending_events=900,
                               dropped_events=3, counters={"5001": 27}, observed=[4005, 4004])
        self.assertIs(validate_request(value), value)

    def test_identifier_formats(self):
        for good in ("tracker-1", "a", "1_test", "a" * 32):
            self.assertEqual(validate_device_id(good), good)
        for bad in ("", "A", "a" * 33, "../tracker", "a\nb", "x y", None, 1):
            with self.subTest(device_id=bad), self.assertRaises(ProtocolError):
                validate_device_id(bad)
        for bad in (BOOT.upper().replace("1", "A"), "a" * 31, "../boot", 1):
            value = request()
            value["events"][0]["boot_id"] = bad
            self.invalid(value)
        for bad in (f"{BOOT}:0", f"{BOOT}:01", f"{BOOT}:2", f"{OTHER_BOOT}:1", None):
            self.invalid(request([event(episode_id=bad)]))

    def test_transitions_and_notice_policies(self):
        for transition in ("repeat", "clear"):
            validate_request(request([event(sequence=2, transition=transition)]))
        validate_request(request([event(1003, transition="notice", episode_id=None)]))
        for bad in (event(1003), event(5001, transition="notice", episode_id=None),
                    event(4005, transition="notice", episode_id=None), event(transition="unknown"),
                    event(transition="notice"), event(sequence=2)):
            self.invalid(request([bad]))

    def test_unknown_codes_retained_without_trusting_severity(self):
        value = request([event(code=4999, catalog_version=2)])
        validate_request(value)
        value["events"][0]["severity"] = "info"
        self.invalid(value)
        self.invalid(request([event(code=4999, context={"raw": "untrusted"})]))

    def test_pmu_startup_notice_context_and_no_condition_semantics(self):
        context = dict(clock_count=27, attempt_count=3, recovery_state="sda_stuck",
                       pmu_ready=False, wire_end_failed=False)
        notice = event(2007, catalog_version=2, transition="notice", episode_id=None,
                       context=context)
        validate_request(request([notice]))
        for transition in ("raise", "repeat", "clear"):
            self.invalid(request([dict(notice, transition=transition, episode_id=f"{BOOT}:1")]))
        for field in ("active", "observed"):
            value = request([])
            value["checkin"][field] = ([{"code": 2007, "episode_id": f"{BOOT}:1"}]
                                          if field == "active" else [4005, 2007])
            self.invalid(value)

    def test_devices_cannot_report_server_monitor_codes(self):
        self.invalid(request([event(code=7001)]))
        value = request([])
        value["checkin"]["active"] = [{"code": 7001, "episode_id": f"{BOOT}:1"}]
        self.invalid(value)

    def test_all_catalog_context_examples_and_bounds(self):
        for row in load_catalog()["codes"]:
            if row["code"] > 6999:
                continue  # server-raised; devices cannot send these
            if row["journal"] == "counter":
                continue
            context = {}
            for key, spec in row["context"].items():
                context[key] = (spec["min"] if spec["type"] == "integer" else
                                spec["values"][0] if spec["type"] == "enum" else False)
            notice = row["journal"] == "per_occurrence"
            valid = event(row["code"], transition="notice" if notice else "raise",
                          episode_id=None if notice else f"{BOOT}:1", context=context)
            with self.subTest(code=row["code"]):
                validate_request(request([valid]))
                for key, spec in row["context"].items():
                    for invalid_value in ([spec["min"] - 1, spec["max"] + 1, True, 1.0]
                                          if spec["type"] == "integer" else ["not-allowed", None]):
                        bad = copy.deepcopy(valid)
                        bad["context"][key] = invalid_value
                        self.invalid(request([bad]))

    def test_no_free_text_context_or_extra_fields(self):
        for context in ({"ssid": "placeholder"}, {"coordinates": [0, 0]}, {"raw_body": "body"}, []):
            self.invalid(request([event(context=context)]))
        for level in ("body", "checkin", "event", "active"):
            value = request()
            target = {"body": value, "checkin": value["checkin"], "event": value["events"][0],
                      "active": value["checkin"]["active"][0]}[level]
            target["unexpected"] = 1
            self.invalid(value)

    def test_numeric_types_and_limits(self):
        for field in ("schema_version", "catalog_version", "boot_counter", "sequence", "uptime_ms", "code"):
            for bad in (True, 1.0, -1, MAX_INTEGER + 1, None):
                self.invalid(request([event(**{field: bad})]))
        for version in (0, 2, True):
            value = request()
            value["schema_version"] = version
            self.invalid(value)

    def test_time_confidence(self):
        for source in ("gps", "rtc", "sntp"):
            validate_request(request([event(time_source=source, utc_ms=1700000000000, sync_age_ms=2000)]))
        for fields in ({"utc_ms": 1}, {"time_source": "gps"}, {"sync_age_ms": 2},
                       {"time_source": "local"}, {"time_source": "gps", "utc_ms": True},
                       {"time_source": "gps", "utc_ms": 253402300800000}, {"sync_age_ms": -1}):
            self.invalid(request([event(**fields)]))

    def test_backlog_boots_need_not_match_snapshot(self):
        value = request()
        value["checkin"].update(boot_id=OTHER_BOOT, active=[])
        validate_request(value)

    def test_active_fault_bounds_and_counters(self):
        mutations = [lambda c: c.update(active=[c["active"][0]] * 2),
                     lambda c: c.update(active=[{"code": 5001, "episode_id": f"{BOOT}:1"}]),
                     lambda c: c.update(active=[{"code": 1000, "episode_id": f"{BOOT}:1"}]),
                     lambda c: c.update(active=[{"code": 4005, "episode_id": f"{OTHER_BOOT}:1"}]),
                     lambda c: c.update(active=[{}] * 65), lambda c: c.update(status_sequence=True),
                     lambda c: c.update(status_sequence=0), lambda c: c.update(uptime_ms=-1),
                     lambda c: c.update(boot_id="bad"), lambda c: c.update(firmware_version="bad text"),
                     lambda c: c.update(storage_backend="disk"), lambda c: c.update(pending_events=1.5),
                     lambda c: c.update(counters={"5001": -1}), lambda c: c.update(counters={"5001": True}),
                     lambda c: c.update(counters={"5002": 1}), lambda c: c.update(counters=[]),
                     lambda c: c.update(observed=[]), lambda c: c.update(observed=[4005, 4005]),
                     lambda c: c.update(observed=[4005, 1000]), lambda c: c.update(observed=[4005, True]),
                     lambda c: c.update(observed=[4005] * 65)]
        for index, mutate in enumerate(mutations):
            with self.subTest(fixture=index):
                value = request()
                mutate(value["checkin"])
                self.invalid(value)

    def test_batch_count_and_duplicate_identity(self):
        batch = [event(sequence=i, episode_id=f"{BOOT}:{i}") for i in range(1, 65)]
        validate_request(request(batch))
        self.invalid(request(batch + [event(sequence=65, episode_id=f"{BOOT}:65")]))
        self.invalid(request([event(), event()]))

    def test_raw_size_and_event_frame_limits(self):
        raw = json.dumps(request()).encode()
        parse_request(raw + b" " * (MAX_REQUEST_BYTES - len(raw)))
        with self.assertRaises(ProtocolError):
            parse_request(raw + b" " * (MAX_REQUEST_BYTES - len(raw) + 1))
        # Mock the cap, not the encoder: this exercises the exact compact byte boundary.
        sample = event()
        size = len(json.dumps(sample, separators=(",", ":")).encode())
        self.assertLessEqual(size, MAX_EVENT_BYTES)
        with mock.patch("common.diagnostics.protocol.MAX_EVENT_BYTES", size):
            validate_request(request([sample]))
        with mock.patch("common.diagnostics.protocol.MAX_EVENT_BYTES", size - 1):
            self.invalid(request([sample]))

    def test_combined_checkin_and_events_must_fit_request(self):
        batch = [event(code=4004, sequence=MAX_INTEGER - i, boot_counter=MAX_INTEGER,
                       uptime_ms=MAX_INTEGER, firmware_version="v" * 32,
                       episode_id=f"{BOOT}:{MAX_INTEGER - i}",
                       time_source="gps", utc_ms=253402300799999, sync_age_ms=MAX_INTEGER,
                       context={"backend": "littlefs", "free_bytes": MAX_INTEGER}) for i in range(64)]
        value = request(batch)
        value["checkin"]["active"] = [{"code": 6100 + i, "episode_id": f"{BOOT}:1"} for i in range(64)]
        self.assertGreater(len(json.dumps(value, separators=(",", ":")).encode()), MAX_REQUEST_BYTES)
        self.invalid(value)

    def test_bad_json_duplicate_keys_nan_encoding_and_nesting(self):
        for raw in (b"{", b"\xff", b"\xef\xbb\xbf{}", b"[]", b"null", b"NaN", b"Infinity",
                    b'{"schema_version":1,"schema_version":1}', b"[" * 1200 + b"]" * 1200):
            with self.subTest(raw_prefix=raw[:20]), self.assertRaises(ProtocolError):
                parse_request(raw)
        for raw in ("{}", None):
            with self.assertRaises(ProtocolError):
                parse_request(raw)

    def test_errors_do_not_echo_untrusted_values(self):
        sentinel = "private-placeholder-do-not-echo"
        with self.assertRaises(ProtocolError) as caught:
            validate_device_id(sentinel + "!")
        self.assertNotIn(sentinel, str(caught.exception))


if __name__ == "__main__":
    unittest.main()

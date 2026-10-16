"""Monitor policy against real ingest schema, fake time and an HTTP ntfy stub."""

import copy
import importlib.util
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock


SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
from common.diagnostics import load_catalog

spec = importlib.util.spec_from_file_location("monitor_under_test", SERVER / "mileage-monitor/monitor.py")
monitor = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = monitor
spec.loader.exec_module(monitor)
spec = importlib.util.spec_from_file_location("monitor_diag_store", SERVER / "mileage-ingest/diag_store.py")
store_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(store_module)
BOOT = "0" * 31 + "1"
OTHER_BOOT = "0" * 31 + "2"


class ConfigTests(unittest.TestCase):
    def test_defaults_disabled(self):
        self.assertEqual(monitor.Config.from_env({}), monitor.Config())

    def test_enabled_without_delivery_needs_no_token(self):
        config = monitor.Config.from_env({"MONITOR_ENABLED": "true"})
        self.assertTrue(config.enabled)
        self.assertFalse(config.notify)
        self.assertEqual(config.stale_seconds, 7 * 86400)

    def test_valid_delivery_config_and_nondefault_numbers(self):
        for url in ("https://notify.example.com/topic", "http://localhost:8092/topic",
                    "http://[::1]:8092/topic", "https://notify.example.com/prefix/topic"):
            config = monitor.Config.from_env({"MONITOR_ENABLED": "true", "NOTIFY_ENABLED": "true",
                "NTFY_URL": url, "NTFY_TOKEN": "test-token", "NOTIFY_HOLD_SECONDS": "0",
                "NOTIFY_REPEAT_HOURS": "0.5", "NOTIFY_MAX_PER_HOUR": "1", "MONITOR_STALE_DAYS": "1.5"})
            self.assertTrue(config.notify)
            self.assertEqual(config.repeat_seconds, 1800)
            self.assertEqual(config.stale_seconds, 129600)

    def test_bad_config_fails_without_echoing_values(self):
        baseline = {"MONITOR_ENABLED": "true", "NOTIFY_ENABLED": "true",
                    "NTFY_URL": "http://localhost/topic", "NTFY_TOKEN": "test-token"}
        cases = {"MONITOR_ENABLED": ["yes"], "NOTIFY_ENABLED": ["1"],
                 "NTFY_URL": ["", "ftp://host/topic", "https://host", "https://u:p@host/t",
                              "http://host/t?secret=value", "http://host/t#frag", "http://host:bad/t",
                              "http://host:0/t", "http://host/\nt", "http://<host>/topic",
                              "http://host\\invalid/topic", "http://host/<topic>", "http://host/topic/"],
                 "NTFY_TOKEN": ["", "<token>", "contains space", "bad\r\nheader", "\u00e9"],
                 "NOTIFY_MIN_SEVERITY": ["unknown"], "NOTIFY_HOLD_SECONDS": ["-1", "nan", "inf", "bad"],
                 "NOTIFY_REPEAT_HOURS": ["0", "-1"], "NOTIFY_MAX_PER_HOUR": ["0", "1.5", "1001"],
                 "MONITOR_STALE_DAYS": ["0", "-1", "nan", "36501"]}
        for key, values in cases.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaisesRegex(ValueError, key):
                    monitor.Config.from_env(dict(baseline, **{key: value}))

    def test_catalog_required_even_disabled(self):
        with mock.patch.object(monitor, "load_catalog", side_effect=ValueError("missing catalog")):
            with self.assertRaisesRegex(ValueError, "missing catalog"):
                monitor.Monitor(monitor.Config())

    def test_missing_catalog_file_is_a_clear_startup_error(self):
        from common.diagnostics import catalog
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.object(catalog, "CATALOG_PATH", Path(root) / "absent.json"):
                with self.assertRaisesRegex(ValueError, "missing or invalid diagnostics catalog.json"):
                    monitor.Monitor(monitor.Config())


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = 1800000000.0
        self.catalog = load_catalog()
        self.store = store_module.DiagnosticsStore(self.root / "data", self.catalog)
        self.state = self.root / "state/monitor.db"
        self.messages = []
        self.settings = dict(enabled=True, notify=True, hold_seconds=300)
        self.worker = self.make_monitor()
        self.revision = 0

    def make_monitor(self, **changes):
        return monitor.Monitor(monitor.Config(**dict(self.settings, **changes)), self.store.path, self.state,
                               clock=lambda: self.now, sender=self.messages.append, catalog=self.catalog)

    def sql(self, sql, parameters=(), *, state=False):
        with closing(sqlite3.connect(self.state if state else self.store.path)) as db:
            with db:
                return db.execute(sql, parameters).fetchall()

    def snapshot(self, active=(4002,), observed=None, *, boot=BOOT, device="tracker-1", at=None,
                 episode=1):
        self.revision += 1
        snapshot = {"boot_id": boot, "status_sequence": self.revision, "uptime_ms": 100,
                    "active": [{"code": code, "episode_id": boot + ":" + str(episode)} for code in active]}
        if observed is not None:
            snapshot["observed"] = observed
        self.store.accept(device, {"schema_version": 1, "checkin": snapshot, "events": []})
        stamp = datetime.fromtimestamp(self.now if at is None else at, timezone.utc).isoformat()
        self.sql("UPDATE devices SET last_contact_at=?,snapshot_received_at=? WHERE device_id=?",
                 (stamp, stamp, device))

    def tick(self, seconds=0):
        self.now += seconds
        return self.worker.tick()

    def test_disabled_does_not_read_send_or_create_state(self):
        state = self.root / "disabled.db"
        worker = monitor.Monitor(monitor.Config(), self.store.path, state, sender=self.messages.append)
        with mock.patch.object(monitor, "read_devices", side_effect=AssertionError("must not read")):
            self.assertTrue(worker.tick())
        self.assertFalse(state.exists())
        self.assertEqual(self.messages, [])

    def test_monitor_without_notifications_tracks_but_never_sends(self):
        self.worker = self.make_monitor(notify=False)
        self.snapshot(active=(4003,))
        self.tick()
        self.assertTrue(self.sql("SELECT * FROM episodes", state=True))
        self.assertEqual(self.messages, [])

    def test_missing_db_does_not_create_it(self):
        self.assertTrue(self.tick())
        self.assertFalse(self.store.path.exists())
        self.assertEqual(self.messages, [])

    def test_corrupt_unreadable_and_unsupported_db_retry(self):
        self.store.path.parent.mkdir(parents=True)
        self.store.path.write_bytes(b"not a database")
        with self.assertLogs("mileage-monitor", "WARNING"):
            self.assertFalse(self.tick())
        self.store.path.unlink()
        self.snapshot(active=(4003,))
        with mock.patch.object(monitor, "read_devices", side_effect=PermissionError), self.assertLogs("mileage-monitor", "WARNING"):
            self.assertFalse(self.tick())
        self.sql("UPDATE schema_version SET version=999")
        with self.assertLogs("mileage-monitor", "WARNING"):
            self.assertFalse(self.tick())
        self.assertEqual(self.messages, [])
        self.sql("UPDATE schema_version SET version=1")
        self.assertTrue(self.tick())
        self.assertEqual(len(self.messages), 1)

    def test_fresh_snapshot_boundary_and_gpx_does_not_refresh_it(self):
        self.snapshot(active=(4003,), at=self.now - 86401)
        self.sql("UPDATE devices SET last_contact_at=?", (datetime.fromtimestamp(self.now, timezone.utc).isoformat(),))
        self.tick()
        self.assertEqual(self.messages, [])
        self.sql("UPDATE devices SET snapshot_received_at=?", (datetime.fromtimestamp(self.now - 86400, timezone.utc).isoformat(),))
        self.tick()
        self.assertEqual(len(self.messages), 1)
        self.tick(86400)
        self.assertEqual(len(self.messages), 1)  # stale snapshot cannot remind

    def test_catalog_invalid_snapshot_preserves_silence_and_other_devices(self):
        self.snapshot(active=(4002,), device="tracker-old", at=self.now - 7 * 86400)
        self.snapshot(active=(4003,), device="tracker-good")
        self.catalog = copy.deepcopy(self.catalog)
        next(row for row in self.catalog["codes"] if row["code"] == 4002)["journal"] = "per_occurrence"
        self.worker = self.make_monitor()
        with self.assertLogs("mileage-monitor", "WARNING") as logs:
            self.assertTrue(self.tick())
        self.assertEqual(logs.output, [
            "WARNING:mileage-monitor:Device snapshot unavailable (ProtocolError); conditions unknown"])
        self.assertEqual(len(self.messages), 2)
        self.assertTrue(any("Alert E7001" in text and "tracker-old" in text for text in self.messages))
        self.assertTrue(any("Alert E4003" in text and "tracker-good" in text for text in self.messages))
        self.assertFalse(any("E4002" in text for text in self.messages))
        self.worker = self.make_monitor()
        with self.assertNoLogs("mileage-monitor", "WARNING"):
            self.assertTrue(self.tick(60))
        # A valid replacement resumes normal fault policy and contact recovery.
        self.snapshot(active=(4003,), device="tracker-old", boot=OTHER_BOOT)
        self.tick()
        self.assertTrue(any("Alert E4003" in text and "tracker-old" in text for text in self.messages))
        self.assertTrue(any("Resolved E7001" in text for text in self.messages))

    def test_corrupt_snapshot_is_unknown_and_warns_once_per_device_revision(self):
        self.snapshot(active=(4003,), device="tracker-bad")
        self.tick()
        self.snapshot(active=(), observed=[4003], device="tracker-bad", at=self.now - 7 * 86400)
        self.sql("UPDATE devices SET snapshot_json=? WHERE device_id=?",
                 ("{private stored text", "tracker-bad"))
        self.snapshot(active=(4003,), device="tracker-good")
        with self.assertLogs("mileage-monitor", "WARNING") as logs:
            self.assertTrue(self.tick())
        self.assertEqual(logs.output, [
            "WARNING:mileage-monitor:Device snapshot unavailable (JSONDecodeError); conditions unknown"])
        self.assertEqual(self.sql("SELECT state FROM episodes WHERE device='tracker-bad' AND code=4003",
                                  state=True), [("unknown",)])
        self.assertEqual(len(self.messages), 3)  # Original alert, silence and other device's fault.
        self.assertFalse(any("Resolved E4003" in text for text in self.messages))
        self.worker = self.make_monitor()
        with self.assertNoLogs("mileage-monitor", "WARNING"):
            self.assertTrue(self.tick(60))
        self.sql("UPDATE devices SET status_sequence=status_sequence+1 WHERE device_id='tracker-bad'")
        with self.assertLogs("mileage-monitor", "WARNING") as logs:
            self.assertTrue(self.tick())
        self.assertEqual(len(logs.output), 1)
        # Each device logs once, even if both share the same bad revision.
        self.snapshot(active=(), device="tracker-second")
        self.sql("UPDATE devices SET snapshot_json=?,status_sequence="
                 "(SELECT status_sequence FROM devices WHERE device_id='tracker-bad') "
                 "WHERE device_id='tracker-second'", ("{private stored text",))
        with self.assertLogs("mileage-monitor", "WARNING") as logs:
            self.assertTrue(self.tick())
        self.assertEqual(len(logs.output), 1)
        # New contact resolves silence even while its health snapshot stays bad.
        stamp = datetime.fromtimestamp(self.now, timezone.utc).isoformat()
        self.sql("UPDATE devices SET last_contact_at=?", (stamp,))
        with self.assertNoLogs("mileage-monitor", "WARNING"):
            self.assertTrue(self.tick())
        self.assertIn("Resolved E7001", self.messages[-1])
        self.assertFalse(any("Resolved E4003" in text for text in self.messages))

    def test_malformed_snapshot_fields_and_receipts_are_isolated(self):
        for value, column in (("null", "snapshot_json"), ("[]", "snapshot_json"),
                              ("[" * 1100, "snapshot_json"), ("not a receipt", "snapshot_received_at"),
                              ("not a receipt", "last_contact_at")):
            with self.subTest(column=column, value=value[:20]):
                self.snapshot(active=(4003,), device="tracker-bad")
                self.snapshot(active=(4003,), device="tracker-good")
                self.sql(f"UPDATE devices SET {column}=? WHERE device_id='tracker-bad'", (value,))
                with self.assertLogs("mileage-monitor", "WARNING"):
                    self.assertTrue(self.tick())
                self.assertEqual(len(self.messages), 1)
                self.assertTrue(all("tracker-good" in text for text in self.messages))

    def test_history_alone_never_alerts(self):
        self.snapshot(active=(), observed=[4003])
        event = {"code": 4003, "transition": "raise"}
        self.sql("INSERT INTO events VALUES (?,?,?,?,?,?,?,?)",
                 ("tracker-1", BOOT, 1, 4003, "critical", None, "2026-01-01T00:00:00+00:00", json.dumps(event)))
        self.tick(300)
        self.assertEqual(self.messages, [])

    def test_hold_boundary(self):
        self.snapshot()
        self.tick()
        self.tick(299)
        self.assertEqual(self.messages, [])
        self.tick(1)
        self.assertEqual(len(self.messages), 1)

    def test_critical_bypasses_hold(self):
        self.snapshot(active=(4003,))
        self.tick()
        self.assertEqual(len(self.messages), 1)
        self.assertIn("E4003", self.messages[0])

    def test_unknown_breaks_hold(self):
        self.snapshot()
        self.tick()
        self.tick(299)
        self.snapshot(active=())
        self.tick()
        self.snapshot()
        self.tick(1)
        self.assertEqual(self.messages, [])
        self.tick(300)
        self.assertEqual(len(self.messages), 1)

    def test_dedupe_and_hold_survive_restart(self):
        self.snapshot()
        self.tick()
        self.tick(200)
        self.worker = self.make_monitor()
        self.tick(100)
        self.assertEqual(len(self.messages), 1)
        self.worker = self.make_monitor()
        self.tick(100)
        self.assertEqual(len(self.messages), 1)

    def test_reminder_timing_and_restart(self):
        self.snapshot(active=(4003,))
        self.tick()
        self.tick(86399)
        self.assertEqual(len(self.messages), 1)
        self.worker = self.make_monitor()
        self.snapshot(active=(4003,))
        self.tick(1)
        self.assertEqual(len(self.messages), 2)
        self.assertIn("Reminder", self.messages[-1])
        self.tick()
        self.assertEqual(len(self.messages), 2)

    def test_hourly_cap_digest_and_persistent_overflow(self):
        self.worker = self.make_monitor(max_per_hour=2)
        for i in range(5):
            self.snapshot(active=(4003,), device=f"tracker-{i}")
        self.tick()
        self.assertEqual(len(self.messages), 2)
        self.assertIn("Diagnostics digest", self.messages[-1])
        self.assertEqual(sum("Device:" in line for text in self.messages for line in text.splitlines()), 5)
        self.snapshot(active=(4003,), device="tracker-new")
        self.tick()
        self.assertEqual(len(self.messages), 2)
        self.worker = self.make_monitor(max_per_hour=2)
        self.tick(3599)
        self.assertEqual(len(self.messages), 2)
        self.tick(1)
        self.assertEqual(len(self.messages), 3)
        self.assertIn("Diagnostics digest", self.messages[-1])
        self.assertIn("tracker-new", self.messages[-1])

    def test_digest_chunks_stay_under_ntfy_limit(self):
        self.worker = self.make_monitor(max_per_hour=1)
        for i in range(30):
            self.snapshot(active=(4003,), device=f"tracker-{i}")
        self.tick()
        self.assertEqual(len(self.messages), 1)
        self.assertLessEqual(len(self.messages[0].encode()), monitor.MAX_BODY_BYTES)
        self.assertTrue(self.sql("SELECT * FROM work", state=True))
        self.tick(3600)
        self.assertEqual(len(self.messages), 2)
        self.assertIn("Diagnostics digest", self.messages[-1])

    def test_resolution_requires_a_sent_alert_and_explicit_clear(self):
        self.snapshot()
        self.tick()
        self.snapshot(active=(), observed=[4002])
        self.tick(300)
        self.assertEqual(self.messages, [])
        self.snapshot(active=(4003,))
        self.tick()
        self.snapshot(active=(), observed=[4003])
        self.tick()
        self.assertEqual(len(self.messages), 2)
        self.assertIn("Resolved E4003", self.messages[-1])
        self.worker = self.make_monitor()
        self.tick()
        self.assertEqual(len(self.messages), 2)

    def test_unknown_reboot_and_old_clear_event_do_not_resolve(self):
        self.snapshot(active=(4003,))
        self.tick()
        self.snapshot(active=(), boot=OTHER_BOOT)
        self.tick()
        self.sql("INSERT INTO events VALUES (?,?,?,?,?,?,?,?)",
                 ("tracker-1", BOOT, 1, 4003, "critical", None,
                  "2026-01-01T00:00:00+00:00", json.dumps({"transition": "clear"})))
        self.tick()
        self.assertEqual(len(self.messages), 1)
        self.snapshot(active=(), observed=[4003], boot=OTHER_BOOT)
        self.tick()
        self.assertEqual(len(self.messages), 2)

    def test_deferred_resolution_requires_current_fresh_clear_evidence(self):
        self.worker = self.make_monitor(max_per_hour=1)
        self.snapshot(active=(4003,))
        self.tick()
        self.snapshot(active=(), observed=[4003])
        self.tick()
        self.assertEqual(len(self.messages), 1)  # cap defers resolution
        self.snapshot(active=(), boot=OTHER_BOOT)
        self.tick(3600)
        self.assertEqual(len(self.messages), 1)  # unknown cannot deliver it
        self.snapshot(active=(), observed=[4003], boot=OTHER_BOOT, at=self.now - 86401)
        self.tick()
        self.assertEqual(len(self.messages), 1)  # neither can stale clear evidence
        self.snapshot(active=(), observed=[4003], boot=OTHER_BOOT)
        self.tick()
        self.assertIn("Resolved E4003", self.messages[-1])

    def test_rate_cap_applies_to_resolution_and_survives_restart(self):
        self.worker = self.make_monitor(max_per_hour=1)
        self.snapshot(active=(4003,))
        self.tick()
        self.snapshot(active=(), observed=[4003])
        self.tick(3599)
        self.assertEqual(len(self.messages), 1)
        self.worker = self.make_monitor(max_per_hour=1)
        self.tick(1)
        self.assertEqual(len(self.messages), 2)
        self.assertIn("Diagnostics digest", self.messages[-1])
        self.assertIn("Resolved", self.messages[-1])

    def test_snapshot_rollback_cannot_resolve_alerted_episode(self):
        self.snapshot(active=(4003,))
        self.tick()
        self.snapshot(active=(), observed=[4003])
        snapshot = json.loads(self.sql("SELECT snapshot_json FROM devices")[0][0])
        snapshot["status_sequence"] = 1
        self.sql("UPDATE devices SET status_sequence=1,snapshot_json=?", (json.dumps(snapshot),))
        self.tick()
        self.assertEqual(len(self.messages), 1)

    def test_locked_db_reports_unavailable_and_retries(self):
        self.snapshot(active=(4003,))
        with closing(sqlite3.connect(self.store.path)) as writer:
            writer.execute("BEGIN EXCLUSIVE")
            with self.assertLogs("mileage-monitor", "WARNING"):
                self.assertFalse(self.tick())
            writer.rollback()
        self.assertEqual(self.messages, [])
        self.tick()
        self.assertEqual(len(self.messages), 1)

    def test_open_fault_spans_hourly_wakes_until_clear_and_resolution(self):
        self.snapshot(active=(4002,))
        self.tick()
        self.tick(300)
        for hour in range(1, 49):
            self.now += 3600
            boot = f"{hour + 1:032x}"
            self.snapshot(active=(4002,), boot=boot)
            self.tick()
            if hour in (12, 36):
                self.worker = self.make_monitor()  # Sent state survives restart.
        self.assertEqual(sum("Alert E4002" in text for text in self.messages), 1)
        self.assertEqual(sum("Reminder E4002" in text for text in self.messages), 2)
        self.assertEqual(len(self.messages), 3)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM episodes WHERE code=4002", state=True), [(1,)])
        self.snapshot(active=(), observed=[4002], boot=boot)
        self.tick()
        self.assertIn("Resolved E4002", self.messages[-1])
        self.assertEqual(len(self.messages), 4)
        self.snapshot(active=(4002,), boot=f"{50:032x}")
        self.tick()
        self.tick(299)
        self.assertEqual(len(self.messages), 4)
        self.tick(1)
        self.assertIn("Alert E4002", self.messages[-1])
        self.assertEqual(len(self.messages), 5)

    def test_open_fault_returns_from_unknown_with_reminder_without_new_hold(self):
        self.snapshot(active=(4002,))
        self.tick()
        self.tick(300)
        self.snapshot(active=(), boot=OTHER_BOOT)
        self.tick(86400)
        self.worker = self.make_monitor()
        self.snapshot(active=(4002,), boot=OTHER_BOOT)
        self.tick()
        self.assertEqual(len(self.messages), 2)
        self.assertIn("Reminder E4002", self.messages[-1])
        self.tick()
        self.assertEqual(len(self.messages), 2)

    def test_return_before_deferred_resolution_continues_open_alert(self):
        self.worker = self.make_monitor(max_per_hour=1)
        self.snapshot(active=(4003,))
        self.tick()
        self.snapshot(active=(), observed=[4003])
        self.tick()  # Resolution waits for a delivery slot.
        self.snapshot(active=(4003,), boot=OTHER_BOOT)
        self.tick(3600)
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.sql("SELECT * FROM work", state=True), [])
        self.snapshot(active=(4003,), boot=OTHER_BOOT)
        self.tick(82800)
        self.assertIn("Reminder E4003", self.messages[-1])
        self.assertEqual(len(self.messages), 2)

    def test_backoff_is_persistent_bounded_and_eventually_delivers(self):
        self.snapshot(active=(4003,))
        failures = []

        def fail(body):
            failures.append(body)
            raise OSError("private endpoint text must not be logged")

        self.worker.sender = fail
        with self.assertLogs("mileage-monitor", "WARNING") as logs:
            self.tick()
        self.assertNotIn("private endpoint", str(logs.output))
        self.tick(59)
        self.assertEqual(len(failures), 1)
        self.worker = self.make_monitor()
        self.tick()
        self.assertEqual(self.messages, [])
        self.tick(1)
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.sql("SELECT attempts,next_attempt FROM retry", state=True), [(0, 0)])
        self.snapshot(active=(4003,), device="tracker-2")
        self.worker.sender = fail
        for delay in (60, 120, 240, 480, 960, 1920, 3600, 3600):
            with self.assertLogs("mileage-monitor", "WARNING"):
                self.tick()
            next_attempt = self.sql("SELECT next_attempt FROM retry", state=True)[0][0]
            self.assertEqual(next_attempt - self.now, delay)
            self.now = next_attempt

    def test_pending_alert_cancelled_if_unknown_clear_or_stale(self):
        for outcome in ("unknown", "clear", "stale"):
            with self.subTest(outcome=outcome):
                self.snapshot(active=(4003,), device=outcome)
                self.worker.sender = mock.Mock(side_effect=OSError)
                with self.assertLogs("mileage-monitor", "WARNING"):
                    self.tick(3600)
                if outcome == "stale":
                    self.now += 86401
                else:
                    self.snapshot(active=(), observed=[4003] if outcome == "clear" else [], device=outcome)
                self.worker.sender = self.messages.append
                self.tick(3600)
                self.assertEqual(self.messages, [])

    def test_db_unavailable_suspends_pending_delivery(self):
        self.snapshot(active=(4003,))
        self.worker.sender = mock.Mock(side_effect=OSError)
        with self.assertLogs("mileage-monitor", "WARNING"):
            self.tick()
        self.worker.sender = self.messages.append
        with mock.patch.object(monitor, "read_devices", side_effect=PermissionError), self.assertLogs("mileage-monitor", "WARNING"):
            self.assertFalse(self.tick(60))
        self.assertEqual(self.messages, [])
        self.tick()
        self.assertEqual(len(self.messages), 1)

    def test_silence_threshold_boundary_reminder_and_contact_resolution(self):
        self.store.refresh_contact("tracker-1")  # A GPX-only device has no snapshot.
        stamp = datetime.fromtimestamp(self.now - 7 * 86400 + 1, timezone.utc).isoformat()
        self.sql("UPDATE devices SET last_contact_at=?", (stamp,))
        self.tick()
        self.assertEqual(self.messages, [])
        self.tick(1)
        self.assertIn("No contact for 7 days", self.messages[-1])
        self.assertIn("E7001", self.messages[-1])  # warning bypasses error filter and hold
        self.worker = self.make_monitor()
        self.tick(86399)
        self.assertEqual(len(self.messages), 1)
        self.tick(1)
        self.assertIn("Reminder", self.messages[-1])
        stamp = datetime.fromtimestamp(self.now, timezone.utc).isoformat()
        self.sql("UPDATE devices SET last_contact_at=?", (stamp,))
        self.tick()
        self.assertIn("Resolved E7001", self.messages[-1])
        self.tick()
        self.assertEqual(len(self.messages), 3)

    def test_clock_reversal_does_not_resolve_silence(self):
        self.snapshot(active=(), at=self.now - 7 * 86400)
        self.tick()
        self.tick(-86400)
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.sql("SELECT state FROM episodes WHERE code=7001", state=True), [("unknown",)])

    def test_current_catalog_controls_severity_and_notify(self):
        # Stored severity can be obsolete; the monitor does not query events.
        self.snapshot(active=(4002,))
        self.sql("INSERT INTO events VALUES (?,?,?,?,?,?,?,?)",
                 ("tracker-1", BOOT, 1, 4002, "critical", None,
                  "2026-01-01T00:00:00+00:00", "{}"))
        self.catalog = copy.deepcopy(self.catalog)
        definition = next(row for row in self.catalog["codes"] if row["code"] == 4002)
        definition["severity"] = "warning"
        self.worker = self.make_monitor(hold_seconds=0)
        self.tick()
        self.assertEqual(self.messages, [])
        definition["severity"] = "critical"
        definition["notify"] = False
        self.worker = self.make_monitor()
        self.tick()
        self.assertEqual(self.messages, [])
        definition["notify"] = True
        self.worker = self.make_monitor()
        self.tick()
        self.assertEqual(len(self.messages), 1)

    def test_unknown_codes_and_ineligible_warnings_do_not_alert(self):
        self.worker = self.make_monitor(min_severity="info", hold_seconds=0)
        self.snapshot(active=(6999, 3002, 2004, 2005))
        self.tick()
        self.assertEqual(self.messages, [])

    def test_code_unknown_to_new_catalog_cannot_generate_a_resolution(self):
        self.snapshot(active=(4003,))
        self.tick()
        self.snapshot(active=(), observed=[4003])
        self.catalog = copy.deepcopy(self.catalog)
        self.catalog["codes"] = [row for row in self.catalog["codes"] if row["code"] != 4003]
        self.worker = self.make_monitor()
        self.tick()
        self.assertEqual(len(self.messages), 1)

    def test_link_free_minimal_message(self):
        self.snapshot(active=(4003,))
        self.tick()
        text = self.messages[0]
        definition = self.worker.codes[4003]
        for expected in ("E4003", definition["title"], definition["recovery"], "tracker-1"):
            self.assertIn(expected, text)
        for forbidden in ("http", "uptime", "boot_id", "2026-", "context"):
            self.assertNotIn(forbidden, text)

    def test_events_db_bytes_mtime_and_sidecars_unchanged(self):
        self.snapshot(active=(4003,))
        before = (self.store.path.read_bytes(), self.store.path.stat().st_mtime_ns)
        self.tick()
        self.worker = self.make_monitor()
        self.tick(60)
        self.assertEqual(before, (self.store.path.read_bytes(), self.store.path.stat().st_mtime_ns))
        for suffix in ("-journal", "-wal", "-shm"):
            self.assertFalse(Path(str(self.store.path) + suffix).exists())

    def test_state_cannot_use_events_path_or_unknown_version(self):
        with self.assertRaisesRegex(ValueError, "separate"):
            monitor.Monitor(monitor.Config(enabled=True), self.store.path, self.store.path)
        self.sql("UPDATE schema_version SET version=999", state=True)
        with self.assertRaisesRegex(ValueError, "unsupported monitor"):
            self.make_monitor()

    def test_failed_first_state_initialization_rolls_back_and_can_restart(self):
        state = self.root / "interrupted.db"
        with mock.patch.object(monitor, "SCHEMA", monitor.SCHEMA + "INVALID SQL;"):
            with self.assertRaises(sqlite3.OperationalError):
                monitor.Monitor(monitor.Config(enabled=True), self.store.path, state)
        with closing(sqlite3.connect(state)) as db:
            self.assertEqual(db.execute("SELECT name FROM sqlite_master").fetchall(), [])
        worker = monitor.Monitor(monitor.Config(enabled=True), self.store.path, state)
        self.assertTrue(worker.tick())


class NtfyHTTPTests(unittest.TestCase):
    def test_redirect_cannot_forward_credentials(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(self.path)
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(307)
                self.send_header("Location", "/redirect-target")
                self.end_headers()

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            sender = monitor.NtfySender(monitor.Config(
                url=f"http://127.0.0.1:{server.server_port}/topic", token="test-token"))
            with self.assertRaises(monitor.urllib.error.HTTPError):
                sender("test message")
            self.assertEqual(received, ["/topic"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_real_http_failure_backoff_then_delivery_and_token(self):
        received = []
        status = [503]

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append((self.path, self.headers.get("Authorization"),
                                 self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(status[0])
                self.end_headers()

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as root:
                clock = [1800000000.0]
                config = monitor.Config(enabled=True, notify=True,
                                        url=f"http://127.0.0.1:{server.server_port}/test-topic", token="test-token")
                store = store_module.DiagnosticsStore(root, load_catalog())
                store.accept("tracker-1", {"schema_version": 1, "checkin": {
                    "boot_id": BOOT, "status_sequence": 1, "uptime_ms": 0,
                    "active": [{"code": 4003, "episode_id": BOOT + ":1"}]}, "events": []})
                stamp = datetime.fromtimestamp(clock[0], timezone.utc).isoformat()
                with closing(sqlite3.connect(store.path)) as db:
                    with db:
                        db.execute("UPDATE devices SET last_contact_at=?,snapshot_received_at=?", (stamp, stamp))
                worker = monitor.Monitor(config, store.path, Path(root) / "monitor.db", clock=lambda: clock[0])
                with self.assertLogs("mileage-monitor", "WARNING"):
                    worker.tick()
                worker.tick()
                self.assertEqual(len(received), 1)
                status[0] = 200
                clock[0] += 60
                worker.tick()
                self.assertEqual(len(received), 2)
                self.assertEqual(received[0], received[1])
                self.assertEqual(received[-1][0:2], ("/test-topic", "Bearer test-token"))
                self.assertIn(b"Alert E4003", received[-1][2])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()

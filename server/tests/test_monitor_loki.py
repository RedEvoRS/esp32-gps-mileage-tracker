"""Durable JSON-log push to an external Loki: config, privacy, cursor and HTTP."""

import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
from contextlib import closing
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from test_monitor import BOOT, OTHER_BOOT, monitor, store_module
from common.diagnostics import load_catalog

loki = monitor.loki_module
PUSH_URL = "http://127.0.0.1:3100/loki/api/v1/push"
HTTP = loki.LokiConfig(enabled=True, url=PUSH_URL, bearer_token="loki-test-token", tenant="test-tenant")


class LokiConfigTests(unittest.TestCase):
    def test_off_by_default_and_valid_auth_modes(self):
        self.assertFalse(monitor.Config.from_env({}).loki.enabled)
        self.assertFalse(monitor.Config.from_env({"MONITOR_ENABLED": "true"}).loki.enabled)
        basic = loki.LokiConfig.from_env({"LOKI_PUSH_URL": PUSH_URL,
                                          "LOKI_BASIC_AUTH_USER": "service",
                                          "LOKI_BASIC_AUTH_PASSWORD": "private-password",
                                          "LOKI_TENANT_ID": "org-a"})
        self.assertEqual(basic.headers()["Authorization"], "Basic c2VydmljZTpwcml2YXRlLXBhc3N3b3Jk")
        self.assertEqual(basic.headers()["X-Scope-OrgID"], "org-a")
        bearer = loki.LokiConfig.from_env({"LOKI_PUSH_URL": "https://logs.example.net/loki/api/v1/push",
                                           "LOKI_BEARER_TOKEN": "abc123"})
        self.assertEqual(bearer.headers()["Authorization"], "Bearer abc123")
        self.assertEqual(monitor.Config.from_env({"MONITOR_ENABLED": "true",
            "LOKI_PUSH_URL": PUSH_URL}).loki.enabled, True)

    def test_invalid_url_and_ambiguous_or_incomplete_auth_fail_without_echo(self):
        cases = [
            {"LOKI_PUSH_URL": "ftp://host/loki/api/v1/push"},
            {"LOKI_PUSH_URL": "http://user:secret@host/loki/api/v1/push"},
            {"LOKI_PUSH_URL": "http://host/loki/api/v1/push?secret=value"},
            {"LOKI_PUSH_URL": "http://host/elsewhere"},
            {"LOKI_PUSH_URL": PUSH_URL, "LOKI_BASIC_AUTH_USER": "only-user"},
            {"LOKI_PUSH_URL": PUSH_URL, "LOKI_BEARER_TOKEN": "bearer-secret",
             "LOKI_BASIC_AUTH_USER": "user", "LOKI_BASIC_AUTH_PASSWORD": "password"},
            {"LOKI_PUSH_URL": PUSH_URL, "LOKI_TENANT_ID": "bad\r\nheader"},
            {"LOKI_PUSH_URL": PUSH_URL, "LOKI_BEARER_TOKEN": "<token>"},
            {"LOKI_BASIC_AUTH_USER": "user", "LOKI_BASIC_AUTH_PASSWORD": "password"},
        ]
        for values in cases:
            with self.subTest(values=values), self.assertRaises(ValueError) as caught:
                loki.LokiConfig.from_env(values)
            for value in ("secret", "private-password", "bearer-secret", "bad\r\nheader"):
                self.assertNotIn(value, str(caught.exception))

    def test_output_requires_monitor_enabled(self):
        with self.assertRaisesRegex(ValueError, "MONITOR_ENABLED"):
            monitor.Config.from_env({"LOKI_PUSH_URL": PUSH_URL})


class LokiLineTests(unittest.TestCase):
    def setUp(self):
        self.catalog = load_catalog()
        self.codes = {row["code"]: row for row in self.catalog["codes"]}

    def test_event_line_uses_catalog_context_and_fixed_labels(self):
        received = "2027-01-15T08:00:00.123456+00:00"
        canonical = json.dumps({"schema_version": 1, "catalog_version": 1,
                                "transition": "raise", "episode_id": BOOT + ":3",
                                "firmware_version": "test-1.0", "boot_id": BOOT,
                                "boot_counter": 1, "sequence": 3, "code": 4005,
                                "uptime_ms": 10, "time_source": "gps",
                                "utc_ms": 1800000000000, "sync_age_ms": 1,
                                "context": {"backend": "sd", "free_bytes": 32,
                                            "ssid": "<ssid>", "latitude": "<coordinates>",
                                            "free_text": "<context>"}})
        row = (7, "tracker-1", BOOT, 3, 4005, received, canonical)
        result = loki.event_log(self.catalog, row)
        self.assertIsNotNone(result)
        (device, severity), stamp, line = result
        body = json.loads(line)
        self.assertEqual((device, severity), ("tracker-1", "critical"))
        self.assertEqual(stamp, loki._iso_timestamp_ns(received))
        self.assertEqual((body["kind"], body["transition"], body["title"]),
                         ("event", "raise", self.codes[4005]["title"]))
        self.assertEqual((body["ctx_backend"], body["ctx_free_bytes"]), ("sd", 32))
        serialized = json.dumps(body)
        for forbidden in ("<ssid>", "latitude", "<coordinates>", "<context>", "ssid"):
            self.assertNotIn(forbidden, serialized)

    def test_damaged_protocol_text_is_skipped(self):
        event = {"schema_version": 1, "catalog_version": 1, "transition": "notice",
                 "episode_id": None, "firmware_version": "test-1.0", "boot_id": BOOT,
                 "boot_counter": 1, "sequence": 3, "code": 1000, "uptime_ms": 1,
                 "time_source": "<context>", "utc_ms": None, "sync_age_ms": None, "context": {}}
        row = (7, "tracker-1", BOOT, 3, 1000, "2027-01-15T08:00:00+00:00", json.dumps(event))
        self.assertIsNone(loki.event_log(self.catalog, row))

    def test_decision_line_is_bounded_to_catalog_and_decision(self):
        result = loki.decision_log(self.codes, (1, 1800000000.25, "tracker-1", 4003, "resolution"))
        self.assertIsNotNone(result)
        labels, stamp, line = result
        body = json.loads(line)
        self.assertEqual(labels, ("tracker-1", "critical"))
        self.assertEqual(stamp, 1800000000250000000)
        self.assertEqual(body["kind"], "alert_decision")
        self.assertEqual(body["decision"], "resolution")
        self.assertIsNone(loki.decision_log(self.codes, (1, 1, "tracker-1", 9999, "alert")))

    def test_condition_change_line_uses_current_catalog_state(self):
        labels, stamp, line = loki.condition_log(
            self.codes, "tracker-1", 4003, "active", 1800000000.25)
        body = json.loads(line)
        self.assertEqual(labels, ("tracker-1", "critical"))
        self.assertEqual(stamp, 1800000000250000000)
        self.assertEqual((body["kind"], body["device_id"], body["code"], body["state"]),
                         ("condition_change", "tracker-1", 4003, "active"))
        self.assertEqual(body["title"], self.codes[4003]["title"])
        unknown = json.loads(loki.condition_log(
            self.codes, "tracker-1", 4003, "unknown", 1800000000.25)[2])
        self.assertEqual(unknown["state"], "unknown")
        self.assertIsNone(loki.condition_log(self.codes, "tracker-1", 4003, "raise", 1))
        self.assertIsNone(loki.condition_log(self.codes, "tracker-1", 9999, "active", 1))

    def test_each_stream_is_sorted_and_keeps_real_timestamps(self):
        items = [(("tracker-1", "critical"), 300, '{"line":"late"}'),
                 (("tracker-1", "critical"), 100, '{"line":"early"}'),
                 (("tracker-1", "critical"), 300, '{"line":"tie"}')]
        stream = json.loads(loki.push_payload(items))["streams"][0]
        self.assertEqual(stream["stream"], {"job": "mileage-tracker", "device_id": "tracker-1",
                                            "severity": "critical"})
        self.assertEqual([int(value[0]) for value in stream["values"]], [100, 300, 300])
        self.assertEqual([json.loads(value[1])["line"] for value in stream["values"]],
                         ["early", "late", "tie"])


class LokiPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = 1800000000.0
        self.catalog = load_catalog()
        self.store = store_module.DiagnosticsStore(self.root / "data", self.catalog)
        self.state = self.root / "state/monitor.db"
        self.revision = 0
        self.sequence = 0
        self.payloads = []
        self.ntfy = []
        self.graylog = []
        self.worker = self.make_monitor()

    def make_monitor(self, *, notify=False, sender=None, graylog=False, loki_sender=None):
        graylog_config = monitor.gelf.GelfConfig(enabled=True, url="http://127.0.0.1:12201/gelf") if graylog else monitor.gelf.GelfConfig()
        config = monitor.Config(enabled=True, notify=notify, hold_seconds=0,
                                graylog=graylog_config, loki=HTTP)
        return monitor.Monitor(config, self.store.path, self.state, clock=lambda: self.now,
                               sender=sender or self.ntfy.append, catalog=self.catalog,
                               gelf_sender=self.capture_gelf if graylog else None,
                               loki_sender=loki_sender or self.capture_loki)

    def capture_loki(self, payload):
        self.payloads.append(json.loads(payload.decode("utf-8")))

    def capture_gelf(self, payloads):
        self.graylog.extend(json.loads(payload.decode("ascii")) for payload in payloads)
        return len(payloads)

    def serve_loki(self, statuses, response_body=b""):
        handler = type("Handler", (LokiReceiver,), {
            "status": 204, "statuses": list(statuses), "received": [],
            "response_body": response_body})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_port}/loki/api/v1/push"
        sender = loki.transport(loki.LokiConfig(enabled=True, url=url, bearer_token="loki-test-token"))
        return handler, sender, url

    def sql(self, sql, parameters=(), *, state=False):
        with closing(sqlite3.connect(self.state if state else self.store.path)) as db:
            with db:
                return db.execute(sql, parameters).fetchall()

    def post(self, *, active=(), observed=None, events=(), boot=BOOT):
        self.revision += 1
        checkin = {"boot_id": boot, "status_sequence": self.revision, "uptime_ms": 100,
                   "active": [{"code": code, "episode_id": boot + ":1"} for code in active]}
        if observed is not None:
            checkin["observed"] = observed
        records = []
        for code, transition, context in events:
            self.sequence += 1
            records.append({"schema_version": 1, "catalog_version": 1, "firmware_version": "test-1.0",
                            "boot_id": boot, "boot_counter": 1, "sequence": self.sequence, "code": code,
                            "transition": transition,
                            "episode_id": None if transition == "notice" else
                            f"{boot}:{self.sequence if transition == 'raise' else 1}",
                            "uptime_ms": 10, "utc_ms": None, "time_source": "unknown", "sync_age_ms": None,
                            "context": context})
        self.store.accept("tracker-1", {"schema_version": 1, "checkin": checkin, "events": records})
        stamp = datetime.fromtimestamp(self.now, timezone.utc).isoformat()
        self.sql("UPDATE devices SET last_contact_at=?,snapshot_received_at=?", (stamp, stamp))

    def tick(self, seconds=0):
        self.now += seconds
        before = sum(len(stream["values"]) for payload in self.payloads for stream in payload["streams"])
        self.assertTrue(self.worker.tick())
        entries = [(stream["stream"], json.loads(line), int(timestamp))
                   for payload in self.payloads for stream in payload["streams"]
                   for timestamp, line in stream["values"]]
        return entries[before:]

    def test_events_forward_once_with_fixed_labels_and_independent_cursor(self):
        self.post(events=[(4005, "raise", {"backend": "sd", "free_bytes": 0}),
                          (1000, "notice", {})])
        entries = self.tick()
        events = [entry for entry in entries if entry[1].get("kind") == "event"]
        conditions = [entry for entry in entries if entry[1].get("kind") == "condition_change"]
        self.assertEqual(len(events), 2)
        self.assertTrue(conditions)
        self.assertEqual({entry[0]["job"] for entry in entries}, {"mileage-tracker"})
        self.assertEqual({frozenset(entry[0]) for entry in entries},
                         {frozenset(("job", "device_id", "severity"))})
        self.assertEqual({entry[1]["kind"] for entry in events}, {"event"})
        self.assertEqual(self.sql("SELECT event_rowid FROM loki_cursor", state=True), [(2,)])
        self.assertEqual(self.sql("SELECT event_rowid FROM gelf_cursor", state=True), [(0,)])
        self.assertEqual(self.tick(60), [])
        self.worker = self.make_monitor()
        self.assertEqual(self.tick(60), [])

    def test_event_backlog_drains_before_condition_lines(self):
        with mock.patch.object(loki, "MAX_PER_TICK", 2):
            self.post(active=(4003,), observed=[4003],
                      events=[(1000, "notice", {}) for _ in range(3)])
            first = self.tick()
            self.assertEqual([entry[1]["kind"] for entry in first], ["event", "event"])
            second = self.tick(60)
            kinds = [entry[1]["kind"] for entry in second]
            self.assertEqual(kinds.count("event"), 1)
            self.assertIn("condition_change", kinds)

    def test_condition_changes_are_independent_of_ntfy_and_deduplicated_across_wakes(self):
        self.post(active=(4003,), observed=[4003])
        first = self.tick()
        active = [entry[1] for entry in first
                  if entry[1].get("kind") == "condition_change" and entry[1].get("code") == 4003]
        self.assertEqual([entry["state"] for entry in active], ["active"])
        self.assertFalse(self.ntfy)

        self.assertEqual(self.tick(60), [])
        self.post(active=(4003,), observed=[4003], boot=OTHER_BOOT)
        repeated = self.tick(60)
        self.assertFalse([entry for _, entry, _ in repeated
                          if entry.get("kind") == "condition_change" and entry.get("code") == 4003])

        self.post(active=(), observed=[4003])
        cleared = self.tick(60)
        changes = [entry[1] for entry in cleared
                   if entry[1].get("kind") == "condition_change" and entry[1].get("code") == 4003]
        self.assertEqual([entry["state"] for entry in changes], ["clear"])
        self.assertFalse(self.ntfy)
        self.assertEqual(self.sql("SELECT state FROM loki_conditions WHERE device='tracker-1' AND code=4003",
                                  state=True), [("clear",)])

    def test_stale_snapshot_emits_unknown_condition_change(self):
        self.post(active=(4003,), observed=[4003])
        self.tick()
        stale_at = datetime.fromtimestamp(
            self.now - monitor.SNAPSHOT_MAX_AGE_SECONDS - 1, timezone.utc).isoformat()
        self.sql("UPDATE devices SET snapshot_received_at=?", (stale_at,))
        unknown = self.tick(60)
        changes = [entry[1] for entry in unknown
                   if entry[1].get("kind") == "condition_change" and entry[1].get("code") == 4003]
        self.assertEqual([entry["state"] for entry in changes], ["unknown"])
        self.assertFalse(self.ntfy)

    def test_silence_and_recovery_condition_changes_are_forwarded_once(self):
        self.post(observed=[])
        silent_at = datetime.fromtimestamp(
            self.now - self.worker.config.stale_seconds - 1, timezone.utc).isoformat()
        self.sql("UPDATE devices SET last_contact_at=?", (silent_at,))
        active = self.tick()
        silence = [entry[1] for entry in active
                   if entry[1].get("kind") == "condition_change" and entry[1].get("code") == 7001]
        self.assertEqual([entry["state"] for entry in silence], ["active"])
        self.assertFalse(self.ntfy)

        self.post(observed=[])
        recovered = self.tick(60)
        recovery = [entry[1] for entry in recovered
                    if entry[1].get("kind") == "condition_change" and entry[1].get("code") == 7001]
        self.assertEqual([entry["state"] for entry in recovery], ["clear"])
        self.assertFalse(self.ntfy)

    def test_successful_alert_and_resolution_are_pushed_as_decisions(self):
        self.worker = self.make_monitor(notify=True)
        self.post(active=(4003,), observed=[4003])
        alert = self.tick()
        decisions = [entry[1] for entry in alert if entry[1].get("kind") == "alert_decision"]
        self.assertEqual([entry["decision"] for entry in decisions], ["alert"])
        self.post(active=(), observed=[4003])
        resolution = self.tick(60)
        decisions = [entry[1] for entry in resolution if entry[1].get("kind") == "alert_decision"]
        self.assertEqual([entry["decision"] for entry in decisions], ["resolution"])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM loki_decisions", state=True), [(0,)])
        self.assertEqual(self.sql("SELECT decision_id FROM loki_cursor", state=True), [(2,)])

    def test_restored_events_db_resets_only_loki_cursor_and_resends(self):
        self.post(events=[(1000, "notice", {})] * 3)
        first = self.tick()
        self.assertEqual(sum(entry[1].get("kind") == "event" for entry in first), 3)
        self.sql("UPDATE gelf_cursor SET event_rowid=77", state=True)
        self.sql("DELETE FROM events WHERE sequence>1")
        with self.assertLogs("mileage-monitor", "WARNING") as logs:
            self.assertEqual(self.tick(60), [])
        self.assertIn("Loki cursor", logs.output[0])
        self.assertEqual(self.sql("SELECT event_rowid FROM loki_cursor", state=True), [(0,)])
        self.assertEqual(self.sql("SELECT event_rowid FROM gelf_cursor", state=True), [(77,)])
        resent = self.tick(60)
        self.assertEqual(sum(entry[1].get("kind") == "event" for entry in resent), 1)

    def test_existing_monitor_state_adds_loki_tables_to_schema_v1(self):
        state = self.root / "legacy.db"
        monitor.Monitor(monitor.Config(enabled=True), self.store.path, state, catalog=self.catalog)
        with closing(sqlite3.connect(state)) as db, db:
            for table in ("loki_cursor", "loki_retry", "loki_decisions", "loki_conditions"):
                db.execute("DROP TABLE " + table)
        monitor.Monitor(monitor.Config(enabled=True, loki=HTTP), self.store.path, state,
                        catalog=self.catalog, loki_sender=self.capture_loki)
        with closing(sqlite3.connect(state)) as db:
            self.assertEqual(db.execute("SELECT event_rowid,decision_id FROM loki_cursor").fetchall(), [(0, 0)])
            self.assertEqual(db.execute("SELECT attempts,next_attempt FROM loki_retry").fetchall(), [(0, 0.0)])
            self.assertIsNotNone(db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='loki_conditions'").fetchone())

    def assert_content_status_skips(self, status):
        handler, sender, url = self.serve_loki([status, 204], b"private rejection details")
        self.worker = self.make_monitor(loki_sender=sender)
        self.post(events=[(1000, "notice", {})])
        self.sql("INSERT INTO loki_decisions(occurred_at,device,code,decision) VALUES (?,?,?,?)",
                 (self.now, "tracker-1", 4003, "alert"), state=True)
        with self.assertLogs("mileage-monitor", "WARNING") as logs:
            self.assertTrue(self.worker.tick())
        text = "\n".join(logs.output)
        self.assertIn(str(status), text)
        self.assertNotIn(url, text)
        self.assertNotIn("private rejection details", text)
        self.assertEqual(self.sql("SELECT event_rowid,decision_id FROM loki_cursor", state=True), [(1, 1)])
        self.assertEqual(self.sql("SELECT state FROM loki_conditions", state=True), [("clear",)])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM loki_decisions", state=True), [(0,)])
        self.assertEqual(self.sql("SELECT attempts,next_attempt FROM loki_retry", state=True), [(0, 0.0)])

        self.post(events=[(1000, "notice", {})])
        self.tick(60)
        self.assertEqual(len(handler.received), 2)
        streams = json.loads(handler.received[1][1])["streams"]
        lines = [json.loads(line) for stream in streams for _, line in stream["values"]]
        self.assertEqual([line["sequence"] for line in lines if line.get("kind") == "event"], [2])
        self.assertEqual(self.sql("SELECT event_rowid FROM loki_cursor", state=True), [(2,)])

    def test_permanent_400_skips_batch_and_later_rows_still_deliver(self):
        self.assert_content_status_skips(400)

    def test_permanent_422_skips_batch_and_later_rows_still_deliver(self):
        self.assert_content_status_skips(422)

    def assert_transient_status_retries(self, status):
        handler, sender, _ = self.serve_loki([status, 204])
        self.worker = self.make_monitor(loki_sender=sender)
        self.post(events=[(1000, "notice", {})])
        self.sql("INSERT INTO loki_decisions(occurred_at,device,code,decision) VALUES (?,?,?,?)",
                 (self.now, "tracker-1", 4003, "alert"), state=True)
        with self.assertLogs("mileage-monitor", "WARNING"):
            self.assertTrue(self.worker.tick())
        self.assertEqual(len(handler.received), 1)
        self.assertEqual(self.sql("SELECT event_rowid,decision_id FROM loki_cursor", state=True), [(0, 0)])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM loki_conditions", state=True), [(0,)])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM loki_decisions", state=True), [(1,)])
        self.assertEqual(self.sql("SELECT attempts,next_attempt FROM loki_retry", state=True),
                         [(1, self.now + 60)])

        self.assertEqual(self.tick(59), [])
        self.assertEqual(len(handler.received), 1)
        self.assertEqual(self.sql("SELECT event_rowid FROM loki_cursor", state=True), [(0,)])
        self.assertEqual(self.tick(1), [])
        self.assertEqual(len(handler.received), 2)
        self.assertEqual(self.sql("SELECT event_rowid,decision_id FROM loki_cursor", state=True), [(1, 1)])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM loki_conditions", state=True), [(1,)])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM loki_decisions", state=True), [(0,)])
        self.assertEqual(self.sql("SELECT attempts,next_attempt FROM loki_retry", state=True), [(0, 0.0)])

    def test_http_401_preserves_cursors(self):
        self.assert_transient_status_retries(401)

    def test_http_403_preserves_cursors(self):
        self.assert_transient_status_retries(403)

    def test_http_404_preserves_cursors(self):
        self.assert_transient_status_retries(404)

    def test_http_405_preserves_cursors(self):
        self.assert_transient_status_retries(405)

    def test_http_407_preserves_cursors(self):
        self.assert_transient_status_retries(407)

    def test_http_413_preserves_cursors(self):
        self.assert_transient_status_retries(413)

    def test_other_4xx_conservatively_preserves_cursors(self):
        self.assert_transient_status_retries(415)

    def test_http_500_backs_off_without_advancing_cursors(self):
        self.assert_transient_status_retries(500)

    def test_http_503_backs_off_without_advancing_cursors(self):
        self.assert_transient_status_retries(503)

    def test_http_408_backs_off_without_advancing_cursors(self):
        self.assert_transient_status_retries(408)

    def test_http_429_backs_off_without_advancing_cursors(self):
        self.assert_transient_status_retries(429)

    def test_configuration_warning_on_each_retry_including_restart(self):
        _, sender, url = self.serve_loki([401, 401, 204, 401], b"private rejection details")
        self.worker = self.make_monitor(loki_sender=sender)
        self.post(events=[(1000, "notice", {})])
        with self.assertLogs("mileage-monitor", "INFO") as logs:
            self.worker.tick()
            self.assertEqual(len(logs.output), 1)
            self.tick(59)  # No attempt, no repeated warning.
            self.assertEqual(len(logs.output), 1)
            self.worker = self.make_monitor(loki_sender=sender)
            self.tick(1)  # A repeated rejection after a restart still warns.
            self.tick(120)  # Success resets the persisted retry backoff.
            self.post(events=[(1000, "notice", {})])
            self.tick(60)
        warnings = [line for line in logs.output if line.startswith("WARNING:")]
        self.assertEqual(len(warnings), 3)
        for line, delay in zip(warnings, (60, 120, 60), strict=True):
            self.assertIn(f"retry in {delay} seconds", line)
            for hint in ("HTTP 401", "token", "tenant", "push URL", "proxy", "without advancing cursors"):
                self.assertIn(hint, line)
        text = "\n".join(logs.output)
        for private in (url, "loki-test-token", "private rejection details"):
            self.assertNotIn(private, text)

    def test_loki_failure_does_not_prevent_ntfy_or_gelf_and_retries(self):
        self.worker = self.make_monitor(notify=True, graylog=True,
                                       loki_sender=mock.Mock(side_effect=OSError("private endpoint")))
        self.post(active=(4003,), observed=[4003], events=[(1000, "notice", {})])
        with self.assertLogs("mileage-monitor", "WARNING") as logs:
            self.worker.tick()
        self.assertNotIn("private endpoint", str(logs.output))
        self.assertEqual(len(self.ntfy), 1)
        self.assertTrue(self.graylog)
        self.assertEqual(self.sql("SELECT attempts FROM loki_retry", state=True), [(1,)])
        self.assertEqual(self.sql("SELECT next_attempt FROM loki_retry", state=True), [(self.now + 60,)])
        self.assertEqual(self.sql("SELECT event_rowid FROM loki_cursor", state=True), [(0,)])

    def test_bounded_backoff_and_resume_after_restart(self):
        calls = []

        def fail(payload):
            calls.append(payload)
            raise OSError("private endpoint")

        self.worker = self.make_monitor(loki_sender=fail)
        self.post(events=[(1000, "notice", {})])
        with self.assertLogs("mileage-monitor", "WARNING"):
            self.worker.tick()
        self.tick(59)
        self.assertEqual(len(calls), 1)
        self.worker = self.make_monitor()
        entries = self.tick(1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(sum(entry[1].get("kind") == "event" for entry in entries), 1)
        self.assertEqual(self.sql("SELECT attempts,next_attempt FROM loki_retry", state=True), [(0, 0.0)])

    def test_events_db_is_never_written(self):
        self.post(events=[(1000, "notice", {})])
        before = (self.store.path.read_bytes(), self.store.path.stat().st_mtime_ns)
        self.tick()
        self.assertEqual(before, (self.store.path.read_bytes(), self.store.path.stat().st_mtime_ns))


class LokiReceiver(BaseHTTPRequestHandler):
    status = 204
    statuses = []
    response_body = b""
    received = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        type(self).received.append((dict(self.headers), body, self.path))
        statuses = type(self).statuses
        status = statuses.pop(0) if statuses else type(self).status
        self.send_response(status)
        if status == 302:
            self.send_header("Location", "http://127.0.0.1:1/elsewhere")
        response_body = type(self).response_body
        self.send_header("Content-Length", str(len(response_body)))
        self.end_headers()
        if response_body:
            self.wfile.write(response_body)

    def log_message(self, *_):
        pass


class LokiTransportTests(unittest.TestCase):
    def serve(self, status):
        handler = type("Handler", (LokiReceiver,), {"status": status, "received": []})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return handler, f"http://127.0.0.1:{server.server_port}/loki/api/v1/push"

    def test_http_posts_loki_payload_with_bearer_and_tenant(self):
        handler, url = self.serve(204)
        config = loki.LokiConfig(enabled=True, url=url, bearer_token="token-value", tenant="org-a")
        loki.transport(config)(b'{"streams":[]}')
        headers, body, path = handler.received[0]
        self.assertEqual(path, "/loki/api/v1/push")
        self.assertEqual(body, b'{"streams":[]}')
        self.assertEqual(headers["Authorization"], "Bearer token-value")
        self.assertEqual(next(value for key, value in headers.items()
                              if key.lower() == "x-scope-orgid"), "org-a")
        self.assertEqual(headers["Content-Type"], "application/json")

    def test_http_error_and_redirect_are_failures_without_following(self):
        for status in (500, 302, 408, 429):
            handler, url = self.serve(status)
            with self.subTest(status=status), self.assertRaises(urllib.error.HTTPError):
                loki.transport(loki.LokiConfig(enabled=True, url=url))(b'{"streams":[]}')
            self.assertEqual(len(handler.received), 1)

    def test_http_400_raises_status_only_permanent_rejection(self):
        handler, url = self.serve(400)
        with self.assertRaises(loki.PermanentRejection) as caught:
            loki.transport(loki.LokiConfig(enabled=True, url=url))(b'{"streams":[]}')
        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(str(caught.exception), "400")
        self.assertEqual(len(handler.received), 1)


if __name__ == "__main__":
    unittest.main()

"""GELF export to an external Graylog (#135): config, payloads, policy, transports."""

import json
import socket
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from test_monitor import BOOT, monitor, store_module  # also puts server/ on sys.path
from common.diagnostics import load_catalog  # noqa: E402

gelf = monitor.gelf
HTTP = gelf.GelfConfig(enabled=True, url="http://127.0.0.1:12201/gelf")


def decode(payload):
    return json.loads(payload.decode("ascii"))


class GelfConfigTests(unittest.TestCase):
    BASE = {"MONITOR_ENABLED": "true", "GRAYLOG_ENABLED": "true"}

    def config(self, **values):
        return monitor.Config.from_env(dict(self.BASE, **values)).graylog

    def test_off_by_default_and_ignored_while_monitor_disabled(self):
        self.assertFalse(monitor.Config.from_env({"MONITOR_ENABLED": "true"}).graylog.enabled)
        self.assertEqual(monitor.Config.from_env({"GRAYLOG_ENABLED": "true",
                                                  "GRAYLOG_GELF_URL": "bad"}), monitor.Config())

    def test_valid_http_https_and_tcp(self):
        self.assertEqual(self.config(GRAYLOG_GELF_URL="http://graylog.example.lan:12201/gelf").url,
                         "http://graylog.example.lan:12201/gelf")
        https = self.config(GRAYLOG_GELF_URL="https://10.0.0.5/gelf", GRAYLOG_GELF_HEADER="X-Token: abc-123")
        self.assertEqual((https.header_name, https.header_value), ("X-Token", "abc-123"))
        tcp = self.config(GRAYLOG_GELF_TCP="graylog.example.lan:12201", GRAYLOG_GELF_TLS="true")
        self.assertEqual((tcp.tcp_host, tcp.tcp_port, tcp.tls, tcp.tls_verify),
                         ("graylog.example.lan", 12201, True, True))
        self.assertEqual(self.config(GRAYLOG_GELF_TCP="[::1]:12201").tcp_host, "::1")

    def test_invalid_settings_fail_without_echoing_values(self):
        cases = [
            {}, {"GRAYLOG_GELF_URL": "http://a/gelf", "GRAYLOG_GELF_TCP": "a:1"},
            {"GRAYLOG_GELF_URL": "ftp://host/gelf"}, {"GRAYLOG_GELF_URL": "http://u:secretpw@host/gelf"},
            {"GRAYLOG_GELF_URL": "http://host/gelf?secretq=1"}, {"GRAYLOG_GELF_URL": "http://<host>/gelf"},
            {"GRAYLOG_GELF_URL": "http://host:0/gelf"},
            {"GRAYLOG_GELF_TCP": "hostonly"}, {"GRAYLOG_GELF_TCP": "host:0"}, {"GRAYLOG_GELF_TCP": "host:70000"},
            {"GRAYLOG_GELF_URL": "http://host/gelf", "GRAYLOG_GELF_TLS": "true"},
            {"GRAYLOG_GELF_TCP": "host:12201", "GRAYLOG_GELF_HEADER": "X-A: secretval"},
            {"GRAYLOG_GELF_URL": "http://host/gelf", "GRAYLOG_GELF_HEADER": "no colon secretval"},
            {"GRAYLOG_GELF_URL": "http://host/gelf", "GRAYLOG_GELF_HEADER": "X-A: bad\r\nsecretval"},
            {"GRAYLOG_GELF_URL": "http://host/gelf", "GRAYLOG_GELF_CA_FILE": "/no/such/secretca.pem"},
            {"GRAYLOG_GELF_TCP": "host:12201", "GRAYLOG_GELF_TLS": "yes"},
        ]
        for values in cases:
            with self.subTest(values=values), self.assertRaises(ValueError) as caught:
                self.config(**values)
            self.assertNotIn("secret", str(caught.exception))
        with self.assertRaises(ValueError):
            monitor.Config.from_env({"MONITOR_ENABLED": "true", "GRAYLOG_ENABLED": "1"})


class GelfPayloadTests(unittest.TestCase):
    def setUp(self):
        self.codes = {row["code"]: row for row in load_catalog()["codes"]}

    def fields_valid(self, message):
        for key in message:
            if key.startswith("_"):
                self.assertNotEqual(key, "_id")
                self.assertRegex(key[1:], r"^[A-Za-z0-9_.\-]+$")

    def test_condition_levels_and_fields(self):
        active = decode(gelf.condition_message(self.codes, "tracker-1", 4003, "active", BOOT + ":1", 1800000000.0))
        self.assertEqual((active["version"], active["host"], active["level"]), ("1.1", "mileage-tracker", 2))
        self.assertEqual((active["_kind"], active["_state"], active["_symbol"], active["_severity"]),
                         ("condition", "active", "NO_RECORDING_STORAGE", "critical"))
        self.assertIn("E4003", active["short_message"])
        self.fields_valid(active)
        clear = decode(gelf.condition_message(self.codes, "tracker-1", 4003, "clear", None, 1800000000.0))
        self.assertEqual(clear["level"], 6)
        self.assertNotIn("_episode_id", clear)
        unknown_code = decode(gelf.condition_message(self.codes, "tracker-1", 6999, "active", None, 0))
        self.assertEqual((unknown_code["level"], unknown_code["_severity"]), (5, "unknown"))
        silent = decode(gelf.condition_message(self.codes, "tracker-1", 7001, "active", "silence:x",
                                               10 * 86400, contact=0))
        self.assertEqual((silent["_days_without_contact"], silent["level"]), (10, 4))

    def row(self, event, *, received="2027-01-15T08:00:00+00:00", code=2004):
        return (7, "tracker-1", BOOT, 3, code, received, json.dumps(event))

    def test_event_fields_context_and_device_time(self):
        event = {"transition": "raise", "episode_id": BOOT + ":3", "firmware_version": "test-1.0",
                 "time_source": "gps", "utc_ms": 1800000000000, "sync_age_ms": 5, "uptime_ms": 99,
                 "context": {"temperature_mc": 51000, "flag": True}}
        message = decode(gelf.event_message(self.codes, self.row(event)))
        self.assertEqual((message["_kind"], message["_transition"], message["level"]), ("event", "raise", 4))
        self.assertEqual((message["_ctx_temperature_mc"], message["_ctx_flag"]), (51000, "true"))
        self.assertEqual((message["_device_utc_ms"], message["_time_source"], message["_sequence"]),
                         (1800000000000, "gps", 3))
        self.assertEqual(message["timestamp"],
                         datetime(2027, 1, 15, 8, tzinfo=timezone.utc).timestamp())
        self.fields_valid(message)
        clear = decode(gelf.event_message(self.codes, self.row(dict(event, transition="clear"))))
        self.assertEqual(clear["level"], 6)

    def test_damaged_rows_are_skipped_and_hostile_context_dropped(self):
        for canonical in ("not json", "[]", json.dumps({"transition": "bogus"}),
                          json.dumps({"transition": "raise", "context": []})):
            self.assertIsNone(gelf.event_message(self.codes, (1, "t", BOOT, 1, 2004, "2027-01-01T00:00:00+00:00",
                                                             canonical)))
        self.assertIsNone(gelf.event_message(self.codes, self.row({"transition": "raise"}, received="garbage")))
        hostile = {"transition": "notice", "context": {"bad key": 1, "_id": 2, "free": "two words <b>",
                                                       "ok": "sd", "x" * 40: 1}}
        message = decode(gelf.event_message(self.codes, self.row(hostile)))
        context = {key: value for key, value in message.items() if key.startswith("_ctx_")}
        self.assertEqual(context, {"_ctx__id": 2, "_ctx_ok": "sd"})


class GelfPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = 1800000000.0
        self.catalog = load_catalog()
        self.store = store_module.DiagnosticsStore(self.root / "data", self.catalog)
        self.state = self.root / "state/monitor.db"
        self.sent, self.ntfy = [], []
        self.revision, self.sequence = 0, 0
        self.worker = self.make_monitor()

    def make_monitor(self, notify=False, sender=None):
        config = monitor.Config(enabled=True, notify=notify, hold_seconds=0, graylog=HTTP)
        return monitor.Monitor(config, self.store.path, self.state, clock=lambda: self.now,
                               sender=self.ntfy.append, catalog=self.catalog,
                               gelf_sender=sender or self.capture)

    def capture(self, payloads):
        self.sent.extend(decode(p) for p in payloads)
        return len(payloads)

    def sql(self, sql, parameters=(), *, state=False):
        with closing(sqlite3.connect(self.state if state else self.store.path)) as db:
            with db:
                return db.execute(sql, parameters).fetchall()

    def post(self, active=(), observed=None, events=(), *, boot=BOOT, at=None):
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
        stamp = datetime.fromtimestamp(self.now if at is None else at, timezone.utc).isoformat()
        self.sql("UPDATE devices SET last_contact_at=?,snapshot_received_at=?", (stamp, stamp))

    def tick(self, seconds=0):
        self.now += seconds
        self.sent.clear()
        self.assertTrue(self.worker.tick())
        return self.sent

    def kinds(self, messages):
        return sorted((m["_kind"], m["_code"], m.get("_state", m.get("_transition"))) for m in messages)

    def test_events_forwarded_once_across_restart(self):
        self.post(events=[(4005, "raise", {"backend": "sd", "free_bytes": 0}), (1000, "notice", {})])
        self.assertEqual(self.kinds(self.tick()), [("condition", 7001, "clear"),
                                                   ("event", 1000, "notice"), ("event", 4005, "raise")])
        self.assertEqual(self.tick(60), [])
        self.worker = self.make_monitor()
        self.assertEqual(self.tick(60), [])
        self.post(events=[(4005, "clear", {})])
        self.assertEqual(self.kinds(self.tick(60)), [("event", 4005, "clear")])

    def test_condition_state_changes_not_repeated_per_wake(self):
        self.post(active=(4003,), observed=[4003, 4001])
        self.assertEqual(self.kinds(self.tick()), [("condition", 4001, "clear"), ("condition", 4003, "active"),
                                                   ("condition", 7001, "clear")])
        for hour in range(1, 6):  # each deep-sleep wake has a new boot_id
            self.post(active=(4003,), observed=[4003, 4001], boot=f"{hour:032x}")
            self.assertEqual(self.tick(3600), [])
        self.post(active=(), observed=[4003, 4001])
        self.assertEqual(self.kinds(self.tick(60)), [("condition", 4003, "clear")])
        self.post(active=(4003,), observed=[4003, 4001])
        self.assertEqual(self.kinds(self.tick(60)), [("condition", 4003, "active")])
        # A stale snapshot leaves its conditions unknown (silence stays clear).
        self.assertEqual(self.kinds(self.tick(86401)), [("condition", 4001, "unknown"),
                                                        ("condition", 4003, "unknown")])

    def test_silence_alert_and_contact_resolution(self):
        self.post(active=())
        self.tick()
        silent = self.tick(7 * 86400)
        self.assertEqual(self.kinds(silent), [("condition", 7001, "active")])
        self.assertEqual(silent[0]["_days_without_contact"], 7)
        self.assertEqual(self.tick(86400), [])
        self.post(active=())
        self.assertEqual(self.kinds(self.tick(60)), [("condition", 7001, "clear")])

    def test_graylog_runs_while_ntfy_off_and_ntfy_survives_graylog_failure(self):
        self.post(active=(4003,), observed=[4003])
        self.assertTrue(self.tick())
        self.assertEqual(self.ntfy, [])
        self.worker = self.make_monitor(notify=True, sender=mock.Mock(side_effect=gelf.DeliveryError(0, OSError())))
        self.post(active=(4003,), observed=[4003], events=[(1000, "notice", {})])
        with self.assertLogs("mileage-monitor", "WARNING"):
            self.worker.tick()
        self.assertEqual(len(self.ntfy), 1)  # ntfy alert still delivered

    def test_partial_failure_backoff_and_resume(self):
        self.post(events=[(4005, "raise", {}), (4005, "repeat", {}), (4005, "clear", {})])
        calls = []

        def flaky(payloads):
            calls.append(len(payloads))
            raise gelf.DeliveryError(2, ConnectionResetError())
        self.worker = self.make_monitor(sender=flaky)
        with self.assertLogs("mileage-monitor", "WARNING"):
            self.worker.tick()
        self.assertEqual(calls, [4])  # 7001 baseline + three events; first two accepted
        self.assertEqual(self.sql("SELECT attempts FROM gelf_retry", state=True), [(1,)])
        self.now += 30
        self.worker.tick()
        self.assertEqual(calls, [4])  # still backing off
        self.worker = self.make_monitor()
        self.assertEqual(self.kinds(self.tick(31)), [("event", 4005, "clear"), ("event", 4005, "repeat")])
        self.assertEqual(self.sql("SELECT attempts,next_attempt FROM gelf_retry", state=True), [(0, 0)])

    def test_damaged_event_is_skipped_and_cursor_advances(self):
        self.post(events=[(4005, "raise", {}), (4005, "clear", {})])
        self.sql("UPDATE events SET canonical_json='{broken' WHERE sequence=1")
        with self.assertLogs("mileage-monitor", "WARNING"):
            sent = self.tick()
        self.assertEqual(self.kinds(sent), [("condition", 7001, "clear"), ("event", 4005, "clear")])
        self.assertEqual(self.tick(60), [])

    def test_per_tick_cap_and_restored_events_db_resends(self):
        self.post(events=[(1000, "notice", {})] * 5)
        with mock.patch.object(gelf, "MAX_PER_TICK", 2):
            self.assertEqual(len(self.tick()), 2)
            self.assertEqual(len(self.tick(60)), 2)
            self.assertEqual(len(self.tick(60)), 2)
            self.assertEqual(self.tick(60), [])
        # A restore that leaves fewer events than the cursor starts over.
        self.sql("DELETE FROM events WHERE sequence>2")
        with self.assertLogs("mileage-monitor", "WARNING"):
            self.assertEqual(self.tick(60), [])
        self.assertEqual(len(self.tick(60)), 2)

    def test_events_db_is_never_written(self):
        self.post(active=(4003,), events=[(4005, "raise", {})])
        before = (self.store.path.read_bytes(), self.store.path.stat().st_mtime_ns)
        self.tick()
        self.tick(60)
        self.assertEqual(before, (self.store.path.read_bytes(), self.store.path.stat().st_mtime_ns))
        self.assertFalse(Path(str(self.store.path) + "-journal").exists())

    def test_existing_state_db_gains_gelf_tables(self):
        state = self.root / "legacy.db"
        monitor.Monitor(monitor.Config(enabled=True), self.store.path, state, catalog=self.catalog)
        with closing(sqlite3.connect(state)) as db, db:
            db.execute("DROP TABLE gelf_cursor")
            db.execute("DROP TABLE gelf_retry")
        monitor.Monitor(monitor.Config(enabled=True, graylog=HTTP), self.store.path, state,
                        catalog=self.catalog, gelf_sender=self.capture)
        with closing(sqlite3.connect(state)) as db:
            self.assertEqual(db.execute("SELECT event_rowid FROM gelf_cursor").fetchall(), [(0,)])


class Receiver(BaseHTTPRequestHandler):
    status = 202
    received = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        type(self).received.append((dict(self.headers), body))
        self.send_response(self.status)
        if self.status == 302:
            self.send_header("Location", "http://127.0.0.1:1/elsewhere")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


class GelfTransportTests(unittest.TestCase):
    def serve(self, status):
        handler = type("Handler", (Receiver,), {"status": status, "received": []})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return handler, f"http://127.0.0.1:{server.server_port}/gelf"

    def test_http_posts_each_message_with_optional_header(self):
        handler, url = self.serve(202)
        config = gelf.GelfConfig(enabled=True, url=url, header_name="X-Token", header_value="abc")
        self.assertEqual(gelf.transport(config)([b'{"a":1}', b'{"b":2}']), 2)
        self.assertEqual([body for _, body in handler.received], [b'{"a":1}', b'{"b":2}'])
        self.assertEqual(handler.received[0][0]["X-Token"], "abc")
        self.assertEqual(handler.received[0][0]["Content-Type"], "application/json")

    def test_http_error_and_redirect_are_failures(self):
        for status in (500, 302):
            handler, url = self.serve(status)
            with self.subTest(status=status), self.assertRaises(gelf.DeliveryError) as caught:
                gelf.transport(gelf.GelfConfig(enabled=True, url=url))([b"{}", b"{}"])
            self.assertEqual(caught.exception.delivered, 0)
            self.assertEqual(len(handler.received), 1)

    def test_tcp_frames_are_null_terminated(self):
        listener = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(listener.close)
        chunks = []

        def accept():
            connection, _ = listener.accept()
            with connection:
                while data := connection.recv(4096):
                    chunks.append(data)
        thread = threading.Thread(target=accept, daemon=True)
        thread.start()
        config = gelf.GelfConfig(enabled=True, tcp_host="127.0.0.1", tcp_port=listener.getsockname()[1])
        self.assertEqual(gelf.transport(config)([b'{"a":1}', b'{"b":2}']), 2)
        thread.join(5)
        self.assertEqual(b"".join(chunks), b'{"a":1}\0{"b":2}\0')

    def test_tcp_connection_refused_is_a_failure(self):
        probe = socket.create_server(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        with self.assertRaises(gelf.DeliveryError) as caught:
            gelf.transport(gelf.GelfConfig(enabled=True, tcp_host="127.0.0.1", tcp_port=port))([b"{}"])
        self.assertEqual(caught.exception.delivered, 0)


if __name__ == "__main__":
    unittest.main()

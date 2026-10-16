"""Diagnostics HTTP, transaction, worker concurrency and packaging contracts."""

import importlib.util
import io
import json
import multiprocessing
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock


SERVER = Path(__file__).resolve().parents[1]
APP_PATH = SERVER / "mileage-ingest" / "app.py"
BOOT = "0" * 31 + "1"
OTHER_BOOT = "0" * 31 + "2"
HEADERS = {"Authorization": "Bearer test-token", "X-Device-Id": "tracker-1"}


def load_ingest(root):
    with mock.patch.dict(os.environ, INGEST_AUTH_TOKEN="test-token",
                         INGEST_DATA_ROOT=str(root), DISPLAY_TZ="UTC"):
        spec = importlib.util.spec_from_file_location("ingest_diagnostics", APP_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


def batch(revision=1, sequence=1, boot=BOOT):
    return {
        "schema_version": 1,
        "checkin": {"boot_id": boot, "status_sequence": revision, "uptime_ms": 100,
                    "active": [{"code": 4005, "episode_id": boot + ":1"}],
                    "observed": [4001, 4005]},
        "events": [{"schema_version": 1, "catalog_version": 1, "firmware_version": "test-1",
                    "boot_id": boot, "boot_counter": 1, "sequence": sequence,
                    "code": 4005, "transition": "raise", "episode_id": boot + ":" + str(sequence),
                    "uptime_ms": 50, "utc_ms": None, "time_source": "unknown",
                    "sync_age_ms": None, "context": {"backend": "sd", "free_bytes": 0}}],
    }


def process_worker(root, barrier, queue):
    try:
        ing = load_ingest(root)
        barrier.wait(timeout=20)
        response = ing.app.test_client().post("/v1/device-events", json=batch(), headers=HEADERS)
        queue.put((response.status_code, response.get_json()))
    except Exception as exc:
        queue.put(("exception", type(exc).__name__))


class DiagnosticsIngest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ing = load_ingest(self.root)
        self.client = self.ing.app.test_client()

    def post(self, body=None, headers=None):
        return self.client.post("/v1/device-events", json=batch() if body is None else body,
                                headers=HEADERS if headers is None else headers)

    def rows(self, sql, parameters=()):
        db = sqlite3.connect(self.ing.DIAGNOSTICS_STORE.path)
        try:
            return db.execute(sql, parameters).fetchall()
        finally:
            db.close()

    def stream_post(self, payload, *, length=True, chunked=False, headers=None):
        stream = io.BytesIO(payload)
        overrides = {"wsgi.input": stream}
        if length:
            overrides["CONTENT_LENGTH"] = str(len(payload))
        else:
            overrides["CONTENT_LENGTH"] = None
        if chunked:
            # WSGI servers remove chunk framing before exposing wsgi.input.
            overrides.update(HTTP_TRANSFER_ENCODING="chunked", **{"wsgi.input_terminated": True})
        response = self.client.post("/v1/device-events", environ_overrides=overrides,
                                    headers=HEADERS if headers is None else headers)
        return response, stream.tell()

    def test_auth_rejects_and_drains_with_and_without_length(self):
        for token in (None, "wrong", "caf\u00e9", "Basic test-token"):
            headers = {"X-Device-Id": "tracker-1"}
            if token is not None:
                headers["Authorization"] = token if token.startswith("Basic") else "Bearer " + token
            for length in (False, True):
                with self.subTest(token=token, length=length):
                    response, consumed = self.stream_post(b"x" * 100, length=length, headers=headers)
                    self.assertEqual(response.status_code, 401)
                    self.assertEqual(consumed, 100)
        self.assertFalse(self.ing.DIAGNOSTICS_STORE.path.exists())

    def test_bad_or_missing_device_id(self):
        for value in (None, "UPPER", "sensitive value", "x" * 33, "-tracker"):
            headers = {"Authorization": HEADERS["Authorization"]}
            if value is not None:
                headers["X-Device-Id"] = value
            response = self.post(headers=headers)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.get_json(), {"error": "invalid device_id"})

    def test_streaming_cap_and_draining(self):
        for length, chunked in ((True, False), (False, False), (False, True)):
            with self.subTest(length=length, chunked=chunked):
                payload = b" " * (32768 + 1)
                response, consumed = self.stream_post(payload, length=length, chunked=chunked)
                self.assertEqual(response.status_code, 413)
                self.assertEqual(consumed, len(payload))
        self.assertFalse(self.ing.DIAGNOSTICS_STORE.path.exists())

    def test_drain_is_bounded(self):
        budget = self.ing.MAX_DIAGNOSTICS_DISCARD_BYTES
        for headers, status in ((HEADERS, 413), ({}, 401)):
            response, consumed = self.stream_post(b"x" * (budget + 100), length=False, headers=headers)
            self.assertEqual(response.status_code, status)
            self.assertEqual(consumed, budget)
            response, consumed = self.stream_post(b"x" * (budget + 100), length=True, headers=headers)
            self.assertEqual(response.status_code, status)
            self.assertEqual(consumed, 0)

    def test_exact_cap_and_chunked_heartbeat(self):
        body = batch()
        body["events"] = []
        encoded = json.dumps(body).encode()
        for length, chunked in ((True, False), (False, False), (False, True)):
            response, consumed = self.stream_post(encoded + b" " * (32768 - len(encoded)),
                                                  length=length, chunked=chunked)
            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertEqual(consumed, 32768)
            self.assertEqual(response.get_json(), {"accepted": []})

    def test_malformed_json_and_protocol_errors_never_echo_values(self):
        for raw in (b'{"sensitive":', b'\xff', b'{"schema_version":1,"schema_version":1}'):
            response, _ = self.stream_post(raw)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.get_json(), {"error": "invalid request JSON"})
        for field in ("code", "context", "firmware_version", "utc_ms"):
            body = batch()
            body["events"][0][field] = "sensitive value"
            response = self.post(body)
            self.assertEqual(response.status_code, 400)
            self.assertNotIn("sensitive", response.get_data(as_text=True))
        self.assertFalse(self.ing.DIAGNOSTICS_STORE.path.exists())

    def test_valid_batch_ack_and_server_receipt_are_stored(self):
        body = batch()
        event = body["events"][0]
        event.update(utc_ms=1234, time_source="gps", sync_age_ms=1)
        response = self.post(body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"accepted": [{"boot_id": BOOT, "sequence": 1}]})
        code, severity, utc_ms, receipt, content = self.rows(
            "SELECT code,severity,utc_ms,received_at,canonical_json FROM events")[0]
        self.assertEqual((code, severity, utc_ms), (4005, "critical", 1234))
        self.assertTrue(receipt.endswith("+00:00"))
        self.assertEqual(json.loads(content), event)
        self.assertEqual(len(self.rows("SELECT * FROM boots")), 1)
        self.assertEqual(self.rows("SELECT version FROM schema_version"), [(1,)])

    def test_identical_retry_reacked_without_refreshing_snapshot_or_event(self):
        self.assertEqual(self.post().status_code, 200)
        snapshot = self.rows("SELECT snapshot_received_at FROM devices")
        receipt = self.rows("SELECT received_at FROM events")
        # Re-encode every object with reversed key order and indentation.
        encoded = json.dumps(batch(), sort_keys=True, indent=2).encode()
        response, _ = self.stream_post(encoded)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.rows("SELECT * FROM events")), 1)
        self.assertEqual(self.rows("SELECT snapshot_received_at FROM devices"), snapshot)
        self.assertEqual(self.rows("SELECT received_at FROM events"), receipt)

    def test_event_conflict_rolls_back_entire_batch_and_contact(self):
        self.post()
        devices = self.rows("SELECT * FROM devices")
        boots = self.rows("SELECT * FROM boots")
        body = batch(revision=2)
        conflict = body["events"][0]
        conflict["uptime_ms"] += 1
        body["events"] = [batch(sequence=2)["events"][0], conflict]
        response = self.post(body)
        self.assertEqual(response.status_code, 409)
        self.assertNotIn("accepted", response.get_json())
        self.assertEqual(len(self.rows("SELECT * FROM events")), 1)
        self.assertEqual(self.rows("SELECT * FROM devices"), devices)
        self.assertEqual(self.rows("SELECT * FROM boots"), boots)

    def test_unknown_code_has_no_severity(self):
        body = batch()
        body["events"][0].update(code=6999, context={})
        self.assertEqual(self.post(body).status_code, 200)
        self.assertEqual(self.rows("SELECT code,severity FROM events"), [(6999, None)])

    def test_server_codes_are_rejected_in_all_fields(self):
        for field in ("events", "active", "observed"):
            body = batch()
            if field == "events":
                body["events"][0].update(code=7001, context={})
            elif field == "active":
                body["checkin"]["active"][0]["code"] = 7001
            else:
                body["checkin"]["observed"].append(7001)
            self.assertEqual(self.post(body).status_code, 400)

    def test_lower_revision_stores_history_and_contact_but_cannot_change_health(self):
        self.post(batch(revision=10, boot=OTHER_BOOT))
        before = self.rows("SELECT status_sequence,snapshot_json,snapshot_received_at FROM devices")
        contact = self.rows("SELECT last_contact_at FROM devices")[0][0]
        old = batch(revision=9)
        old["checkin"].update(active=[], observed=[4005])
        self.assertEqual(self.post(old).status_code, 200)
        self.assertEqual(self.rows("SELECT status_sequence,snapshot_json,snapshot_received_at FROM devices"), before)
        self.assertGreaterEqual(self.rows("SELECT last_contact_at FROM devices")[0][0], contact)
        self.assertEqual(len(self.rows("SELECT * FROM events")), 2)
        self.assertEqual(self.rows("SELECT state FROM device_conditions WHERE code=4005"), [("active",)])

    def test_equal_revision_different_snapshot_is_atomic_conflict(self):
        self.post()
        body = batch(sequence=2)
        body["checkin"]["uptime_ms"] += 1
        before = self.rows("SELECT * FROM devices")
        response = self.post(body)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(len(self.rows("SELECT * FROM events")), 1)
        self.assertEqual(self.rows("SELECT * FROM devices"), before)

    def test_new_boot_observed_clear_and_omitted_unknown(self):
        self.post()
        newer = batch(revision=2, boot=OTHER_BOOT)
        newer["events"] = []
        newer["checkin"].update(active=[], observed=[4001])
        self.assertEqual(self.post(newer).status_code, 200)
        self.assertEqual(self.rows("SELECT code,state,episode_id FROM device_conditions ORDER BY code"),
                         [(4001, "clear", None), (4005, "unknown", None)])
        newer["checkin"].update(status_sequence=3, observed=[4001, 4005])
        self.assertEqual(self.post(newer).status_code, 200)
        self.assertEqual(self.rows("SELECT state FROM device_conditions WHERE code=4005"), [("clear",)])

    def test_absent_observed_and_journal_clear_do_not_resolve_health(self):
        self.post()
        newer = batch(revision=2, sequence=2)
        del newer["checkin"]["observed"]
        newer["checkin"]["active"] = []
        newer["events"][0].update(transition="clear", episode_id=BOOT + ":1")
        self.assertEqual(self.post(newer).status_code, 200)
        self.assertEqual(self.rows("SELECT state FROM device_conditions WHERE code=4005"), [("unknown",)])

    def test_devices_have_independent_revisions_and_event_identities(self):
        self.post(batch(revision=10))
        response = self.post(headers=HEADERS | {"X-Device-Id": "tracker-2"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.rows("SELECT * FROM events")), 2)
        self.assertEqual(self.rows("SELECT status_sequence FROM devices ORDER BY device_id"), [(10,), (1,)])

    def test_concurrent_threads_initialize_and_dedupe(self):
        def send(_):
            return self.ing.app.test_client().post("/v1/device-events", json=batch(), headers=HEADERS).status_code
        with ThreadPoolExecutor(max_workers=6) as pool:
            self.assertEqual(list(pool.map(send, range(18))), [200] * 18)
        self.assertEqual(self.rows("PRAGMA integrity_check"), [("ok",)])
        self.assertEqual(len(self.rows("SELECT * FROM events")), 1)

    def test_concurrent_new_events_preserve_high_water(self):
        def send(revision):
            return self.ing.app.test_client().post(
                "/v1/device-events", json=batch(revision=revision, sequence=revision),
                headers=HEADERS).status_code
        with ThreadPoolExecutor(max_workers=6) as pool:
            self.assertEqual(list(pool.map(send, range(1, 19))), [200] * 18)
        self.assertEqual(self.rows("SELECT status_sequence FROM devices"), [(18,)])
        self.assertEqual(self.rows("SELECT count(*) FROM events"), [(18,)])
        self.assertEqual(self.rows("PRAGMA integrity_check"), [("ok",)])

    def test_concurrent_processes_initialize_and_dedupe(self):
        context = multiprocessing.get_context("spawn")
        queue = context.Queue()
        barrier = context.Barrier(4)
        workers = [context.Process(target=process_worker, args=(str(self.root), barrier, queue)) for _ in range(4)]
        try:
            for worker in workers:
                worker.start()
            results = [queue.get(timeout=35) for _ in workers]
            self.assertEqual([status for status, _ in results], [200] * 4, results)
        finally:
            for worker in workers:
                worker.join(timeout=5)
                if worker.is_alive():
                    worker.terminate()
                    worker.join()
            queue.close()
            queue.join_thread()
        self.assertTrue(all(worker.exitcode == 0 for worker in workers))
        self.assertEqual(len(self.rows("SELECT * FROM events")), 1)
        self.assertEqual(self.rows("PRAGMA integrity_check"), [("ok",)])

    def test_commit_failure_returns_500_without_ack_or_partial_data(self):
        # Initialize separately, then fail the actual commit after all SQL ran.
        self.ing.DIAGNOSTICS_STORE.refresh_contact("tracker-1")
        store = self.ing.DIAGNOSTICS_STORE
        class FailingCommit(sqlite3.Connection):
            def commit(self):
                raise sqlite3.OperationalError("synthetic commit failure")
        def connect():
            return sqlite3.connect(store.path, isolation_level=None, factory=FailingCommit)
        before = self.rows("SELECT * FROM devices")
        with mock.patch.object(store, "_connect", side_effect=connect):
            response = self.post()
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("accepted", response.get_json())
        self.assertEqual(self.rows("SELECT * FROM events"), [])
        self.assertEqual(self.rows("SELECT * FROM boots"), [])
        self.assertEqual(self.rows("SELECT * FROM devices"), before)
        self.assertEqual(self.post().status_code, 200)

    def test_full_sync_delete_journal_and_read_only_access(self):
        self.post()
        db = self.ing.DIAGNOSTICS_STORE._connect()
        try:
            self.assertEqual(db.execute("PRAGMA synchronous").fetchone(), (2,))
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone(), ("delete",))
            self.assertEqual(db.execute("PRAGMA busy_timeout").fetchone(), (5000,))
        finally:
            db.close()
        db = sqlite3.connect(self.ing.DIAGNOSTICS_STORE.path.as_uri() + "?mode=ro", uri=True)
        try:
            self.assertEqual(db.execute("SELECT count(*) FROM events").fetchone(), (1,))
        finally:
            db.close()
        self.assertFalse(Path(str(self.ing.DIAGNOSTICS_STORE.path) + "-wal").exists())

    def test_unsupported_database_schema_fails_closed(self):
        self.post()
        db = sqlite3.connect(self.ing.DIAGNOSTICS_STORE.path)
        try:
            db.execute("UPDATE schema_version SET version=999")
            db.commit()
        finally:
            db.close()
        response = self.post(batch(revision=2, sequence=2))
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("accepted", response.get_json())

    def test_gpx_contact_is_header_only_and_best_effort(self):
        raw = b'<gpx creator="legacy-device"><trk><trkseg><trkpt lat="0" lon="0"/></trkseg></trk></gpx>'
        for device in (None, "INVALID", "tracker-1"):
            headers = {"Authorization": HEADERS["Authorization"], "X-Trip-Id": "trip-" + str(device)}
            if device is not None:
                headers["X-Device-Id"] = device
            response = self.client.post("/upload", data=raw, headers=headers)
            self.assertEqual(response.status_code, 200, response.get_json())
            if device != "tracker-1":
                self.assertFalse(self.ing.DIAGNOSTICS_STORE.path.exists())
        self.assertEqual(self.rows("SELECT device_id,status_sequence,snapshot_received_at FROM devices"),
                         [("tracker-1", None, None)])
        self.assertEqual(self.rows("SELECT * FROM boots"), [])
        # Break the real database path rather than mocking the contact helper.
        self.ing.DIAGNOSTICS_STORE.path.unlink()
        self.ing.DIAGNOSTICS_STORE.path.mkdir()
        # The same broken DB also must not prevent app startup for GPX service.
        self.ing = load_ingest(self.root)
        self.client = self.ing.app.test_client()
        response = self.client.post("/upload", data=raw, headers=HEADERS | {"X-Trip-Id": "broken-db"})
        self.assertEqual(response.status_code, 200)

    def test_gpx_refreshes_contact_without_refreshing_snapshot(self):
        self.post()
        snapshot = self.rows("SELECT status_sequence,snapshot_json,snapshot_received_at FROM devices")
        db = sqlite3.connect(self.ing.DIAGNOSTICS_STORE.path)
        try:
            db.execute("UPDATE devices SET last_contact_at='2000-01-01T00:00:00+00:00'")
            db.commit()
        finally:
            db.close()
        raw = b'<gpx><trk><trkseg><trkpt lat="0" lon="0"/></trkseg></trk></gpx>'
        response = self.client.post("/upload", data=raw, headers=HEADERS)
        self.assertEqual(response.status_code, 200)
        self.assertGreater(self.rows("SELECT last_contact_at FROM devices")[0][0], "2000-01-01")
        self.assertEqual(self.rows("SELECT status_sequence,snapshot_json,snapshot_received_at FROM devices"), snapshot)

    def test_failed_gpx_does_not_refresh_contact(self):
        self.assertEqual(self.client.post("/upload", data=b"bad", headers=HEADERS).status_code, 400)
        self.assertFalse(self.ing.DIAGNOSTICS_STORE.path.exists())

    def test_catalog_cached_and_startup_fails_if_missing_or_invalid(self):
        from common.diagnostics import catalog
        for contents in (None, "{}", "not JSON"):
            path = self.root / "missing-catalog.json"
            if contents is not None:
                path.write_text(contents, encoding="utf-8")
            with mock.patch.object(catalog, "CATALOG_PATH", path):
                with self.assertRaisesRegex(RuntimeError, "missing or invalid diagnostics catalog.json"):
                    load_ingest(self.root)
            path.unlink(missing_ok=True)
        with mock.patch.object(catalog, "load_catalog", side_effect=AssertionError("must use startup cache")):
            self.assertEqual(self.post().status_code, 200)


class DiagnosticsPackaging(unittest.TestCase):
    def test_build_contexts_copy_shared_code_and_allowlist_excludes_secrets(self):
        compose = (SERVER / "docker-compose.yml").read_text(encoding="utf-8")
        ignore = (SERVER / ".dockerignore").read_text(encoding="utf-8").splitlines()
        rules = [line for line in ignore if line and not line.startswith("#")]
        self.assertEqual(rules[0], "**")
        allowed = {line[1:] for line in rules[1:] if line.startswith("!")}
        # Docker re-includes descendants when a directory is allowed. Reset
        # each directory's contents before allowing individual runtime files.
        for index, rule in enumerate(rules):
            if rule.startswith("!") and rule.endswith("/"):
                self.assertEqual(rules[index + 1], rule[1:] + "**")
        expected = {"common/__init__.py", "common/diagnostics/__init__.py",
                    "common/diagnostics/catalog.py", "common/diagnostics/protocol.py",
                    "common/diagnostics/catalog.json"}
        self.assertTrue(expected <= allowed)
        for secret in (".env", "viewer.env", "data/", "backups/", "tests/", "common/__pycache__/",
                       "mileage-ingest/.env", "common/diagnostics/private.json"):
            self.assertNotIn(secret, allowed)
        self.assertTrue(all("*" not in item for item in allowed))
        for service in ("mileage-ingest", "mileage-viewer"):
            self.assertIn(f"context: .\n      dockerfile: {service}/Dockerfile", compose)
            dockerfile = (SERVER / service / "Dockerfile").read_text(encoding="utf-8")
            self.assertIn("COPY common/ /app/common/", dockerfile)
            self.assertIn("ENV PYTHONPATH=/app", dockerfile)
            self.assertIn(f"COPY {service}/requirements.txt", dockerfile)
            self.assertIn(f"{service}/app.py", dockerfile)
        self.assertIn("!mileage-ingest/diag_store.py", rules)


if __name__ == "__main__":
    unittest.main()

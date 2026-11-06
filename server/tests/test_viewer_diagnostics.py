"""Read-only health UI, current-catalog interpretation and hostile stored text."""

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

from werkzeug.security import generate_password_hash

from test_round_trip import SERVER, load_service
from common.diagnostics import validate_request


BOOT = "00000000000000000000000000000001"
RECEIPT = "2027-01-01T07:59:00+00:00"


class ConditionGroupParser(HTMLParser):
    """Record condition headings and their disclosure ancestors."""

    def __init__(self, html):
        super().__init__()
        self.parents = []
        self.headings = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag in {"details", "summary", "h3"}:
            element = {"tag": tag, "attrs": dict(attrs), "text": ""}
            if tag == "h3":
                self.headings.append((element, list(self.parents)))
            self.parents.append(element)

    def handle_endtag(self, tag):
        if tag in {"details", "summary", "h3"}:
            self.parents.pop()

    def handle_data(self, data):
        if self.parents and self.parents[-1]["tag"] == "h3":
            self.parents[-1]["text"] += data


class ViewerDiagnostics(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        with patch.dict(os.environ, {
            "INGEST_DATA_ROOT": str(cls.root / "data"),
            "VIEWER_DB_PATH": str(cls.root / "annotations.db"),
            "VIEWER_USERNAME": "driver",
            "VIEWER_PASSWORD_HASH": generate_password_hash("test"),
            "VIEWER_SECRET_KEY": "diagnostics-test-session-key-" * 3,
            "DISPLAY_TZ": "America/Los_Angeles",
        }):
            cls.viewer = load_service("mileage-viewer")
        # Import just the store, never the ingest app.
        spec = importlib.util.spec_from_file_location("diagnostics_fixture_store", SERVER / "mileage-ingest" / "diag_store.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cls.store = module.DiagnosticsStore(cls.viewer.DATA_ROOT, cls.viewer.DIAGNOSTICS_CATALOG)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        if self.store.path.parent.exists():
            for path in self.store.path.parent.iterdir():
                path.unlink()
        self.client = self.viewer.app.test_client()
        with self.client.session_transaction() as session:
            session["user"] = "driver"
            session["pw_tag"] = self.viewer._password_tag()
            session["csrf"] = "test-csrf"

    def fixture(self, count=1):
        events = [{
            "schema_version": 1, "catalog_version": 1, "firmware_version": "test-1.0",
            "boot_id": BOOT, "boot_counter": 1, "sequence": n,
            "code": 4005, "transition": "raise", "episode_id": f"{BOOT}:{n}",
            "uptime_ms": n, "utc_ms": 1798790340000,
            "time_source": "gps", "sync_age_ms": 1200,
            "context": {"backend": "sd", "free_bytes": 0},
        } for n in range(1, count + 1)]
        self.store.accept("tracker-1", {"checkin": {
            "boot_id": BOOT, "status_sequence": 1, "uptime_ms": 1500,
            "firmware_version": "test-1.0", "storage_backend": "sd",
            "pending_events": 7, "dropped_events": 2, "counters": {"5001": 3},
            "active": [{"code": 4005, "episode_id": f"{BOOT}:1"}], "observed": [4001, 4005],
        }, "events": events})
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute("UPDATE devices SET last_contact_at=?, snapshot_received_at=?", (RECEIPT, RECEIPT))
            db.execute("UPDATE events SET received_at=?", (RECEIPT,))

    def page(self, query=""):
        response = self.client.get("/device-health" + query)
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def unavailable(self):
        with self.assertLogs(self.viewer.app.logger, level="WARNING") as logs:
            response = self.client.get("/device-health")
        self.assertEqual(response.status_code, 503)
        self.assertIn("Diagnostics unavailable", response.get_data(as_text=True))
        self.assertNotIn("No diagnostics yet", response.get_data(as_text=True))
        self.assertIn("Diagnostics unavailable", " ".join(logs.output))

    def test_login_required_and_public_health_unchanged(self):
        client = self.viewer.app.test_client()
        self.assertEqual(client.get("/device-health").status_code, 302)
        self.assertEqual(client.get("/health").status_code, 200)
        self.assertIn('href="/device-health"', self.client.get("/").get_data(as_text=True))

    def test_absent_database_is_not_created(self):
        self.assertIn("No diagnostics yet", self.page())
        self.assertFalse(self.store.path.exists())
        self.assertFalse(self.store.path.parent.exists() and list(self.store.path.parent.iterdir()))

    def test_corrupt_database(self):
        self.store.path.parent.mkdir(parents=True, exist_ok=True)
        self.store.path.write_bytes(b"not a sqlite database")
        self.unavailable()

    def test_unsupported_and_missing_schema_version(self):
        self.fixture()
        for version in (2, None):
            with self.subTest(version=version):
                with closing(sqlite3.connect(self.store.path)) as db, db:
                    if version is None:
                        db.execute("DELETE FROM schema_version")
                    else:
                        db.execute("UPDATE schema_version SET version=?", (version,))
                self.unavailable()

    def test_locked_database_has_bounded_timeout(self):
        self.fixture()
        with closing(sqlite3.connect(self.store.path)) as db:
            db.execute("BEGIN EXCLUSIVE")
            try:
                start = time.monotonic()
                self.unavailable()
                elapsed = time.monotonic() - start
                self.assertGreaterEqual(elapsed, 1.8)
                self.assertLess(elapsed, 3.5)
            finally:
                db.rollback()

    def test_never_modifies_database_or_creates_journal(self):
        self.fixture()
        before = self.store.path.read_bytes(), self.store.path.stat().st_mtime_ns
        for query in ("", "?severity=error", "?page=2&code=4005"):
            self.page(query)
        self.assertEqual(before, (self.store.path.read_bytes(), self.store.path.stat().st_mtime_ns))
        self.assertEqual([p.name for p in self.store.path.parent.iterdir()], ["events.db"])

    def test_hot_journal_is_unavailable_and_not_recovered(self):
        self.fixture()
        # Crash a writer after spilling dirty pages; a hot journal requires
        # recovery that mode=ro must refuse, even on a writable host directory.
        script = """import os, sqlite3, sys
db = sqlite3.connect(sys.argv[1])
db.execute('PRAGMA cache_size=1')
db.execute('BEGIN IMMEDIATE')
db.execute('UPDATE devices SET snapshot_json=?', ('x' * 200000,))
os._exit(0)
"""
        subprocess.run([sys.executable, "-c", script, str(self.store.path)], check=True)
        journal = self.store.path.with_name("events.db-journal")
        self.assertTrue(journal.exists())
        before = [(p, p.read_bytes(), p.stat().st_mtime_ns) for p in (self.store.path, journal)]
        self.unavailable()
        for path, contents, mtime in before:
            self.assertEqual(contents, path.read_bytes())
            self.assertEqual(mtime, path.stat().st_mtime_ns)

    def test_current_conditions_and_snapshot_fields(self):
        self.fixture()
        page = self.page()
        for value in ("tracker-1", "test-1.0", "Storage backend", "Pending events", "Dropped events",
                      "E5001 = 3", "Active conditions (1)", "Clear conditions (1)",
                      "Unknown conditions", "Snapshot age measures staleness", "OLED:", "Recovery:"):
            self.assertIn(value, page)
        data = self.viewer.read_diagnostics(1, None, "")
        groups = data["devices"][0]["groups"]
        self.assertEqual([c["code"] for c in groups["active"]], [4005])
        self.assertEqual([c["code"] for c in groups["clear"]], [4001])
        self.assertIn(1001, [c["code"] for c in groups["unknown"]])
        self.assertNotIn(7001, [c["code"] for g in groups.values() for c in g])  # server-raised

    def test_only_active_conditions_start_expanded(self):
        self.fixture()
        groups = self.viewer.read_diagnostics(1, None, "")["devices"][0]["groups"]
        headings = ConditionGroupParser(self.page()).headings
        self.assertEqual([heading["text"] for heading, _ in headings], [
            f"{state.capitalize()} conditions ({len(conditions)})"
            for state, conditions in groups.items()
        ])
        for (state, _), (_, parents) in zip(groups.items(), headings):
            with self.subTest(state=state):
                if state == "active":
                    self.assertEqual(parents, [])
                else:
                    self.assertEqual([parent["tag"] for parent in parents], ["details", "summary"])
                    self.assertNotIn("open", parents[0]["attrs"])

    def test_gpx_only_contact_has_no_snapshot(self):
        self.store.refresh_contact("tracker-1")
        self.assertIn("No health snapshot received", self.page())

    def test_pmu_startup_notice_is_warning_history_without_a_condition(self):
        self.fixture()
        context = dict(clock_count=4, attempt_count=2, recovery_state="recovered",
                       pmu_ready=True, wire_end_failed=False)
        body = {"schema_version": 1, "checkin": {
            "boot_id": BOOT, "status_sequence": 2, "uptime_ms": 1600, "active": []},
            "events": [{"schema_version": 1, "catalog_version": 2, "firmware_version": "test-1.0",
                        "boot_id": BOOT, "boot_counter": 1, "sequence": 2, "code": 2007,
                        "transition": "notice", "episode_id": None, "uptime_ms": 100,
                        "utc_ms": None, "time_source": "unknown", "sync_age_ms": None,
                        "context": context}]}
        self.store.accept("tracker-1", validate_request(body))
        data = self.viewer.read_diagnostics(1, 2007, "warning")
        self.assertEqual(len(data["events"]), 1)
        self.assertEqual(data["events"][0]["context"], context)
        for group in data["devices"][0]["groups"].values():
            self.assertNotIn(2007, [row["code"] for row in group])
        page = self.page("?code=2007&severity=warning")
        for text in ("E2007", "PMU startup needed recovery or retries", "clock_count", "recovered", "notice"):
            self.assertIn(text, page)

    def test_contact_and_snapshot_age_are_independent(self):
        self.fixture()
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute("UPDATE devices SET last_contact_at='2027-01-03T07:58:00+00:00'")
        with patch.object(self.viewer, "datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(2027, 1, 3, 7, 59, tzinfo=timezone.utc)
            page = self.page()
        self.assertIn("1 minutes ago", page)
        self.assertIn("2 days ago", page)

    def test_open_error_is_unavailable(self):
        self.fixture()
        with patch.object(self.viewer.sqlite3, "connect", side_effect=sqlite3.OperationalError("open failed")):
            self.unavailable()

    def test_unknown_code_has_no_severity(self):
        self.fixture()
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute("UPDATE events SET code=6999, severity='critical'")
            db.execute("INSERT INTO device_conditions VALUES ('tracker-1', 6999, 'unknown', NULL)")
        self.assertIn("Unknown E6999", self.page())
        self.assertNotIn('class="diagnostic-event"', self.page("?severity=critical"))
        data = self.viewer.read_diagnostics(1, 6999, "")
        self.assertIsNone(data["events"][0]["definition"]["severity"])

    def test_severity_display_and_filter_use_current_catalog(self):
        self.fixture()
        severity = self.viewer.DIAGNOSTICS_CODES[4005]["severity"]
        for stored in (None, "info"):
            with self.subTest(stored=stored):
                with closing(sqlite3.connect(self.store.path)) as db, db:
                    db.execute("UPDATE events SET severity=?", (stored,))
                self.assertIn('class="diagnostic-event"', self.page(f"?severity={severity}&code=4005"))
                self.assertNotIn('class="diagnostic-event"', self.page("?severity=info"))
                self.assertEqual(self.viewer.read_diagnostics(1, None, "")["events"][0]["definition"]["severity"], severity)

    def test_pagination_boundaries_and_tie_order(self):
        for count in (0, 49, 50, 51, 100, 101):
            with self.subTest(count=count):
                self.setUp()
                self.fixture(count)
                seen = []
                for page in range(1, 4):
                    result = self.viewer.read_diagnostics(page, 4005, "")
                    seen.extend(e["sequence"] for e in result["events"])
                    self.assertEqual(result["has_next"], count > page * 50)
                    html = self.page(f"?page={page}&code=4005")
                    self.assertEqual(html.count('class="diagnostic-event"'), len(result["events"]))
                    if result["has_next"]:
                        self.assertIn(f"page={page+1}&amp;code=4005", html)
                self.assertEqual(seen, list(range(count, 0, -1)))

    def test_history_newest_by_receipt_before_sequence(self):
        self.fixture(2)
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute("UPDATE events SET received_at='2027-01-02T00:00:00+00:00' WHERE sequence=1")
        self.assertEqual([e["sequence"] for e in self.viewer.read_diagnostics(1, None, "")["events"]], [1, 2])

    def test_strict_filter_validation(self):
        for query in ("page=0", "page=-1", "page=01", "page=", "page=1.5", "page=９", "page=" + "9" * 5000,
                      "code=E4005", "code=4005x", "code=999", "code=8000", "code=４００５",
                      "severity=ERROR", "severity=unknown", "code=4005&code=4001", "page=1&page=2", "other=1"):
            with self.subTest(query=query[:50]):
                self.assertEqual(self.client.get("/device-health?" + query).status_code, 400)
        self.fixture()
        self.assertIn("No events in this selection", self.page("?code=6999"))
        self.page("?page=9999999&severity=&code=")

    def test_display_timezone_and_clock_confidence(self):
        self.fixture()
        page = self.page()
        self.assertGreaterEqual(page.count("2026-12-31 23:59 PST"), 4)
        self.assertIn("America/Los_Angeles", page)
        self.assertIn("Source: gps", page)
        self.assertIn("1200 ms", page)
        with closing(sqlite3.connect(self.store.path)) as db, db:
            event = json.loads(db.execute("SELECT canonical_json FROM events").fetchone()[0])
            event.update(utc_ms=None, time_source="unknown", sync_age_ms=None)
            db.execute("UPDATE events SET canonical_json=?", (json.dumps(event),))
        self.assertIn("Device clock unknown", self.page())

    def test_hostile_stored_text_is_autoescaped(self):
        self.fixture()
        hostile = '<script>alert("stored")</script><img src=x onerror=alert(1)>'
        with closing(sqlite3.connect(self.store.path)) as db, db:
            snapshot = json.loads(db.execute("SELECT snapshot_json FROM devices").fetchone()[0])
            snapshot.update(firmware_version=hostile, storage_backend=hostile, counters={hostile: hostile})
            event = json.loads(db.execute("SELECT canonical_json FROM events").fetchone()[0])
            event.update(transition=hostile, episode_id=hostile, context={hostile: hostile}, time_source=hostile)
            db.execute("UPDATE devices SET snapshot_json=?", (json.dumps(snapshot),))
            db.execute("UPDATE events SET canonical_json=?", (json.dumps(event),))
            db.execute("UPDATE device_conditions SET episode_id=? WHERE state='active'", (hostile,))
        page = self.page()
        self.assertNotIn("<script>", page)
        self.assertNotIn("<img", page)
        self.assertGreaterEqual(page.count("&lt;script&gt;alert(&#34;stored&#34;)"), 8)

    def test_summer_timezone_and_unknown_sync_age(self):
        self.fixture()
        stamp = datetime(2026, 7, 1, 7, 15, tzinfo=timezone.utc)
        with closing(sqlite3.connect(self.store.path)) as db, db:
            event = json.loads(db.execute("SELECT canonical_json FROM events").fetchone()[0])
            event.update(utc_ms=int(stamp.timestamp()) * 1000, sync_age_ms=None)
            db.execute("UPDATE events SET canonical_json=?, received_at=?", (json.dumps(event), stamp.isoformat()))
        page = self.page()
        self.assertEqual(page.count("2026-07-01 00:15 PDT"), 2)
        self.assertIn("Sync age: unknown", page)

    def test_malformed_stored_json_is_unavailable(self):
        self.fixture()
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute("UPDATE events SET canonical_json='{' ")
        self.unavailable()

    def test_catalog_failure_prevents_startup(self):
        from common.diagnostics import CatalogError
        with patch("common.diagnostics.load_catalog", side_effect=CatalogError("missing")):
            with self.assertRaisesRegex(RuntimeError, "Cannot start viewer"):
                load_service("mileage-viewer")


if __name__ == "__main__":
    unittest.main()

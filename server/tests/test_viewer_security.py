"""Viewer auth and export regressions; run with unittest discovery."""

import csv
import importlib.util
import io
import json
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from openpyxl import load_workbook
from werkzeug.security import generate_password_hash


SERVER = Path(__file__).resolve().parent.parent


class ViewerSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name)
        cls.data = root / "data"
        cls.data.mkdir()
        cls.db = root / "annotations.db"
        spec = importlib.util.spec_from_file_location(
            "viewer_security_tests", SERVER / "mileage-viewer" / "app.py"
        )
        cls.viewer = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {
            "INGEST_DATA_ROOT": str(cls.data),
            "VIEWER_DB_PATH": str(cls.db),
            "VIEWER_USERNAME": "driver",
            "VIEWER_PASSWORD_HASH": generate_password_hash("test-password"),
            "VIEWER_SECRET_KEY": "test-session-key-" * 4,
            "DISPLAY_TZ": "America/Los_Angeles",
        }):
            spec.loader.exec_module(cls.viewer)
        cls.viewer.app.config["TESTING"] = True
        month = cls.data / "2026-10"
        month.mkdir()
        (month / "drive.json").write_text(json.dumps({
            "trip_id": "drive", "device_id": "=1+2",
            "start_time": "2026-10-05T16:00:00Z",
            "end_time": "2026-10-05T16:10:00Z",
            "distance_miles": 3.5, "duration_seconds": 600,
            "avg_speed_mph": 21, "max_speed_mph": 30,
        }), encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.client = self.viewer.app.test_client()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("DELETE FROM login_attempts")
            conn.execute("DELETE FROM annotations")

    def login_token(self):
        self.client.get("/login")
        with self.client.session_transaction() as sess:
            return sess["csrf"]

    def signed_in(self, csrf=True):
        with self.client.session_transaction() as sess:
            sess["user"] = self.viewer.VIEWER_USERNAME
            sess["pw_tag"] = self.viewer._password_tag()
            if csrf:
                sess["csrf"] = "known-token"

    def test_login_without_prior_session_requires_csrf(self):
        response = self.client.post("/login", data={
            "username": "driver", "password": "test-password",
        })
        self.assertEqual(response.status_code, 400)
        with self.client.session_transaction() as sess:
            self.assertNotIn("user", sess)

    def test_login_rejects_missing_and_unicode_csrf(self):
        self.login_token()
        for token in ("", "untrusted-\u00e9"):
            with self.subTest(token=token):
                response = self.client.post("/login", data={
                    "csrf_token": token, "username": "driver",
                    "password": "test-password",
                })
                self.assertEqual(response.status_code, 400)

    def test_valid_login_and_logout(self):
        token = self.login_token()
        response = self.client.post("/login?next=/export.csv", data={
            "csrf_token": token, "username": "driver", "password": "test-password",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/export.csv")
        with self.client.session_transaction() as sess:
            self.assertEqual(sess["user"], "driver")
            new_token = sess["csrf"]
            self.assertNotEqual(new_token, token)
        self.assertEqual(self.client.post("/logout", data={
            "csrf_token": new_token,
        }).status_code, 302)

    def test_new_password_hash_ends_existing_sessions(self):
        self.client.post("/login", data={
            "csrf_token": self.login_token(), "username": "driver",
            "password": "test-password",
        })
        self.assertEqual(self.client.get("/").status_code, 200)
        # Restarting with a new hash is a new module-level value.
        with patch.object(self.viewer, "VIEWER_PASSWORD_HASH",
                          generate_password_hash("new-password")):
            response = self.client.get("/")
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response.headers["Location"].startswith("/login"))
            with self.client.session_transaction() as sess:
                token = sess["csrf"]
            self.assertEqual(self.client.post("/trip/2026-10/drive/annotate", data={
                "csrf_token": token, "category": "business",
            }).status_code, 401)
            self.assertEqual(self.client.post("/login", data={
                "csrf_token": self.login_token(), "username": "driver",
                "password": "new-password",
            }).status_code, 302)
            self.assertEqual(self.client.get("/").status_code, 200)
        # A session from before sessions carried the tag is logged out too.
        with self.client.session_transaction() as sess:
            del sess["pw_tag"]
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_unicode_username_is_a_failed_login(self):
        response = self.client.post("/login", data={
            "csrf_token": self.login_token(), "username": "driv\u00e9r",
            "password": "test-password",
        })
        self.assertEqual(response.status_code, 401)

    def test_authenticated_posts_require_nonempty_matching_csrf(self):
        self.signed_in(csrf=False)
        self.assertEqual(self.client.post("/logout").status_code, 400)
        self.signed_in()
        self.assertEqual(self.client.post("/logout", data={
            "csrf_token": "untrusted-\u00e9",
        }).status_code, 400)
        with self.client.session_transaction() as sess:
            self.assertIn("user", sess)

    def test_logged_in_post_to_unknown_path_is_404_not_400(self):
        self.signed_in()
        for data in ({}, {"csrf_token": "known-token"}):
            with self.subTest(data=data):
                self.assertEqual(self.client.post("/no-such-page", data=data).status_code, 404)
                # A real page that doesn't take POST is a 405, token or not.
                self.assertEqual(self.client.post("/", data=data).status_code, 405)
        # Real POST routes still need the token.
        self.assertEqual(self.client.post("/trip/2026-10/drive/annotate").status_code, 400)
        # Anonymous callers can't tell missing paths from real ones.
        anonymous = self.viewer.app.test_client()
        for path in ("/no-such-page", "/trip/2026-10/drive/annotate"):
            with self.subTest(path=path):
                self.assertEqual(anonymous.post(path).status_code, 401)

    def test_concurrent_failures_are_all_counted(self):
        with patch.object(self.viewer, "MAX_FAILS", 1000):
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(self.viewer._record_failure, ["test-ip"] * 40))
        with closing(sqlite3.connect(self.db)) as conn, conn:
            fails = conn.execute(
                "SELECT fails FROM login_attempts WHERE ip = 'test-ip'"
            ).fetchone()[0]
        self.assertEqual(fails, 40)

    def test_inflight_failure_cannot_clear_new_lock(self):
        # All these requests may pass the lock check before any finishes
        # password verification. Later failures must preserve the new lock.
        for _ in range(self.viewer.MAX_FAILS + 1):
            self.viewer._record_failure("test-ip")
        with closing(sqlite3.connect(self.db)) as conn, conn:
            locked_until = conn.execute(
                "SELECT locked_until FROM login_attempts WHERE ip = 'test-ip'"
            ).fetchone()[0]
        self.assertIsNotNone(locked_until)
        self.assertGreater(datetime.fromisoformat(locked_until), datetime.now(timezone.utc))
        self.assertGreater(self.viewer._lock_seconds_remaining("test-ip"), 0)

    def test_lockout_blocks_correct_credentials(self):
        token = self.login_token()
        for _ in range(self.viewer.MAX_FAILS):
            self.viewer._record_failure("127.0.0.1")
        self.assertEqual(self.client.post("/login", data={
            "csrf_token": token, "username": "driver", "password": "test-password",
        }).status_code, 429)

    def test_xlsx_keeps_formulas_as_literal_text_and_numbers_numeric(self):
        self.signed_in()
        self.viewer.upsert_annotation("2026-10/drive", "business", "=1+2")
        response = self.client.get("/export.xlsx")
        self.assertEqual(response.status_code, 200)
        workbook = load_workbook(io.BytesIO(response.data), data_only=False)
        try:
            rows = list(workbook.active.iter_rows())
            cells = dict(zip((cell.value for cell in rows[0]), rows[1], strict=True))
            for name in ("device_id", "purpose_note"):
                self.assertEqual(cells[name].value, "=1+2")
                self.assertEqual(cells[name].data_type, "s")
            self.assertEqual(cells["distance_miles"].value, 3.5)
            self.assertEqual(cells["distance_miles"].data_type, "n")
        finally:
            workbook.close()

    def test_csv_escapes_formula_prefixes_and_keeps_numbers(self):
        self.signed_in()
        for note in ("=1+2", "+1+2", "-1+2", "@SUM(1)", "\t=1+2", "\r=1+2", "\n=1+2"):
            with self.subTest(note=note):
                self.viewer.upsert_annotation("2026-10/drive", "business", note)
                response = self.client.get("/export.csv")
                self.assertEqual(response.status_code, 200)
                row = next(csv.DictReader(io.StringIO(response.data.decode())))
                self.assertEqual(row["purpose_note"], "'" + note)
                self.assertEqual(row["distance_miles"], "3.5")
        self.assertEqual(self.viewer._csv_safe(-3.5), -3.5)


if __name__ == "__main__":
    unittest.main()

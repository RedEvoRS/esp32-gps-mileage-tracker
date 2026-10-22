"""Admin removal keeps source recordings while hiding them from the log."""

import csv
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from openpyxl import load_workbook
from werkzeug.security import generate_password_hash

from test_round_trip import load_service


class ViewerRemovalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.month = self.data / "2026-01"
        self.month.mkdir(parents=True)
        self.db = self.root / "annotations.db"
        with patch.dict(os.environ, {
            "INGEST_DATA_ROOT": str(self.data), "VIEWER_DB_PATH": str(self.db),
            "VIEWER_USERNAME": "driver", "VIEWER_PASSWORD_HASH": generate_password_hash("test-password"),
            "VIEWER_SECRET_KEY": "removal-test-session-key-" * 3, "DISPLAY_TZ": "America/Los_Angeles",
            "VIEWER_GEOCODER_URL": "",
        }):
            self.viewer = load_service("mileage-viewer")
        self.admin = self.viewer.app.test_client()
        self.sign_in(self.admin, "admin")
        self.viewer.users.change_user(self.db, "add", "helper", "everyday-password", "driver")
        self.editor = self.viewer.app.test_client()
        self.sign_in(self.editor, "editor")
        self.base = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)

    def sign_in(self, client, role):
        with client.session_transaction() as session:
            session.update(csrf="test-csrf")
            if role == "admin":
                session.update(user="driver", pw_tag=self.viewer._password_tag())
            else:
                user = self.viewer.users.get_user(self.db, "helper")
                session.update(user="helper", kind="editor",
                               pw_tag=self.viewer.users.session_tag(self.viewer.VIEWER_SECRET_KEY, user))

    def add_trip(self, name, start_seconds=0, device="device-a"):
        key = f"2026-01/{name}"
        start = self.base + timedelta(seconds=start_seconds)
        points = []
        for index, offset in enumerate((0, 10, 20)):
            # These synthetic points exist only to exercise merge parsing.
            points.append(
                f'<trkpt lat="0" lon="{index * 0.00001}"><time>'
                f'{(start + timedelta(seconds=offset)).isoformat()}</time><speed>10</speed><hdop>1</hdop></trkpt>'
            )
        (self.month / f"{name}.gpx").write_text(
            "<gpx><trk><trkseg>" + "".join(points) + "</trkseg></trk></gpx>", encoding="utf-8"
        )
        summary = {
            "trip_id": name, "device_id": device, "gpx_file": f"{key}.gpx",
            "start_time": start.isoformat(), "end_time": (start + timedelta(seconds=20)).isoformat(),
            "recorded_until": (start + timedelta(seconds=20)).isoformat(),
            "distance_miles": 1.25, "duration_seconds": 20, "avg_speed_mph": 20,
            "max_speed_mph": 25, "num_points": 3,
        }
        (self.month / f"{name}.json").write_text(json.dumps(summary), encoding="utf-8")
        return key

    def post(self, path, **data):
        return self.admin.post(path, data={"csrf_token": "test-csrf", **data})

    def data_hash(self):
        digest = hashlib.sha256()
        for path in sorted(p for p in self.data.rglob("*") if p.is_file()):
            digest.update(path.relative_to(self.data).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
        return digest.hexdigest()

    def exported_csv(self):
        return list(csv.DictReader(io.StringIO(self.admin.get("/export.csv").data.decode("utf-8"))))

    def test_remove_hides_all_outputs_and_restore_returns_trip_without_touching_data(self):
        key = self.add_trip("drive")
        original_hash = self.data_hash()
        self.assertEqual(self.admin.get("/").status_code, 200)
        self.assertIn("drive", self.admin.get("/").get_data(as_text=True))
        self.assertEqual(len(self.viewer.load_trips()[0]), 1)
        gpx_response = self.admin.get(f"/gpx/{key}")
        self.assertEqual(gpx_response.status_code, 200)
        gpx_response.close()

        removed = self.post(f"/trip/{key}/remove", reason="Duplicate test upload")
        self.assertEqual(removed.status_code, 302)
        with closing(sqlite3.connect(self.db)) as conn:
            row = conn.execute("SELECT trip_key, reason, removed_by FROM trip_removals").fetchone()
        self.assertEqual(row, (key, "Duplicate test upload", "driver"))
        self.assertEqual(self.admin.get(f"/trip/{key}").status_code, 200)
        page = self.admin.get(f"/trip/{key}").get_data(as_text=True)
        self.assertIn("Removed from the log", page)
        self.assertIn("by driver on", page)
        self.assertIn("Duplicate test upload", page)
        self.assertIn("Restore to the log", page)
        for endpoint in ("annotate", "merge", "split", "lookup-place", "remove"):
            self.assertNotIn(f'action="/trip/{key}/{endpoint}', page)
        gpx_response = self.admin.get(f"/gpx/{key}")
        self.assertEqual(gpx_response.status_code, 200)
        gpx_response.close()

        listing = self.admin.get("/").get_data(as_text=True)
        self.assertNotIn(f"/trip/{key}", listing)
        self.assertIn("0.000 mi total", listing)
        self.assertFalse(self.viewer.load_trips()[0])
        self.assertFalse([r for r in self.exported_csv() if r["row_type"] == "trip"])
        workbook = load_workbook(io.BytesIO(self.admin.get("/export.xlsx").data))
        try:
            self.assertEqual(workbook["Mileage"].max_row, 1)
        finally:
            workbook.close()
        removed_page = self.admin.get("/admin/removed").get_data(as_text=True)
        self.assertIn(key, removed_page)
        self.assertIn("Duplicate test upload", removed_page)
        self.assertIn("Restore", removed_page)
        self.assertIn("/admin/removed", self.admin.get("/admin").get_data(as_text=True))

        self.assertEqual(self.editor.get(f"/trip/{key}").status_code, 404)
        self.assertEqual(self.editor.get(f"/gpx/{key}").status_code, 404)
        self.assertEqual(self.editor.get("/admin").status_code, 403)
        self.assertEqual(self.editor.get("/admin/removed").status_code, 403)
        self.assertEqual(self.editor.post(f"/trip/{key}/remove", data={"csrf_token": "test-csrf", "reason": "x"}).status_code, 403)
        self.assertEqual(self.editor.post(f"/trip/{key}/restore", data={"csrf_token": "test-csrf"}).status_code, 403)
        for action, data in (("annotate", {"category": "business"}),
                             ("merge", {"next_trip": "2026-01/next"}),
                             ("split", {"confirm": "yes"}),
                             ("lookup-place", {"endpoint": "start"})):
            response = self.editor.post(f"/trip/{key}/{action}", data={"csrf_token": "test-csrf", **data})
            self.assertEqual(response.status_code, 409, action)

        restored = self.post(f"/trip/{key}/restore")
        self.assertEqual(restored.status_code, 302)
        self.assertIn(key, self.admin.get("/").get_data(as_text=True))
        self.assertEqual([r["trip_id"] for r in self.exported_csv() if r["row_type"] == "trip"], ["drive"])
        self.assertEqual(self.data_hash(), original_hash)

    def test_reason_csrf_duplicate_and_missing_key_validation(self):
        key = self.add_trip("drive")
        for reason in ("", "   ", "x" * 501):
            response = self.post(f"/trip/{key}/remove", reason=reason)
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.admin.post(f"/trip/{key}/remove", data={"reason": "missing token"}).status_code, 400)
        self.assertEqual(self.post("/trip/2026-01/missing/remove", reason="Not found").status_code, 404)
        self.assertEqual(self.post(f"/trip/{key}/restore").status_code, 409)
        self.assertEqual(self.post(f"/trip/{key}/remove", reason="  Needs cleanup  ").status_code, 302)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT reason FROM trip_removals").fetchone()[0], "Needs cleanup")
        self.assertEqual(self.post(f"/trip/{key}/remove", reason="Again").status_code, 409)

    def test_merged_removal_is_whole_and_parts_cannot_be_removed_or_split(self):
        first = self.add_trip("first", 0)
        second = self.add_trip("second", 120)
        original_hash = self.data_hash()
        merged = self.admin.post(f"/trip/{first}/merge", data={
            "csrf_token": "test-csrf", "next_trip": second,
        })
        self.assertEqual(merged.status_code, 302, merged.get_data(as_text=True))
        key = merged.headers["Location"].split("/trip/")[1].split("?")[0]
        self.assertEqual(self.post(f"/trip/{first}/remove", reason="Part is not a complete trip").status_code, 409)
        self.assertEqual(self.post(f"/trip/{key}/remove", reason="Combined duplicate").status_code, 302)
        self.assertEqual(self.post(f"/trip/{key}/split", confirm="yes").status_code, 409)
        self.assertNotIn("Merge with next trip", self.admin.get(f"/trip/{first}").get_data(as_text=True))
        self.assertEqual(self.data_hash(), original_hash)
        self.assertEqual(self.post(f"/trip/{key}/restore").status_code, 302)
        self.assertEqual(self.post(f"/trip/{key}/split", confirm="yes").status_code, 302)
        self.assertEqual(set(t["trip_key"] for t in self.viewer.load_trips()[0]), {first, second})

    def test_removed_trip_is_not_offered_or_accepted_as_next_merge_trip(self):
        first = self.add_trip("first", 0)
        second = self.add_trip("second", 120)
        self.assertEqual(self.post(f"/trip/{second}/remove", reason="Duplicate test upload").status_code, 302)
        page = self.admin.get(f"/trip/{first}").get_data(as_text=True)
        self.assertNotIn("Merge with next trip", page)
        self.assertEqual(self.admin.post(f"/trip/{first}/merge", data={
            "csrf_token": "test-csrf", "next_trip": second,
        }).status_code, 409)

    def test_unreadable_removal_clears_warning_and_unreadable_exports(self):
        key = self.add_trip("broken")
        (self.month / "broken.json").write_text("[not a summary]", encoding="utf-8")
        original_hash = self.data_hash()
        with self.assertLogs(self.viewer.app.logger, "WARNING"):
            page = self.admin.get("/").get_data(as_text=True)
        self.assertIn("file(s) can't be read", page)
        self.assertIn("broken.json", page)
        self.assertEqual([r["trip_id"] for r in self.exported_csv() if r["row_type"] == "unreadable"], ["broken"])
        self.assertEqual(self.post(f"/trip/{key}/remove", reason="Unreadable test file").status_code, 302)
        self.assertNotIn("file(s) can't be read", self.admin.get("/").get_data(as_text=True))
        self.assertFalse([r for r in self.exported_csv() if r["row_type"] == "unreadable"])
        removed_page = self.admin.get("/admin/removed").get_data(as_text=True)
        self.assertIn("2026-01", removed_page)
        self.assertIn("unreadable", removed_page)
        self.assertEqual(self.post(f"/trip/{key}/restore").status_code, 302)
        with self.assertLogs(self.viewer.app.logger, "WARNING"):
            self.assertIn("file(s) can't be read", self.admin.get("/").get_data(as_text=True))
        self.assertEqual(self.data_hash(), original_hash)


if __name__ == "__main__":
    unittest.main()

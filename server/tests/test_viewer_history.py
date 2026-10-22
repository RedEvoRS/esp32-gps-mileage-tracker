"""Atomic trip history, last-edited state, and admin history browsing."""

import csv
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


class ViewerHistoryTests(unittest.TestCase):
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
            "VIEWER_SECRET_KEY": "history-test-session-key-" * 3, "DISPLAY_TZ": "America/Los_Angeles",
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
            # Synthetic points exist only to exercise the viewer's merge parser.
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

    def annotate(self, client, key, category="", note="", destination=""):
        return client.post(f"/trip/{key}/annotate", data={
            "csrf_token": "test-csrf", "category": category,
            "purpose_note": note, "destination": destination,
        })

    def merge(self, key, following):
        response = self.admin.post(f"/trip/{key}/merge", data={
            "csrf_token": "test-csrf", "next_trip": following,
        })
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True))
        return response.headers["Location"].split("/trip/")[1].split("?")[0]

    def csv_rows(self):
        response = self.admin.get("/export.csv")
        return list(csv.DictReader(io.StringIO(response.data.decode("utf-8"))))

    def test_annotations_record_changes_for_both_roles_and_noop_saves_add_nothing(self):
        key = self.add_trip("drive")
        page = self.admin.get(f"/trip/{key}").get_data(as_text=True)
        self.assertNotIn("Last edited by", page)
        self.assertNotIn("Edited before the change history began", page)
        self.assertEqual(self.annotate(self.admin, key).status_code, 302)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM trip_history").fetchone()[0], 0)

        first_at = "2026-01-03T12:00:00+00:00"
        with patch.object(self.viewer.trip_log, "now_utc", return_value=first_at):
            self.assertEqual(self.annotate(self.admin, key, "business", "Example purpose", "Example Road").status_code, 302)
            self.assertEqual(self.annotate(self.admin, key, "business", "Example purpose", "Example Road").status_code, 302)
        with closing(sqlite3.connect(self.db)) as conn:
            rows = conn.execute(
                "SELECT trip_key, actor, actor_role, at, action, details FROM trip_history ORDER BY id"
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][:5], (key, "driver", "admin", first_at, "annotate"))
        self.assertEqual(json.loads(rows[0][5]), {
            "before": {"category": "", "purpose_note": "", "destination": ""},
            "after": {"category": "business", "purpose_note": "Example purpose", "destination": "Example Road"},
        })

        second_at = "2026-01-04T12:00:00+00:00"
        with patch.object(self.viewer.trip_log, "now_utc", return_value=second_at):
            self.assertEqual(self.annotate(self.editor, key, "business", "Helper note", "Example Road").status_code, 302)
            self.assertEqual(self.annotate(self.editor, key, "business", "Helper note", "Example Road").status_code, 302)
        with closing(sqlite3.connect(self.db)) as conn:
            rows = conn.execute("SELECT actor, actor_role, action FROM trip_history ORDER BY id").fetchall()
        self.assertEqual(rows, [("driver", "admin", "annotate"), ("helper", "editor", "annotate")])
        detail = self.editor.get(f"/trip/{key}").get_data(as_text=True)
        self.assertIn("Last edited by helper on 2026-01-04 04:00 PST", detail)

        history = self.admin.get(f"/admin/history?trip={key}").get_data(as_text=True)
        self.assertIn("helper", history)
        self.assertIn("editor", history)
        self.assertIn("Annotation saved", history)
        self.assertIn("Note: Example purpose → Helper note", history)
        self.assertIn(f'href="/trip/{key}"', history)

        response = self.admin.get("/export.csv")
        reader = csv.DictReader(io.StringIO(response.data.decode("utf-8")))
        self.assertEqual(reader.fieldnames[-3:], ["last_edited_by", "last_edited_at", "row_type"])
        rows = list(reader)
        trip_row = next(row for row in rows if row["row_type"] == "trip")
        total_row = next(row for row in rows if row["row_type"] == "total")
        self.assertEqual((trip_row["last_edited_by"], trip_row["last_edited_at"]),
                         ("helper", "2026-01-04T04:00:00-08:00"))
        self.assertEqual((total_row["last_edited_by"], total_row["last_edited_at"]), ("", ""))
        workbook = load_workbook(io.BytesIO(self.admin.get("/export.xlsx").data))
        try:
            headers = [cell.value for cell in workbook["Mileage"][1]]
            cells = dict(zip(headers, workbook["Mileage"][2]))
            self.assertEqual(headers[-2:], ["last_edited_by", "last_edited_at"])
            self.assertEqual((cells["last_edited_by"].value, cells["last_edited_at"].value),
                             ("helper", "2026-01-04T04:00:00-08:00"))
        finally:
            workbook.close()

    def test_pre_history_fallback_fills_readable_and_unreadable_exports(self):
        readable = self.add_trip("old")
        unreadable = self.add_trip("broken", 120)
        (self.month / "broken.json").write_text("[not a summary]", encoding="utf-8")
        old_at = "2025-01-05T12:00:00+00:00"
        with closing(sqlite3.connect(self.db)) as conn, conn:
            for key in (readable, unreadable):
                conn.execute("INSERT INTO annotations (trip_key, category, purpose_note, destination, updated_at) "
                             "VALUES (?, 'business', 'Old note', '', ?)", (key, old_at))
        with self.assertLogs(self.viewer.app.logger, "WARNING"):
            page = self.admin.get(f"/trip/{readable}").get_data(as_text=True)
        self.assertIn("Edited before the change history began (2025-01-05 04:00 PST)", page)
        self.assertNotIn("Last edited by", page)
        with self.assertLogs(self.viewer.app.logger, "WARNING"):
            rows = self.csv_rows()
        old = next(row for row in rows if row["trip_id"] == "old")
        bad = next(row for row in rows if row["row_type"] == "unreadable")
        for row in (old, bad):
            self.assertEqual(row["last_edited_by"], "unknown")
            self.assertEqual(row["last_edited_at"], "2025-01-05T04:00:00-08:00")
        self.assertEqual(bad["distance_miles"], "")
        with self.assertLogs(self.viewer.app.logger, "WARNING"):
            workbook = load_workbook(io.BytesIO(self.admin.get("/export.xlsx").data))
        try:
            self.assertEqual([cell.value for cell in workbook["Unreadable trips"][1]],
                             list(self.viewer.UNREADABLE_COLUMNS))
            values = dict(zip(self.viewer.UNREADABLE_COLUMNS, workbook["Unreadable trips"][2]))
            self.assertEqual((values["last_edited_by"].value, values["last_edited_at"].value),
                             ("unknown", "2025-01-05T04:00:00-08:00"))
        finally:
            workbook.close()

    def test_merge_split_and_live_aliases_share_last_edited_history(self):
        first = self.add_trip("first", 0)
        second = self.add_trip("second", 120)
        third = self.add_trip("third", 240)
        merged_once = self.merge(first, second)
        merged_twice = self.merge(merged_once, third)
        with closing(sqlite3.connect(self.db)) as conn:
            merge_rows = conn.execute(
                "SELECT trip_key, details FROM trip_history WHERE action='merge' ORDER BY id"
            ).fetchall()
            alias = conn.execute("SELECT merge_key FROM trip_merge_aliases WHERE alias_key=?", (merged_once,)).fetchone()
        self.assertEqual([row[0] for row in merge_rows], [merged_once, merged_twice])
        self.assertEqual(json.loads(merge_rows[0][1]), {
            "annotation_from": second,
            "merged_from": [first, second],
            "parts": [first, second],
        })
        self.assertEqual(alias[0], merged_twice)
        response = self.admin.get(f"/trip/{merged_once}")
        self.assertEqual(response.status_code, 302)
        self.assertIn(merged_twice, response.headers["Location"])
        page = self.admin.get(f"/trip/{merged_twice}").get_data(as_text=True)
        self.assertIn("Last edited by driver on", page)
        alias_history = self.admin.get(f"/admin/history?trip={merged_once}").get_data(as_text=True)
        self.assertIn(merged_once, alias_history)
        self.assertIn(merged_twice, alias_history)

        split = self.admin.post(f"/trip/{merged_twice}/split", data={
            "csrf_token": "test-csrf", "confirm": "yes",
        })
        self.assertEqual(split.status_code, 302)
        with closing(sqlite3.connect(self.db)) as conn:
            row = conn.execute(
                "SELECT trip_key, details, actor, actor_role FROM trip_history WHERE action='split'"
            ).fetchone()
        self.assertEqual(row[0], merged_twice)
        self.assertEqual(json.loads(row[1]), {"parts": [first, second, third]})
        self.assertEqual(row[2:], ("driver", "admin"))
        part_page = self.admin.get(f"/trip/{first}").get_data(as_text=True)
        self.assertIn("Last edited by driver on", part_page)
        split_history = self.admin.get(f"/admin/history?trip={first}").get_data(as_text=True)
        self.assertIn("Undo merge", split_history)

    def count_history_queries(self, path):
        """Statements that read trip_history, merge tables or annotations during one request."""
        statements = []
        real_connect = sqlite3.connect

        def traced_connect(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            conn.set_trace_callback(statements.append)
            return conn

        with patch.object(sqlite3, "connect", traced_connect):
            response = self.admin.get(path)
        self.assertEqual(response.status_code, 200)
        response.close()
        return len([sql for sql in statements if sql.lstrip().upper().startswith("SELECT")
                    and any(name in sql for name in ("trip_history", "trip_merge_aliases", "trip_merge_parts"))])

    def test_exports_read_the_history_once_however_many_trips_there_are(self):
        # Merged chains, split history and plain trips: every kind of lookup.
        keys = [self.add_trip(f"t{i:02d}", i * 120) for i in range(24)]
        merged = self.merge(self.merge(keys[0], keys[1]), keys[2])
        self.admin.post(f"/trip/{merged}/split", data={"csrf_token": "test-csrf", "confirm": "yes"})
        self.merge(keys[3], keys[4])
        for key in keys[6:12]:
            self.annotate(self.admin, key, "business", "Test purpose")
        few = self.count_history_queries("/export.csv")
        with patch.object(self.viewer, "_related_history_rows", side_effect=AssertionError("per-trip scan")):
            self.assertGreater(self.count_history_queries("/export.xlsx"), 0)
        # Twelve more trips must not add a single history or merge-table query.
        for i in range(24, 36):
            self.add_trip(f"t{i:02d}", i * 120)
        self.assertEqual(self.count_history_queries("/export.csv"), few)
        self.assertEqual(self.count_history_queries("/export.xlsx"), few)
        self.assertLessEqual(few, 6)

    def test_history_pages_filter_and_reject_invalid_query_parameters(self):
        first = self.add_trip("first")
        other = self.add_trip("other", 120, device="device-b")
        for index in range(51):
            with patch.object(self.viewer.trip_log, "now_utc", return_value=f"2026-01-03T12:00:{index % 60:02d}+00:00"):
                self.assertEqual(self.annotate(self.admin, first, "business", f"Change {index}").status_code, 302)
        with patch.object(self.viewer.trip_log, "now_utc", return_value="2026-01-05T12:00:00+00:00"):
            self.assertEqual(self.annotate(self.admin, other, "personal", "Other trip").status_code, 302)

        page_one = self.admin.get("/admin/history")
        self.assertEqual(page_one.status_code, 200)
        self.assertEqual(page_one.data.count(b'class="history-row"'), 50)
        self.assertIn(b">Older</a>", page_one.data)
        page_two = self.admin.get("/admin/history?page=2")
        self.assertEqual(page_two.data.count(b'class="history-row"'), 2)
        self.assertIn(b">Newer</a>", page_two.data)
        filtered = self.admin.get(f"/admin/history?trip={first}")
        self.assertEqual(filtered.data.count(b'class="history-row"'), 50)
        self.assertNotIn(b"Other trip", filtered.data)
        filtered_two = self.admin.get(f"/admin/history?page=2&trip={first}")
        self.assertEqual(filtered_two.data.count(b'class="history-row"'), 1)
        self.assertIn(f"trip={first}".encode(), filtered_two.data)

        for query in (
            "page=0", "page=0001", "page=10000000", "unknown=1", "page=1&page=2",
            "trip=not-a-trip", f"trip={first}&trip={first}",
        ):
            with self.subTest(query=query):
                self.assertEqual(self.admin.get("/admin/history?" + query).status_code, 400)
        self.assertEqual(self.editor.get("/admin/history").status_code, 403)

    def test_history_writer_rolls_back_both_change_and_row_on_failure(self):
        key = self.add_trip("drive")
        original_record = self.viewer.trip_log.record

        def record_then_fail(conn, trip_key, action, details, *, actor, actor_role):
            original_record(conn, trip_key, action, details, actor=actor, actor_role=actor_role)
            raise RuntimeError("force rollback")

        with patch.object(self.viewer.trip_log, "record", side_effect=record_then_fail):
            with self.assertRaisesRegex(RuntimeError, "force rollback"):
                self.annotate(self.admin, key, "business", "Example purpose")
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM annotations WHERE trip_key=?", (key,)).fetchone())
            self.assertEqual(conn.execute("SELECT count(*) FROM trip_history").fetchone()[0], 0)

        with patch.object(self.viewer.trip_log, "record", side_effect=record_then_fail):
            with self.assertRaisesRegex(RuntimeError, "force rollback"):
                self.admin.post(f"/trip/{key}/remove", data={
                    "csrf_token": "test-csrf", "reason": "Example reason",
                })
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM trip_removals WHERE trip_key=?", (key,)).fetchone())
            self.assertEqual(conn.execute("SELECT count(*) FROM trip_history").fetchone()[0], 0)

        self.assertEqual(self.admin.post(f"/trip/{key}/remove", data={
            "csrf_token": "test-csrf", "reason": "Example reason",
        }).status_code, 302)
        self.assertEqual(self.admin.post(f"/trip/{key}/restore", data={"csrf_token": "test-csrf"}).status_code, 302)
        with closing(sqlite3.connect(self.db)) as conn:
            rows = conn.execute("SELECT action, details FROM trip_history ORDER BY id").fetchall()
        self.assertEqual([row[0] for row in rows], ["remove", "restore"])
        self.assertEqual([json.loads(row[1]) for row in rows], [
            {"reason": "Example reason"}, {"reason": "Example reason"},
        ])

    def test_new_export_columns_keep_formula_prefixes_as_text(self):
        key = self.add_trip("drive")
        with closing(sqlite3.connect(self.db)) as conn, conn:
            self.viewer.trip_log.record(conn, key, "remove", {"reason": "Example reason"},
                                        actor="=1+2", actor_role="admin")
        row = next(row for row in self.csv_rows() if row["row_type"] == "trip")
        self.assertEqual(row["last_edited_by"], "'=1+2")
        workbook = load_workbook(io.BytesIO(self.admin.get("/export.xlsx").data), data_only=False)
        try:
            headers = [cell.value for cell in workbook["Mileage"][1]]
            cells = dict(zip(headers, workbook["Mileage"][2]))
            self.assertEqual(cells["last_edited_by"].value, "=1+2")
            self.assertEqual(cells["last_edited_by"].data_type, "s")
        finally:
            workbook.close()


if __name__ == "__main__":
    unittest.main()

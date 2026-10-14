"""Year selection, totals, legacy annotations and recorded endpoint exports."""

import csv
import io
import json
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from openpyxl import load_workbook
from werkzeug.security import generate_password_hash

from test_round_trip import load_service


class ViewerRecords(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        with patch.dict(os.environ, {
            "INGEST_DATA_ROOT": str(cls.root / "data"),
            "VIEWER_DB_PATH": str(cls.root / "annotations.db"),
            "VIEWER_USERNAME": "driver",
            "VIEWER_PASSWORD_HASH": generate_password_hash("test"),
            "VIEWER_SECRET_KEY": "record-test-session-key-" * 3,
            "DISPLAY_TZ": "America/Los_Angeles",
        }):
            cls.viewer = load_service("mileage-viewer")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.month = self.viewer.DATA_ROOT / "2027-01"
        self.month.mkdir(parents=True, exist_ok=True)
        for path in self.month.iterdir():
            path.unlink()
        with closing(sqlite3.connect(self.viewer.DB_PATH)) as conn, conn:
            conn.execute("DELETE FROM annotations")
        self.client = self.viewer.app.test_client()
        with self.client.session_transaction() as session:
            session["user"] = "driver"
            session["pw_tag"] = self.viewer._password_tag()
            session["csrf"] = "test-csrf"

    def trip(self, name, start, miles=1, category="", destination=""):
        (self.month / f"{name}.json").write_text(json.dumps({
            "trip_id": name, "start_time": start,
            "end_time": "2027-01-01T08:10:00Z", "distance_miles": miles,
        }), encoding="utf-8")
        self.viewer.upsert_annotation(f"2027-01/{name}", category, "", destination)

    def csv_rows(self, query=""):
        response = self.client.get("/export.csv" + query)
        self.assertEqual(response.status_code, 200)
        return list(csv.DictReader(io.StringIO(response.data.decode())))

    def test_year_uses_local_start_not_directory_or_end_time(self):
        self.trip("december", "2027-01-01T07:59:59Z", 1.125, "business")
        self.trip("january", "2027-01-01T08:00:00Z", 2, "personal")
        self.trip("naive", "2027-01-01T07:59:00", .125, "business")
        self.trip("missing", None, 3)
        self.trip("invalid", "bad-time", 4)
        rows = self.csv_rows("?year=2026")
        trips = [r for r in rows if r["row_type"] == "trip"]
        totals = {r["category"]: float(r["distance_miles"]) for r in rows if r["row_type"] == "total"}
        self.assertEqual({r["trip_id"] for r in trips}, {"december", "naive"})
        self.assertEqual(totals["business"], 1.25)
        self.assertEqual(totals["all"], 1.25)
        self.assertEqual(totals["personal"], 0)
        self.assertEqual(len([r for r in self.csv_rows() if r["row_type"] == "trip"]), 5)
        page = self.client.get("/?year=2026").get_data(as_text=True)
        self.assertIn('href="/export.csv?year=2026"', page)
        self.assertIn('href="/export.xlsx?year=2026"', page)
        self.assertIn('value="2026" selected', page)
        self.assertIn("America/Los_Angeles", page)
        self.assertIn("1.250", page)
        self.assertNotIn("/trip/2027-01/january", page)

    def test_year_rejects_invalid_inputs_consistently(self):
        for year in ("0", "0000", "26", "2026x", "10000", "-2026", "２０２６"):
            for route in ("/", "/export.csv", "/export.xlsx"):
                with self.subTest(year=year, route=route):
                    self.assertEqual(self.client.get(route, query_string={"year": year}).status_code, 400)

    def test_edit_and_save_preserve_year_selection(self):
        self.trip("a", "2026-08-01T12:00:00Z")
        page = self.client.get("/?year=2026").get_data(as_text=True)
        self.assertIn('href="/trip/2027-01/a?year=2026"', page)
        detail = self.client.get("/trip/2027-01/a?year=2026").get_data(as_text=True)
        self.assertIn('href="/?year=2026"', detail)
        self.assertIn('action="/trip/2027-01/a/annotate?year=2026"', detail)
        saved = self.client.post("/trip/2027-01/a/annotate?year=2026", data={
            "csrf_token": "test-csrf", "category": "business",
        })
        self.assertEqual(saved.headers["Location"], "/trip/2027-01/a?year=2026")

    def test_csv_and_xlsx_totals_agree_and_empty_year_has_zero_totals(self):
        for name, category, miles in (("a", "business", 1.123), ("b", "business", .002),
                                      ("c", "personal", 2), ("d", "other", 3), ("e", "", 4)):
            self.trip(name, "2026-08-01T12:00:00Z", miles, category)
        for year, expected in ((2026, 10.125), (2025, 0)):
            rows = self.csv_rows(f"?year={year}")
            csv_totals = {r["category"]: float(r["distance_miles"]) for r in rows if r["row_type"] == "total"}
            response = self.client.get(f"/export.xlsx?year={year}")
            self.assertIn(f"mileage-{year}.xlsx", response.headers["Content-Disposition"])
            workbook = load_workbook(io.BytesIO(response.data))
            try:
                totals = {r[2]: r[3] for r in workbook["Totals"].iter_rows(min_row=2, values_only=True)}
                self.assertEqual(totals, csv_totals)
                self.assertEqual(totals["all"], expected)
                self.assertNotIn("commuting", totals)
                self.assertEqual(workbook["Mileage"].max_row, 6 if year == 2026 else 1)
            finally:
                workbook.close()

    def test_destination_round_trip_security_and_raw_files_unchanged(self):
        self.trip("a", "2026-08-01T12:00:00Z")
        raw_path = self.month / "a.json"
        original = raw_path.read_bytes()
        for destination in ('=HYPERLINK("evil")', '<script>alert(1)</script>'):
            response = self.client.post("/trip/2027-01/a/annotate", data={
                "csrf_token": "test-csrf", "category": "business",
                "purpose_note": "visit", "destination": destination,
            })
            self.assertEqual(response.status_code, 302)
            self.assertEqual(self.viewer.get_annotations()["2027-01/a"]["destination"], destination)
            self.assertEqual(self.csv_rows()[0]["destination"], self.viewer._csv_safe(destination))
            page = self.client.get("/trip/2027-01/a").get_data(as_text=True)
            self.assertNotIn("<script>alert(1)</script>", page)
            workbook = load_workbook(io.BytesIO(self.client.get("/export.xlsx").data))
            try:
                cells = dict(zip((c.value for c in workbook.active[1]), workbook.active[2]))
                self.assertEqual(cells["destination"].value, destination)
                self.assertEqual(cells["destination"].data_type, "s")
            finally:
                workbook.close()
        self.assertEqual(raw_path.read_bytes(), original)

    def test_recorded_endpoints_support_legacy_gpx_and_exclude_waypoints(self):
        self.trip("a", "2026-08-01T12:00:00Z")
        raw = b'''<gpx xmlns="http://www.topografix.com/GPX/1/1">
          <wpt lat="89" lon="89"/><trk><trkseg>
          <trkpt lat="0" lon="-122.5"/><trkpt lat="1" lon="-122"/>
          </trkseg><trkseg><trkpt lat="2" lon="-121"/></trkseg></trk>
          <wpt lat="88" lon="88"/></gpx>'''
        path = self.month / "a.gpx"
        path.write_bytes(raw)
        row = self.csv_rows()[0]
        self.assertEqual([float(row[c]) for c in ("start_latitude", "start_longitude", "end_latitude", "end_longitude")], [0, -122.5, 2, -121])
        workbook = load_workbook(io.BytesIO(self.client.get("/export.xlsx").data))
        try:
            cells = dict(zip((c.value for c in workbook.active[1]), workbook.active[2]))
            self.assertEqual(cells["start_longitude"].value, -122.5)
            self.assertEqual(cells["start_longitude"].data_type, "n")
        finally:
            workbook.close()
        self.assertEqual(path.read_bytes(), raw)

    def test_missing_corrupt_invalid_or_entity_gpx_exports_blank_coordinates(self):
        self.trip("a", "2026-08-01T12:00:00Z")
        self.assertEqual(self.csv_rows()[0]["start_latitude"], "")
        for raw in (
            b'<gpx><trk><trkseg><trkpt lat="1" lon="2"/>',
            b'<gpx><trk><trkseg><trkpt lat="nan" lon="2"/></trkseg></trk></gpx>',
            b'<gpx><trk><trkseg><trkpt lat="91" lon="2"/></trkseg></trk></gpx>',
            b'<gpx><trk><trkseg><trkpt lat="1"/></trkseg></trk></gpx>',
            '<!DOCTYPE gpx [<!ENTITY x "1">]><gpx><trk><trkseg><trkpt lat="&x;" lon="2"/></trkseg></trk></gpx>'.encode("utf-16"),
        ):
            with self.subTest(raw=raw):
                (self.month / "a.gpx").write_bytes(raw)
                self.assertEqual(self.csv_rows()[0]["start_latitude"], "")

    def test_damaged_summary_is_skipped_everywhere_not_a_500(self):
        self.trip("good", "2026-08-01T12:00:00Z", 2, "business")
        damaged = {
            "truncated": b'{"trip_id": "truncated", "distance_mi',
            "not_utf8": b'\xff\xfe{"distance_miles": 1}',
            "not_object": b'[1, 2]',
            "text_number": b'{"distance_miles": "abc"}',
            "number_time": b'{"start_time": 12345}',
            "object_text": b'{"device_id": {"a": 1}}',
            "nan_number": b'{"distance_miles": NaN}',
            "huge_number": b'{"distance_miles": 1' + b'0' * 400 + b'}',
            "bool_number": b'{"distance_miles": true}',
        }
        for name, raw in damaged.items():
            with self.subTest(name=name), self.assertLogs(self.viewer.app.logger, "WARNING") as logs:
                path = self.month / f"{name}.json"
                path.write_bytes(raw)
                try:
                    detail = self.client.get(f"/trip/2027-01/{name}")
                    self.assertEqual(detail.status_code, 404)
                    self.assertIn("summary file", detail.get_data(as_text=True))
                    self.assertEqual(self.client.get("/").status_code, 200)
                    rows = self.csv_rows()
                    self.assertEqual([r["trip_id"] for r in rows if r["row_type"] == "trip"], ["good"])
                    self.assertIn({"category": "all", "distance_miles": "2.0"},
                                  [{k: r[k] for k in ("category", "distance_miles")} for r in rows])
                    self.assertEqual(self.client.get("/export.xlsx").status_code, 200)
                finally:
                    path.unlink()
                self.assertIn(f"{name}.json", logs.output[0])
        # Fields the viewer doesn't use are dropped, not type-checked.
        (self.month / "extra.json").write_text(json.dumps({
            "trip_id": "extra", "distance_miles": 1.5, "bbox": None, "start_latitude": {},
        }), encoding="utf-8")
        self.assertEqual(self.viewer.read_summary(self.month / "extra.json"),
                         {"trip_id": "extra", "distance_miles": 1.5})
        self.assertEqual(self.client.get("/export.xlsx").status_code, 200)

    def test_legacy_database_migration_preserves_rows_and_is_concurrent_safe(self):
        path = self.root / "legacy.db"
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("CREATE TABLE annotations (trip_key TEXT PRIMARY KEY, category TEXT, purpose_note TEXT, updated_at TEXT)")
            conn.execute("INSERT INTO annotations VALUES ('old', 'personal', 'original', 'yesterday')")
        with patch.object(self.viewer, "DB_PATH", path):
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda _: self.viewer.init_db(), range(8)))
            self.assertEqual(self.viewer.get_annotations()["old"], {
                "trip_key": "old", "category": "personal", "purpose_note": "original", "destination": "",
            })
        with closing(sqlite3.connect(path)) as conn:
            self.assertEqual(conn.execute("SELECT updated_at FROM annotations").fetchone()[0], "yesterday")


if __name__ == "__main__":
    unittest.main()

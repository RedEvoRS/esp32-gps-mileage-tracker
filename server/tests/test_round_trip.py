"""Exercise ingest -> authenticated viewer -> annotation -> both exports."""

import csv
import importlib.util
import io
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from openpyxl import load_workbook
from werkzeug.security import generate_password_hash


SERVER = Path(__file__).resolve().parents[1]


def load_service(name):
    spec = importlib.util.spec_from_file_location(name, SERVER / name / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.app.config["TESTING"] = True
    return module


class TripRoundTrip(unittest.TestCase):
    def test_untimed_month_boundary_retry_keeps_annotations_and_single_exported_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.dict(os.environ, {
                "INGEST_DATA_ROOT": str(root / "data"),
                "INGEST_AUTH_TOKEN": "round-trip-test-token",
                "VIEWER_DB_PATH": str(root / "annotations.db"),
                "VIEWER_USERNAME": "test-driver",
                "VIEWER_PASSWORD_HASH": generate_password_hash("test-password"),
                "VIEWER_SECRET_KEY": "round-trip-session-key-" * 3,
                "DISPLAY_TZ": "America/Los_Angeles",
            }):
                ingest = load_service("mileage-ingest")
                viewer = load_service("mileage-viewer")

            raw = re.sub(rb"<time>[^<]*</time>", b"", (SERVER / "sample.gpx").read_bytes())
            headers = {"Authorization": "Bearer round-trip-test-token", "X-Device-Id": "test-device"}
            uploader = ingest.app.test_client()
            with patch.object(ingest, "datetime", wraps=datetime) as clock:
                # October 31, 23:59:59 in the configured Pacific timezone.
                clock.now.return_value = datetime(2026, 11, 1, 6, 59, 59, tzinfo=timezone.utc)
                uploaded = uploader.post("/upload", data=raw, headers=headers)
            self.assertEqual(uploaded.status_code, 200)
            path = Path(uploaded.json["stored_as"])
            self.assertEqual(path.parent.name, "2026-10")
            key = path.with_suffix("").as_posix()
            gpx = root / "data" / path
            original_summary = gpx.with_suffix(".json").read_bytes()
            record = json.loads(original_summary)
            self.assertIsNone(record["start_time"])
            self.assertGreater(record["distance_miles"], 0)

            client = viewer.app.test_client()
            client.get("/login")
            with client.session_transaction() as session:
                token = session["csrf"]
            self.assertEqual(client.post("/login", data={
                "csrf_token": token, "username": "test-driver", "password": "test-password",
            }).status_code, 302)
            with client.session_transaction() as session:
                token = session["csrf"]
            self.assertEqual(client.post(f"/trip/{key}/annotate", data={
                "csrf_token": token, "category": "business", "purpose_note": "Test visit",
            }).status_code, 302)

            with patch.object(ingest, "datetime", wraps=datetime) as clock:
                clock.now.return_value = datetime(2026, 11, 1, 7, 0, 1, tzinfo=timezone.utc)
                retry = uploader.post("/upload", data=raw, headers=headers)
            self.assertEqual(retry.status_code, 200)
            self.assertEqual(retry.json["stored_as"], uploaded.json["stored_as"])
            self.assertEqual(gpx.read_bytes(), raw)
            self.assertEqual(gpx.with_suffix(".json").read_bytes(), original_summary)
            self.assertEqual(len(list((root / "data").glob("*/*.json"))), 1)
            self.assertEqual(viewer.get_annotations()[key]["purpose_note"], "Test visit")
            self.assertEqual(client.get(f"/trip/{key}").status_code, 200)

            csv_response = client.get("/export.csv")
            self.assertEqual(csv_response.status_code, 200)
            rows = list(csv.DictReader(io.StringIO(csv_response.data.decode())))
            trips = [row for row in rows if row["row_type"] == "trip"]
            self.assertEqual(len(trips), 1)
            self.assertEqual(trips[0]["category"], "business")
            self.assertEqual(trips[0]["start_time"], "")
            self.assertEqual(float(trips[0]["distance_miles"]), record["distance_miles"])
            total = next(row for row in rows if row["row_type"] == "total" and row["category"] == "all")
            self.assertEqual(float(total["distance_miles"]), record["distance_miles"])

            xlsx_response = client.get("/export.xlsx")
            self.assertEqual(xlsx_response.status_code, 200)
            workbook = load_workbook(io.BytesIO(xlsx_response.data))
            try:
                self.assertEqual(workbook.active.max_row, 2)  # header and one trip
                cells = dict(zip((c.value for c in workbook.active[1]), workbook.active[2]))
                self.assertEqual(cells["distance_miles"].value, record["distance_miles"])
                self.assertEqual(cells["category"].value, "business")
                self.assertEqual(cells["purpose_note"].value, "Test visit")
            finally:
                workbook.close()

    def test_upload_review_annotate_and_export_preserves_raw_track(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.dict(os.environ, {
                "INGEST_DATA_ROOT": str(root / "data"),
                "INGEST_AUTH_TOKEN": "round-trip-test-token",
                "VIEWER_DB_PATH": str(root / "annotations.db"),
                "VIEWER_USERNAME": "test-driver",
                "VIEWER_PASSWORD_HASH": generate_password_hash("test-password"),
                "VIEWER_SECRET_KEY": "round-trip-session-key-" * 3,
                "DISPLAY_TZ": "America/Los_Angeles",
            }):
                ingest = load_service("mileage-ingest")
                viewer = load_service("mileage-viewer")

            raw = (SERVER / "sample.gpx").read_bytes()
            headers = {"Authorization": "Bearer round-trip-test-token",
                       "X-Device-Id": "test-device"}
            uploader = ingest.app.test_client()
            uploaded = uploader.post("/upload", data=raw, headers=headers)
            self.assertEqual(uploaded.status_code, 200)
            path = Path(uploaded.json["stored_as"])
            key = path.with_suffix("").as_posix()
            gpx = root / "data" / path
            summary = gpx.with_suffix(".json")
            original_summary = summary.read_bytes()
            record = json.loads(original_summary)

            client = viewer.app.test_client()
            self.assertEqual(client.get(f"/gpx/{key}").status_code, 302)
            self.assertEqual(client.get("/export.csv").status_code, 302)
            client.get("/login")
            with client.session_transaction() as session:
                token = session["csrf"]
            signed_in = client.post("/login", data={
                "csrf_token": token, "username": "test-driver", "password": "test-password",
            })
            self.assertEqual(signed_in.status_code, 302)
            with client.session_transaction() as session:
                token = session["csrf"]
            self.assertEqual(client.get("/").status_code, 200)
            self.assertEqual(client.get(f"/trip/{key}").status_code, 200)
            with client.get(f"/gpx/{key}") as downloaded:
                self.assertEqual(downloaded.status_code, 200)
                self.assertEqual(downloaded.data, raw)

            annotated = client.post(f"/trip/{key}/annotate", data={
                "csrf_token": token, "category": "business", "purpose_note": "=1+2",
            })
            self.assertEqual(annotated.status_code, 302)
            self.assertEqual(gpx.read_bytes(), raw)
            self.assertEqual(summary.read_bytes(), original_summary)

            csv_response = client.get("/export.csv")
            self.assertEqual(csv_response.status_code, 200)
            row = next(csv.DictReader(io.StringIO(csv_response.data.decode())))
            self.assertEqual(row["category"], "business")
            self.assertEqual(row["purpose_note"], "'=1+2")
            self.assertEqual(float(row["distance_miles"]), record["distance_miles"])
            self.assertTrue(row["start_time"].endswith("-07:00"))

            xlsx_response = client.get("/export.xlsx")
            self.assertEqual(xlsx_response.status_code, 200)
            workbook = load_workbook(io.BytesIO(xlsx_response.data))
            try:
                cells = dict(zip((c.value for c in workbook.active[1]), workbook.active[2]))
                self.assertEqual(cells["purpose_note"].value, "=1+2")
                self.assertEqual(cells["purpose_note"].data_type, "s")
                self.assertEqual(cells["distance_miles"].value, record["distance_miles"])
            finally:
                workbook.close()

            # The firmware can retry after a lost response without changing
            # the stored raw data, receipt metadata, or annotation key.
            self.assertEqual(uploader.post("/upload", data=raw, headers=headers).status_code, 200)
            self.assertEqual(summary.read_bytes(), original_summary)
            self.assertEqual(viewer.get_annotations()[key]["purpose_note"], "=1+2")

            # A damaged summary is flagged, and the repair in DEPLOY_DEBIAN.md
            # (send the stored GPX again, X-Trip-Id = its file name) brings
            # the trip back with its annotation.
            summary.write_bytes(original_summary[:40])
            with self.assertLogs(viewer.app.logger, "WARNING"):
                self.assertIn(f"{key}.json</code>", client.get("/").get_data(as_text=True))
            with self.assertLogs(ingest.app.logger, "WARNING"):
                repaired = uploader.post("/upload", data=gpx.read_bytes(),
                                         headers=headers | {"X-Trip-Id": gpx.stem})
            self.assertEqual(repaired.status_code, 200)
            self.assertEqual(repaired.json["stored_as"], uploaded.json["stored_as"])
            rebuilt = json.loads(summary.read_bytes())
            self.assertEqual({k: rebuilt[k] for k in ("trip_id", "device_id", "distance_miles")},
                             {k: record[k] for k in ("trip_id", "device_id", "distance_miles")})
            page = client.get("/").get_data(as_text=True)
            self.assertNotIn("can't be read", page)
            self.assertIn(f"/trip/{key}", page)
            self.assertEqual(viewer.get_annotations()[key]["purpose_note"], "=1+2")
            self.assertEqual(gpx.read_bytes(), raw)


if __name__ == "__main__":
    unittest.main()

"""Endpoint privacy, cache coordination and real local HTTP integration."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import csv
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from openpyxl import load_workbook
from werkzeug.security import generate_password_hash
from test_round_trip import load_service


class EndpointPlaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.received = []
        cls.status = 200
        cls.body = b'{"display_name":"Invented workshop, Example Road"}'
        cls.drip = 0
        cls.entered = threading.Event()
        cls.release = threading.Event()
        cls.release.set()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                cls.received.append((self.path, dict(self.headers)))
                cls.entered.set()
                cls.release.wait(5)
                self.send_response(cls.status)
                self.send_header("Content-Type", "application/json")
                if cls.status == 302:
                    self.send_header("Location", "/should-not-be-followed")
                if cls.status == 429:
                    self.send_header("Retry-After", "3600")
                self.end_headers()
                try:
                    if cls.drip:
                        for byte in cls.body:
                            self.wfile.write(bytes([byte]))
                            self.wfile.flush()
                            time.sleep(cls.drip)
                    else:
                        self.wfile.write(cls.body)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

            def log_message(self, *args):
                pass

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.provider_url = f"http://127.0.0.1:{cls.server.server_port}/reverse"
        with patch.dict(os.environ, {
            "INGEST_DATA_ROOT": str(cls.root / "data"),
            "VIEWER_DB_PATH": str(cls.root / "annotations.db"),
            "VIEWER_USERNAME": "driver",
            "VIEWER_PASSWORD_HASH": generate_password_hash("pw", "pbkdf2:sha256:1000"),
            "VIEWER_SECRET_KEY": "endpoint-test-key-" * 4,
            "DISPLAY_TZ": "UTC",
            "VIEWER_GEOCODER_URL": cls.provider_url,
            "VIEWER_GEOCODER_USER_AGENT": "MileageTests/1.0",
        }):
            cls.viewer = load_service("mileage-viewer")
        cls.month = cls.viewer.DATA_ROOT / "2026-10"
        cls.month.mkdir(parents=True)
        cls.gpx_path = cls.month / "sample.gpx"
        (cls.month / "sample.json").write_text(json.dumps({
            "trip_id": "sample", "start_time": "2026-10-01T12:00:00Z",
            "end_time": "2026-10-01T12:10:00Z", "distance_miles": 1,
            "duration_seconds": 600, "max_speed_mph": 20, "num_points": 2,
        }), encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.release.set()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.temp.cleanup()

    def setUp(self):
        cls = type(self)
        cls.received.clear()
        cls.status = 200
        cls.drip = 0
        cls.body = b'{"display_name":"Invented workshop, Example Road"}'
        cls.entered.clear()
        cls.release.set()
        with closing(sqlite3.connect(self.viewer.DB_PATH)) as conn, conn:
            for table in ("geocode_cache", "geocode_gate", "annotations"):
                conn.execute(f"DELETE FROM {table}")
        self.viewer.upsert_annotation("2026-10/sample", "business", "Keep note", "Keep destination")
        # Entirely invented coordinates, generated to avoid real location data.
        points = "".join(f'<trkpt lat="{i}" lon="{i + 2}"/>' for i in (1, 2))
        self.good_gpx = f"<gpx><trk><trkseg>{points}</trkseg></trk></gpx>"
        self.gpx_path.write_text(self.good_gpx, encoding="utf-8")
        self.client = self.viewer.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user="driver", pw_tag=self.viewer._password_tag(), csrf="test-csrf")
        self.route = "/trip/2026-10/sample/lookup-place"

    def post(self, endpoint="end", **extra):
        return self.client.post(self.route, data={"csrf_token": "test-csrf", "endpoint": endpoint, **extra})

    def reset_gate(self):
        with closing(sqlite3.connect(self.viewer.DB_PATH)) as conn, conn:
            conn.execute("DELETE FROM geocode_gate")

    def test_only_explicit_lookup_sends_selected_coordinate_and_no_trip_metadata(self):
        for route in ("/", "/trip/2026-10/sample", "/export.csv", "/export.xlsx"):
            self.assertEqual(self.client.get(route).status_code, 200)
        self.assertEqual(self.received, [])
        response = self.post("end", lat=str(99), lon=str(99), provider="https://example.invalid")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["label"], "Invented workshop, Example Road")
        path, headers = self.received[0]
        self.assertEqual(urlsplit(path).path, "/reverse")
        self.assertEqual(parse_qs(urlsplit(path).query), {
            "lat": ["2.0"], "lon": ["4.0"], "format": ["jsonv2"],
            "zoom": ["18"], "addressdetails": ["0"], "accept-language": ["en"],
        })
        self.assertEqual(headers["User-Agent"], "MileageTests/1.0")
        self.assertNotIn("Cookie", headers)
        self.assertNotIn("Referer", headers)
        self.assertEqual(self.gpx_path.read_text(), self.good_gpx)
        annotation = self.viewer.get_annotations()["2026-10/sample"]
        self.assertEqual(annotation["destination"], "Keep destination")
        self.assertEqual(annotation["purpose_note"], "Keep note")
        # Success is cached even through a fresh provider object (another worker).
        with patch.dict(os.environ, {"VIEWER_GEOCODER_URL": self.provider_url}):
            other_worker = self.viewer.Geocoder(self.viewer.DB_PATH)
        self.assertEqual(other_worker.lookup(2, 4), response.json["label"])
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(len(self.received), 1)
        self.reset_gate()
        self.assertEqual(self.post("start").status_code, 200)
        self.assertEqual(parse_qs(urlsplit(self.received[-1][0]).query)["lat"], ["1.0"])
        html = self.client.get("/trip/2026-10/sample").get_data(as_text=True)
        self.assertIn("Use end suggestion as destination", html)
        self.assertIn("sends only that endpoint", html)
        self.assertNotIn(self.provider_url, html)

    def test_auth_csrf_and_invalid_endpoints_never_call_provider(self):
        self.assertEqual(self.viewer.app.test_client().post(self.route).status_code, 401)
        self.assertEqual(self.client.post(self.route, data={"endpoint": "end"}).status_code, 400)
        self.assertEqual(self.post("other").status_code, 400)
        self.assertEqual(self.client.get(self.route).status_code, 405)
        self.assertEqual(self.client.post("/trip/2026-10/missing/lookup-place",
                                         data={"csrf_token": "test-csrf", "endpoint": "end"}).status_code, 404)
        invalid = f"<gpx><trk><trkseg><trkpt lat='{float('nan')}' lon='{int(0)}'/></trkseg></trk></gpx>"
        for text in ("<gpx/>", "broken", invalid,
                     "<!DOCTYPE gpx [<!ENTITY x 'x'>]><gpx/>"):
            self.gpx_path.write_text(text)
            self.assertEqual(self.post().status_code, 422)
            html = self.client.get("/trip/2026-10/sample").get_data(as_text=True)
            self.assertIn("No valid recorded endpoint is available.", html)
            self.assertNotIn("Not looked up yet.", html)
            self.assertNotIn("data-place-label", html)
        self.assertEqual(self.received, [])

    def test_no_match_cached_and_bad_responses_fail_without_leaking(self):
        cls = type(self)
        cls.body = b'{"error":"Unable to geocode"}'
        self.assertEqual(self.post().json, {"label": ""})
        self.assertEqual(self.post().json, {"label": ""})
        self.assertEqual(len(self.received), 1)
        cases = [(200, b"not json"), (200, b"[]"), (200, b"{}"),
                 (200, b'{"display_name": 9}'), (200, b'{"display_name": ""}'),
                 (200, b'{"display_name":"a\\nb"}'),
                 (200, json.dumps({"display_name": "x" * 1001}).encode()),
                 (200, b"x" * 65537), (429, b"private upstream error"),
                 (503, b"private upstream error"), (302, b"private upstream error")]
        for status, body in cases:
            with self.subTest(status=status, body_size=len(body)):
                cls.status, cls.body = status, body
                self.reset_gate()
                result = self.post("start")
                self.assertEqual(result.status_code, 503)
                self.assertEqual(result.json, {"error": "The address provider is unavailable. Try again later."})
                before = len(self.received)
                self.assertEqual(self.post("start").status_code, 429)
                self.assertEqual(len(self.received), before)
        self.assertFalse(any("should-not-be-followed" in p for p, _ in self.received))

    def test_timeout_is_generic_and_not_cached(self):
        with patch.object(self.viewer.GEOCODER.opener, "open", side_effect=TimeoutError("private endpoint")) as call:
            response = self.post()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private endpoint", response.get_data(as_text=True))
        self.assertEqual(call.call_args.kwargs["timeout"], 5)
        self.assertIsNone(self.viewer.GEOCODER.cached(2, 4))

    def test_404_no_match_and_provider_retry_after(self):
        cls = type(self)
        cls.status, cls.body = 404, b'{"error":"Unable to geocode"}'
        self.assertEqual(self.post().json, {"label": ""})
        self.assertEqual(self.post().json, {"label": ""})
        self.assertEqual(len(self.received), 1)
        self.reset_gate()
        cls.status = 429
        with patch("geocoding.time.time", return_value=10000):
            with self.assertRaises(self.viewer.GeocodeError) as error:
                self.viewer.GEOCODER.lookup(1, 3)
            self.assertEqual(error.exception.status, 503)
        with closing(sqlite3.connect(self.viewer.DB_PATH)) as conn:
            self.assertEqual(conn.execute("SELECT next_allowed FROM geocode_gate").fetchone()[0], 13600)
        self.assertEqual(self.viewer.GEOCODER._retry_delay("bad"), 300)
        with patch("geocoding.time.time", return_value=0):
            self.assertEqual(self.viewer.GEOCODER._retry_delay("Thu, 01 Jan 1970 01:00:00 GMT"), 3600)

    def test_provider_text_is_escaped_and_only_saved_destination_is_exported(self):
        hostile = '=HYPERLINK("x")<img src=x onerror=alert(1)>'
        type(self).body = json.dumps({"display_name": hostile}).encode()
        self.assertEqual(self.post().status_code, 200)
        html = self.client.get("/trip/2026-10/sample").get_data(as_text=True)
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;img", html)
        self.assertIn('value="Keep destination"', html)
        # Emulate the existing Save flow after the user chooses a suggestion.
        saved = self.client.post("/trip/2026-10/sample/annotate", data={
            "csrf_token": "test-csrf", "category": "business", "purpose_note": "Keep note", "destination": hostile,
        })
        self.assertEqual(saved.status_code, 302)
        rows = list(csv.DictReader(io.StringIO(self.client.get("/export.csv").data.decode())))
        self.assertEqual(rows[0]["destination"], "'" + hostile)
        workbook = load_workbook(io.BytesIO(self.client.get("/export.xlsx").data))
        try:
            cells = dict(zip((c.value for c in workbook.active[1]), workbook.active[2]))
            self.assertEqual(cells["destination"].value, hostile)
            self.assertEqual(cells["destination"].data_type, "s")
        finally:
            workbook.close()
        self.assertEqual(len(self.received), 1)

    def test_shared_gate_does_not_hold_annotation_write_lock(self):
        type(self).release.clear()
        geocoder = self.viewer.GEOCODER
        with ThreadPoolExecutor(max_workers=1) as executor:
            task = executor.submit(geocoder.lookup, 1, 3)
            try:
                self.assertTrue(self.entered.wait(3))
                self.viewer.upsert_annotation("2026-10/sample", "personal", "Saved during HTTP", "Manual")
                self.assertEqual(self.post().status_code, 429)
            finally:
                type(self).release.set()
            self.assertTrue(task.result(timeout=5))
        self.assertEqual(len(self.received), 1)
        self.assertEqual(self.post().status_code, 429)

    def test_default_disabled_provider_changes_and_public_service_rejected(self):
        with patch.dict(os.environ, {"VIEWER_GEOCODER_URL": ""}):
            disabled = self.viewer.Geocoder(self.viewer.DB_PATH)
        with patch.object(self.viewer, "GEOCODER", disabled):
            self.assertEqual(self.post().status_code, 503)
            html = self.client.get("/trip/2026-10/sample").get_data(as_text=True)
            self.assertIn("Address lookup is not configured", html)
            self.assertNotIn("data-place-lookup", html)
        self.assertEqual(self.received, [])
        self.post()
        with patch.dict(os.environ, {"VIEWER_GEOCODER_URL": self.provider_url + "-other"}):
            changed = self.viewer.Geocoder(self.viewer.DB_PATH)
        self.assertIsNone(changed.cached(2, 4))
        self.gpx_path.write_text(self.good_gpx.replace('lat="2"', 'lat="3"'))
        self.assertNotIn("Invented workshop", self.client.get("/trip/2026-10/sample").get_data(as_text=True))
        for value in ("https://nominatim.openstreetmap.org/reverse", "https://nominatim.openstreetmap.org./reverse",
                      "https://OPENSTREETMAP.ORG/reverse", "file:///reverse", "https://example.invalid/reverse?key=secret",
                      "http://user:secret@example.invalid/reverse", "http://example.invalid:bad/reverse"):
            with patch.dict(os.environ, {"VIEWER_GEOCODER_URL": value}):
                with self.assertRaises(RuntimeError) as error:
                    self.viewer.Geocoder(self.viewer.DB_PATH)
                self.assertNotIn(value, str(error.exception))


    def test_slow_drip_body_hits_deadline_and_is_not_cached(self):
        type(self).drip = .03
        started = time.monotonic()
        with patch.object(self.viewer.GEOCODER, "RESPONSE_BUDGET", .25):
            response = self.post()
        elapsed = time.monotonic() - started
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json, {"error": "The address provider is unavailable. Try again later."})
        self.assertGreaterEqual(elapsed, .25)
        self.assertLess(elapsed, 1.5)
        self.assertIsNone(self.viewer.GEOCODER.cached(2, 4))
        self.assertEqual(self.post().status_code, 429)

    def test_aborted_worker_retains_failure_cooldown(self):
        for abort in (SystemExit, KeyboardInterrupt):
            with self.subTest(abort=abort.__name__):
                self.reset_gate()
                with patch.object(self.viewer.GEOCODER, "_fetch", side_effect=abort), \
                     patch("geocoding.time.time", return_value=10000):
                    with self.assertRaises(abort):
                        self.viewer.GEOCODER.lookup(2, 4)
                with closing(sqlite3.connect(self.viewer.DB_PATH)) as conn:
                    self.assertEqual(conn.execute("SELECT next_allowed FROM geocode_gate").fetchone()[0], 10300)
                self.assertIsNone(self.viewer.GEOCODER.cached(2, 4))


if __name__ == "__main__":
    unittest.main()

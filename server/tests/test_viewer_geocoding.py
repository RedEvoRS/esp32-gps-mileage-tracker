"""Endpoint privacy, cache coordination and real local HTTP integration."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import csv
import hashlib
import io
import json
import os
import re
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
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
        cls.mode = "normal"
        cls.entered = threading.Event()
        cls.release = threading.Event()
        cls.release.set()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                cls.received.append((self.path, dict(self.headers)))
                cls.entered.set()
                cls.release.wait(5)
                try:
                    if cls.mode == "silent":
                        time.sleep(0.8)
                        return
                    if cls.mode == "headers":
                        for byte in b"HTTP/1.1 200 OK\r\nX-Slow: value\r\n\r\n":
                            self.wfile.write(bytes([byte]))
                            self.wfile.flush()
                            time.sleep(.06)
                        return
                    if cls.mode == "chunked_body":
                        self.wfile.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
                        for byte in cls.body:
                            self.wfile.write(b"1\r\n" + bytes([byte]) + b"\r\n")
                            self.wfile.flush()
                            time.sleep(.06)
                        self.wfile.write(b"0\r\n\r\n")
                        return
                    if cls.mode == "chunked":
                        self.wfile.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
                        # Drip a chunk-size extension: read1 itself blocks in
                        # framing even though socket operations keep succeeding.
                        for byte in b"1;slow=" + b"x" * 40:
                            self.wfile.write(bytes([byte]))
                            self.wfile.flush()
                            time.sleep(.06)
                        return
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    return
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
            "VIEWER_GEOCODER_INTERVAL": "",
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
        cls.mode = "normal"
        cls.body = b'{"display_name":"Invented workshop, Example Road"}'
        cls.entered.clear()
        cls.release.set()
        with closing(sqlite3.connect(self.viewer.DB_PATH)) as conn, conn:
            for table in ("geocode_cache", "geocode_gate", "annotations", "trip_removals"):
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
            "zoom": ["18"], "addressdetails": ["1"], "accept-language": ["en"],
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

    def clear_cache(self):
        self.reset_gate()
        with closing(sqlite3.connect(self.viewer.DB_PATH)) as conn, conn:
            conn.execute("DELETE FROM geocode_cache")

    def label_for(self, body):
        type(self).body = json.dumps(body).encode()
        self.clear_cache()
        return self.post().json["label"]

    def test_address_formatted_as_standard_address(self):
        us = {"country_code": "us", "house_number": "123", "road": "Example Road",
              "quarter": "Q Quarter", "suburb": "Sub Suburb", "city": "Sampletown",
              "county": "Sample County", "state": "Examplestate", "ISO3166-2-lvl4": "US-ZZ",
              "postcode": "00000", "country": "United States"}
        full = "123 Example Road, Sampletown, ZZ 00000"
        self.assertEqual(self.label_for({
            "display_name": "ignored", "name": "", "address": us}), full)
        # Name only when distinct from house number, road and city.
        self.assertEqual(self.label_for({"name": "Invented Depot", "address": us}),
                         "Invented Depot, " + full)
        for same in ("123", "Example Road", "Sampletown"):
            self.assertEqual(self.label_for({"name": same, "address": us}), full)
        self.assertEqual(self.label_for({"name": 5, "address": us}), full)
        no_number = {k: v for k, v in us.items() if k != "house_number"}
        self.assertEqual(self.label_for({"address": no_number}),
                         "Example Road, Sampletown, ZZ 00000")
        for bad in ({"ISO3166-2-lvl4": None}, {"ISO3166-2-lvl4": "ZZ"},
                    {"ISO3166-2-lvl4": "US-C1"}, {"ISO3166-2-lvl4": "US-ZZZ"}):
            self.assertEqual(self.label_for({"address": {**us, **bad}}),
                             "123 Example Road, Sampletown, Examplestate 00000")
        missing = {k: v for k, v in us.items() if k != "ISO3166-2-lvl4"}
        self.assertEqual(self.label_for({"address": missing}),
                         "123 Example Road, Sampletown, Examplestate 00000")
        town = {k: v for k, v in us.items() if k != "city"} | {"town": "Sampleton"}
        self.assertEqual(self.label_for({"address": town}), "123 Example Road, Sampleton, ZZ 00000")
        no_post = {k: v for k, v in us.items() if k != "postcode"}
        self.assertEqual(self.label_for({"address": no_post}), "123 Example Road, Sampletown, ZZ")
        self.assertEqual(self.label_for({"address": {**us, "city": " ", "village": "Samplevale"}}),
                         "123 Example Road, Samplevale, ZZ 00000")

    def test_non_us_address_includes_country(self):
        address = {"country_code": "gb", "house_number": "7", "road": "Example Road",
                   "suburb": "Sub", "city": "Sampletown", "county": "Sample County",
                   "state": "Examplestate", "postcode": "AB1 2CD", "country": "Exampleland"}
        self.assertEqual(self.label_for({"address": address}),
                         "7 Example Road, Sampletown, Examplestate, AB1 2CD, Exampleland")
        del address["state"], address["house_number"]
        self.assertEqual(self.label_for({"name": "Invented Hall", "address": address}),
                         "Invented Hall, Example Road, Sampletown, AB1 2CD, Exampleland")

    def test_address_fallbacks_and_type_validation(self):
        shown = " Invented workshop, Example Road "
        for body in ({"display_name": shown},
                     {"display_name": shown, "address": "text"},
                     {"display_name": shown, "address": []},
                     {"display_name": shown, "address": {}},
                     {"display_name": shown, "address": {"state": "Examplestate", "postcode": "00000"}},
                     {"display_name": shown, "address": {"road": 5, "city": ["Sampletown"], "house_number": 1}}):
            self.assertEqual(self.label_for(body), "Invented workshop, Example Road")
        self.assertEqual(self.label_for({"address": {
            "country_code": "us", "road": "Example Road", "house_number": 9,
            "city": "Sampletown", "state": 4, "postcode": 12345}}),
            "Example Road, Sampletown")
        # Built labels still obey the label validation.
        for address in ({"road": "a\nb", "city": "Sampletown"}, {"road": "x" * 1001}):
            type(self).body = json.dumps({"address": address}).encode()
            self.clear_cache()
            self.assertEqual(self.post().status_code, 503)

    def test_cache_key_includes_label_format(self):
        geocoder = self.viewer.GEOCODER
        old = hashlib.sha256(f"{geocoder.url}\n2.0\n4.0".encode()).hexdigest()
        self.assertNotEqual(geocoder.key(2, 4), old)
        with closing(sqlite3.connect(self.viewer.DB_PATH)) as conn, conn:
            conn.execute("INSERT INTO geocode_cache VALUES (?, ?, 0)", (old, "Old neighbourhood label"))
        self.assertIsNone(geocoder.cached(2, 4))
        self.assertEqual(self.post().json["label"], "Invented workshop, Example Road")
        self.assertEqual(len(self.received), 1)

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
        with patch("geocoding.build_opener") as factory, patch.object(
                self.viewer.GEOCODER, "_fetch", self.viewer.GEOCODER._fetch_direct):
            call = factory.return_value.open
            call.side_effect = TimeoutError("private endpoint")
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

    def test_default_interval_is_applied_to_success_gate(self):
        for i, value in enumerate((None, "", " ")):
            with self.subTest(value=value), patch.dict(os.environ, {"VIEWER_GEOCODER_URL": self.provider_url}):
                if value is None:
                    os.environ.pop("VIEWER_GEOCODER_INTERVAL", None)
                else:
                    os.environ["VIEWER_GEOCODER_INTERVAL"] = value
                geocoder = self.viewer.Geocoder(self.viewer.DB_PATH)
                self.reset_gate()
                with patch("geocoding.time.time", return_value=10000):
                    self.assertTrue(geocoder.lookup(i, i + 2))
                with closing(sqlite3.connect(self.viewer.DB_PATH)) as conn:
                    self.assertEqual(conn.execute("SELECT next_allowed FROM geocode_gate").fetchone()[0], 10000.1)

    def test_custom_interval_is_enforced_across_workers(self):
        with patch.dict(os.environ, {"VIEWER_GEOCODER_URL": self.provider_url, "VIEWER_GEOCODER_INTERVAL": "0.5"}):
            geocoder = self.viewer.Geocoder(self.viewer.DB_PATH)
            other_worker = self.viewer.Geocoder(self.viewer.DB_PATH)
        with patch("geocoding.time.time", return_value=10000):
            self.assertTrue(geocoder.lookup(1, 3))
        with closing(sqlite3.connect(self.viewer.DB_PATH)) as conn:
            self.assertEqual(conn.execute("SELECT next_allowed FROM geocode_gate").fetchone()[0], 10000.5)
        clock = [10000.49]
        def sleep(delay):
            clock[0] += delay
        with patch("geocoding.time.time", side_effect=lambda: clock[0]), \
             patch("geocoding.time.sleep", side_effect=sleep) as waiting:
            self.assertTrue(other_worker.lookup(2, 4))
        self.assertAlmostEqual(waiting.call_args.args[0], .01)
        self.assertEqual(len(self.received), 2)

    def set_gate(self, delay, token=""):
        with closing(sqlite3.connect(self.viewer.DB_PATH)) as conn, conn:
            conn.execute("INSERT OR REPLACE INTO geocode_gate VALUES (1, ?, ?)", (10000 + delay, token))

    def test_short_gap_wait_releases_lock_and_checks_cache_first(self):
        self.set_gate(.05)
        clock = [10000.0]
        def sleep(delay):
            self.assertLessEqual(delay, 2)
            # A separate connection can write while this request waits.
            self.viewer.upsert_annotation("2026-10/sample", "personal", "While waiting", "Manual")
            clock[0] += delay
        with patch("geocoding.time.time", side_effect=lambda: clock[0]), \
             patch("geocoding.time.sleep", side_effect=sleep) as waiting, \
             patch.object(self.viewer.GEOCODER, "_fetch", return_value="Example") as fetch:
            self.assertEqual(self.viewer.GEOCODER.lookup(1, 3), "Example")
            self.assertEqual(self.viewer.GEOCODER.lookup(1, 3), "Example")
        waiting.assert_called_once()
        fetch.assert_called_once()

    def test_failure_cooldown_and_long_gap_refused_without_waiting(self):
        # A marked failure, a gap longer than the wait budget, and a closed gate
        # further ahead than any reservation (older unmarked failure rows).
        for delay, token in ((.05, "failed"), (300, "failed"), (3600, "failed"), (3, ""),
                             (self.viewer.GEOCODER.LEASE + 1, "older-random-token")):
            self.set_gate(delay, token)
            with patch.object(self.viewer.GEOCODER, "interval", 60), \
                 patch("geocoding.time.time", return_value=10000), \
                 patch("geocoding.time.sleep") as waiting, \
                 patch.object(self.viewer.GEOCODER, "_fetch") as fetch:
                with self.assertRaises(self.viewer.GeocodeError) as error:
                    self.viewer.GEOCODER.lookup(1, 3)
                self.assertEqual(error.exception.status, 429)
                waiting.assert_not_called()
                fetch.assert_not_called()

    def test_running_lookup_is_awaited_then_gap_respected(self):
        # Another worker holds the reservation and finishes 0.3 s later.
        self.set_gate(16, "active")
        clock = [10000.0]
        sleeps = []
        def sleep(delay):
            sleeps.append(delay)
            clock[0] += delay
            if clock[0] >= 10000.3:
                with closing(sqlite3.connect(self.viewer.DB_PATH)) as conn, conn:
                    conn.execute("UPDATE geocode_gate SET next_allowed = ?, token = '' "
                                 "WHERE token = 'active'", (10000.3 + self.viewer.GEOCODER.interval,))
        with patch("geocoding.time.time", side_effect=lambda: clock[0]), \
             patch("geocoding.time.monotonic", side_effect=lambda: clock[0]), \
             patch("geocoding.time.sleep", side_effect=sleep), \
             patch.object(self.viewer.GEOCODER, "_fetch", return_value="Example") as fetch:
            self.assertEqual(self.viewer.GEOCODER.lookup(1, 3), "Example")
        fetch.assert_called_once()
        # Short polls while the other lookup runs, then at most one normal gap.
        polls = [delay for delay in sleeps if delay <= self.viewer.GEOCODER.POLL + 1e-9]
        self.assertGreaterEqual(len(polls), 5)
        self.assertLessEqual(len(sleeps) - len(polls), 1)
        self.assertTrue(all(delay <= self.viewer.GEOCODER.interval + 1e-9 for delay in sleeps))
        # The provider is called only after the finished lookup's gap.
        self.assertGreaterEqual(clock[0], 10000.3 + self.viewer.GEOCODER.interval - 1e-9)
        self.assertLessEqual(clock[0] - 10000, self.viewer.GEOCODER.WAIT_CAP)

    def test_running_lookup_that_never_finishes_is_refused_within_budget(self):
        self.set_gate(16, "active")
        clock = [10000.0]
        def sleep(delay):
            clock[0] += delay
        with patch("geocoding.time.time", side_effect=lambda: clock[0]), \
             patch("geocoding.time.monotonic", side_effect=lambda: clock[0]), \
             patch("geocoding.time.sleep", side_effect=sleep), \
             patch.object(self.viewer.GEOCODER, "_fetch") as fetch:
            with self.assertRaises(self.viewer.GeocodeError) as error:
                self.viewer.GEOCODER.lookup(1, 3)
        self.assertEqual(error.exception.status, 429)
        fetch.assert_not_called()
        self.assertLessEqual(clock[0] - 10000, self.viewer.GEOCODER.WAIT_CAP)

    def test_provider_failure_marks_gate_failed(self):
        failure = self.viewer.GeocodeError("The address provider is unavailable. Try again later.")
        with patch("geocoding.time.time", return_value=10000), \
             patch.object(self.viewer.GEOCODER, "_fetch", side_effect=failure):
            with self.assertRaises(self.viewer.GeocodeError):
                self.viewer.GEOCODER.lookup(1, 3)
        with closing(sqlite3.connect(self.viewer.DB_PATH)) as conn:
            self.assertEqual(conn.execute("SELECT token, next_allowed FROM geocode_gate").fetchone(),
                             (self.viewer.GEOCODER.FAILED, 10300))
        with patch("geocoding.time.time", return_value=10001), \
             patch("geocoding.time.sleep") as waiting, \
             patch.object(self.viewer.GEOCODER, "_fetch") as fetch:
            with self.assertRaises(self.viewer.GeocodeError) as error:
                self.viewer.GEOCODER.lookup(2, 4)
        self.assertEqual(error.exception.status, 429)
        waiting.assert_not_called()
        fetch.assert_not_called()

    def test_wait_deadline_is_bounded_when_gate_keeps_moving(self):
        self.set_gate(.1)
        clock = [10000.0]
        def sleep(delay):
            clock[0] += delay
            with closing(sqlite3.connect(self.viewer.DB_PATH)) as conn, conn:
                conn.execute("UPDATE geocode_gate SET next_allowed = ?", (clock[0] + .1,))
        with patch("geocoding.time.time", side_effect=lambda: clock[0]), \
             patch("geocoding.time.monotonic", side_effect=lambda: clock[0]), \
             patch("geocoding.time.sleep", side_effect=sleep), \
             patch.object(self.viewer.GEOCODER, "_fetch") as fetch:
            with self.assertRaises(self.viewer.GeocodeError):
                self.viewer.GEOCODER.lookup(1, 3)
        self.assertLessEqual(clock[0] - 10000, 2)
        fetch.assert_not_called()

    def test_two_waiters_share_cache_after_atomic_reservation(self):
        self.set_gate(.05)
        barrier = threading.Barrier(2)
        clock = [10000.0]
        def sleep(delay):
            barrier.wait(timeout=3)
            clock[0] = 10000.05
        with patch("geocoding.time.time", side_effect=lambda: clock[0]), \
             patch("geocoding.time.sleep", side_effect=sleep), \
             patch.object(self.viewer.GEOCODER, "_fetch", return_value="Example") as fetch:
            def lookup():
                try:
                    return self.viewer.GEOCODER.lookup(1, 3)
                except self.viewer.GeocodeError as exc:
                    return exc.status
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda _: lookup(), range(2)))
        self.assertIn("Example", results)
        self.assertTrue(all(result in ("Example", 429) for result in results))
        fetch.assert_called_once()

    def test_two_distinct_waiters_cannot_call_inside_success_gap(self):
        geocoder = self.viewer.GEOCODER
        with closing(sqlite3.connect(self.viewer.DB_PATH)) as conn, conn:
            conn.execute("INSERT INTO geocode_gate VALUES (1, ?, '')", (time.time() + .05,))
        barrier = threading.Barrier(2)
        called = []
        def fetch(*args):
            called.append(time.monotonic())
            return "Example"
        def lookup(value):
            barrier.wait(timeout=3)
            try:
                return geocoder.lookup(value, value + 2)
            except self.viewer.GeocodeError as exc:
                return exc.status
        with patch.object(geocoder, "_fetch", side_effect=fetch):
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lookup, (1, 2)))
        self.assertTrue(all(result in ("Example", 429) for result in results))
        self.assertGreaterEqual(len(called), 1)
        if len(called) == 2:
            self.assertGreaterEqual(called[1] - called[0], geocoder.interval)

    def test_auto_removed_and_invalid_pages_have_no_lookup_hook_or_both_button(self):
        with patch.object(self.viewer, "GEOCODER_AUTO_LOOKUP", True):
            self.gpx_path.write_text("<gpx/>")
            html = self.client.get("/trip/2026-10/sample").get_data(as_text=True)
            self.assertNotIn('id="lookup-both-places"', html)
            self.assertNotIn("data-place-lookup", html)
            self.gpx_path.write_text(self.good_gpx)
            response = self.client.post("/trip/2026-10/sample/remove", data={
                "csrf_token": "test-csrf", "reason": "Duplicate test recording"})
            self.assertEqual(response.status_code, 302)
            html = self.client.get("/trip/2026-10/sample").get_data(as_text=True)
            self.assertNotIn('id="auto-lookup"', html)
            self.assertNotIn("data-auto-lookup", html)
            self.assertNotIn("data-place-lookup", html)
            self.assertNotIn('id="lookup-both-places"', html)
            self.assertEqual(self.post().status_code, 409)
        self.assertEqual(self.received, [])

    def test_auto_setting_parsing_and_startup_failure(self):
        env = {
            "DISPLAY_TZ": "UTC", "VIEWER_DB_PATH": str(self.viewer.DB_PATH),
            "INGEST_DATA_ROOT": str(self.viewer.DATA_ROOT),
            "VIEWER_USERNAME": "driver", "VIEWER_SECRET_KEY": "test-key-" * 8,
            "VIEWER_PASSWORD_HASH": generate_password_hash("pw", "pbkdf2:sha256:1000"),
        }
        for value, expected in (("", False), ("false", False), ("FALSE", False), ("true", True), ("TRUE", True)):
            with patch.dict(os.environ, {**env, "VIEWER_GEOCODER_AUTO_LOOKUP": value}):
                self.assertEqual(load_service("mileage-viewer").GEOCODER_AUTO_LOOKUP, expected)
        with patch.dict(os.environ, env):
            os.environ.pop("VIEWER_GEOCODER_AUTO_LOOKUP", None)
            self.assertFalse(load_service("mileage-viewer").GEOCODER_AUTO_LOOKUP)
        for value in ("1", "yes", " true ", "invalid"):
            with patch.dict(os.environ, {**env, "VIEWER_GEOCODER_AUTO_LOOKUP": value}):
                with self.assertRaisesRegex(RuntimeError, "refusing to start: VIEWER_GEOCODER_AUTO_LOOKUP must be true or false"):
                    load_service("mileage-viewer")

    def test_auto_page_text_hook_and_both_button(self):
        for enabled in (False, True):
            with patch.object(self.viewer, "GEOCODER_AUTO_LOOKUP", enabled):
                html = self.client.get("/trip/2026-10/sample").get_data(as_text=True)
            self.assertIn('<span data-auto-lookup-status>', html)
            self.assertEqual('id="auto-lookup"' in html, enabled)
            if enabled:
                nav = re.search(r'<nav class="trip-nav".*?</nav>', html, re.S).group()
                self.assertRegex(nav.replace('\n', ''),
                                 r'Previous</span>.*?<button[^>]*id="auto-lookup"'
                                 r'[^>]*type="button"[^>]*aria-pressed="true" hidden>'
                                 r'Auto lookup: On</button>.*?Next')
            self.assertEqual('data-auto-lookup="true"' in html, enabled)
            self.assertEqual("Nothing is sent until you click." in html, not enabled)
            self.assertEqual("Uncached endpoints are looked up when a trip opens." in html, enabled)
            self.assertIn('id="lookup-both-places"', html)
            self.assertIn('data-place-cached="false"', html)
        self.assertEqual(self.received, [])
        self.post()
        html = self.client.get("/trip/2026-10/sample").get_data(as_text=True)
        self.assertIn('data-place-cached="true"', html)
        with patch.object(self.viewer.GEOCODER, "url", ""), patch.object(self.viewer, "GEOCODER_AUTO_LOOKUP", True):
            html = self.client.get("/trip/2026-10/sample").get_data(as_text=True)
            self.assertNotIn('id="auto-lookup"', html)
            self.assertNotIn("data-auto-lookup", html)
            self.assertNotIn('id="lookup-both-places"', html)

    def test_interval_range_includes_both_limits(self):
        for value in ("0.1", "60"):
            with self.subTest(value=value), patch.dict(os.environ, {"VIEWER_GEOCODER_INTERVAL": value}):
                self.assertEqual(self.viewer.Geocoder(self.viewer.DB_PATH).interval, float(value))

    def test_invalid_interval_rejected_even_when_disabled(self):
        for url in ("", self.provider_url):
            for value in ("abc", "nan", "inf", "-inf", "-1", "0", "0.05", "61"):
                with self.subTest(enabled=bool(url), value=value), patch.dict(os.environ, {
                    "VIEWER_GEOCODER_URL": url, "VIEWER_GEOCODER_INTERVAL": value,
                }):
                    with self.assertRaisesRegex(RuntimeError, "VIEWER_GEOCODER_INTERVAL.*finite.*0.1 to 60"):
                        self.viewer.Geocoder(self.viewer.DB_PATH)

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
            cells = dict(zip((c.value for c in workbook.active[1]), workbook.active[2], strict=True))
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
        self.assertEqual(self.post().status_code, 200)

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

    def test_stalled_headers_and_chunk_framing_are_killed_and_cooled_down(self):
        import geocoding
        geocoder = self.viewer.GEOCODER
        for mode in ("silent", "headers", "chunked", "chunked_body"):
            with self.subTest(mode=mode):
                self.reset_gate()
                self.entered.clear()
                type(self).mode = mode
                started = time.monotonic()
                with patch.object(geocoder, "RESPONSE_BUDGET", .45):
                    with self.assertRaises(self.viewer.GeocodeError) as error:
                        geocoder.lookup(2, 4)
                self.assertEqual(error.exception.status, 503)
                self.assertTrue(self.entered.is_set())
                self.assertGreaterEqual(time.monotonic() - started, .45)
                self.assertLess(time.monotonic() - started, 1.5)
                self.assertIsNotNone(geocoding._fetch_process.poll())
                self.assertIsNone(geocoder.cached(2, 4))
                with closing(sqlite3.connect(self.viewer.DB_PATH)) as conn:
                    next_allowed = conn.execute("SELECT next_allowed FROM geocode_gate").fetchone()[0]
                self.assertGreater(next_allowed, time.time() + 295)
                self.assertEqual(self.post().status_code, 429)
                # Failure gate expires and a subsequent lookup can succeed.
                type(self).mode = "normal"
                with patch("geocoding.time.time", return_value=next_allowed + 1):
                    self.assertTrue(geocoder.lookup(2, 4))
                self.clear_cache()

    def test_repeated_deadlines_leave_no_children_or_fetch_threads(self):
        import geocoding
        type(self).mode = "headers"
        threads = {thread.ident for thread in threading.enumerate()}
        for _ in range(5):
            self.reset_gate()
            with patch.object(self.viewer.GEOCODER, "RESPONSE_BUDGET", .3):
                self.assertEqual(self.post().status_code, 503)
            self.assertIsNotNone(geocoding._fetch_process.poll())
        # Only local fake-provider handlers may have been added.
        added = [thread for thread in threading.enumerate() if thread.ident not in threads]
        self.assertTrue(all("process_request_thread" in thread.name for thread in added))

    def test_lease_is_derived_from_deadline_and_cleanup_with_margin(self):
        geocoder = self.viewer.GEOCODER
        for budget in (.1, 10, 120):
            with patch.object(geocoder, "RESPONSE_BUDGET", budget):
                self.assertGreater(geocoder.LEASE, budget + geocoder.CLEANUP_BUDGET)

    def test_dns_stall_is_killed_at_whole_request_deadline(self):
        import geocoding
        original = subprocess.Popen

        def stalled_dns(command, **kwargs):
            # Patch DNS in the actual isolated interpreter, before running its
            # normal entry point. No external DNS or provider is contacted.
            script = ("import socket,time,runpy,sys; "
                      "socket.getaddrinfo=lambda *a,**k: time.sleep(60); "
                      "runpy.run_path(sys.argv[1],run_name='__main__')")
            return original([command[0], "-c", script, command[1]], **kwargs)

        started = time.monotonic()
        with patch("geocoding.subprocess.Popen", side_effect=stalled_dns), \
             patch.object(self.viewer.GEOCODER, "RESPONSE_BUDGET", .4):
            with self.assertRaises(self.viewer.GeocodeError):
                self.viewer.GEOCODER.lookup(2, 4)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertIsNotNone(geocoding._fetch_process.poll())
        self.assertEqual(self.received, [])
        self.assertEqual(self.post().status_code, 429)

    def test_child_pending_cleanup_refuses_replacement(self):
        child = Mock()
        child.poll.return_value = None
        with patch("geocoding._fetch_process", child), \
             patch("geocoding.subprocess.Popen") as spawn:
            with self.assertRaises(self.viewer.GeocodeError):
                self.viewer.GEOCODER.lookup(2, 4)
        spawn.assert_not_called()
        self.assertEqual(self.post().status_code, 429)

    def test_only_reservation_holder_releases_gate(self):
        def replaced_reservation(*args):
            with closing(sqlite3.connect(self.viewer.DB_PATH)) as conn, conn:
                conn.execute("UPDATE geocode_gate SET token = 'other', next_allowed = 99999999999")
            raise self.viewer.GeocodeError("The address provider is unavailable. Try again later.")

        with patch.object(self.viewer.GEOCODER, "_fetch", side_effect=replaced_reservation):
            with self.assertRaises(self.viewer.GeocodeError):
                self.viewer.GEOCODER.lookup(2, 4)
        with closing(sqlite3.connect(self.viewer.DB_PATH)) as conn:
            self.assertEqual(conn.execute("SELECT token, next_allowed FROM geocode_gate").fetchone(),
                             ("other", 99999999999))

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

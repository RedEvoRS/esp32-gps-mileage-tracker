"""Regression tests for upload integrity and untrusted GPX values."""

import importlib.util
import json
import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


APP_PATH = Path(__file__).resolve().parents[1] / "mileage-ingest" / "app.py"
HEADERS = {"Authorization": "Bearer test-token", "X-Trip-Id": "audit-trip",
           "X-Device-Id": "test-device"}


def load_ingest(root):
    with mock.patch.dict(os.environ, INGEST_AUTH_TOKEN="test-token",
                         INGEST_DATA_ROOT=str(root), DISPLAY_TZ="America/Los_Angeles"):
        spec = importlib.util.spec_from_file_location("ingest_safety", APP_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


def body(points=2, lat="37", lon="-122", speed="10", hdop="1"):
    track = "".join(
        f'<trkpt lat="{lat}" lon="{lon}"><time>2026-04-14T14:30:{i:02d}Z</time>'
        f"<speed>{speed}</speed><hdop>{hdop}</hdop></trkpt>" for i in range(points)
    )
    return f"<gpx><trk><trkseg>{track}</trkseg></trk></gpx>".encode()


def upload_worker(root, payload, barrier, results):
    try:
        app = load_ingest(root)
        barrier.wait(timeout=20)
        response = app.app.test_client().post("/upload", data=payload, headers=HEADERS)
        results.put((response.status_code, response.get_json()))
    except Exception as exc:
        results.put(("exception", repr(exc)))


class UploadSafety(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ing = load_ingest(self.root)
        self.client = self.ing.app.test_client()

    def upload(self, payload=None, **headers):
        return self.client.post("/upload", data=payload or body(), headers=HEADERS | headers)

    def saved_pair(self, response):
        gpx = self.root / response.get_json()["stored_as"]
        return gpx, gpx.with_suffix(".json")

    def test_identical_retry_preserves_original_record(self):
        response = self.upload()
        self.assertEqual(response.status_code, 200)
        gpx, summary = self.saved_pair(response)
        before = (gpx.read_bytes(), summary.read_bytes())
        self.assertEqual(self.upload().status_code, 200)
        self.assertEqual((gpx.read_bytes(), summary.read_bytes()), before)

    def test_different_payload_cannot_overwrite_existing_trip(self):
        gpx, summary = self.saved_pair(self.upload())
        before = (gpx.read_bytes(), summary.read_bytes())
        self.assertEqual(self.upload(body(3)).status_code, 409)
        self.assertEqual((gpx.read_bytes(), summary.read_bytes()), before)

    def test_device_and_sanitized_id_collisions_preserve_existing_trip(self):
        response = self.upload(**{"X-Trip-Id": "trip:id"})
        gpx, summary = self.saved_pair(response)
        before = (gpx.read_bytes(), summary.read_bytes())
        for headers in ({"X-Trip-Id": "trip_id"},
                        {"X-Trip-Id": "trip:id", "X-Device-Id": "other"}):
            self.assertEqual(self.upload(**headers).status_code, 409)
        self.assertEqual((gpx.read_bytes(), summary.read_bytes()), before)

    def test_retry_repairs_summary_after_write_failure(self):
        with mock.patch.object(self.ing, "_atomic_write_json", side_effect=OSError("disk full")):
            self.assertEqual(self.upload().status_code, 500)
        self.assertEqual(len(list(self.root.glob("*/*.gpx"))), 1)
        self.assertEqual(list(self.root.glob("*/*.json")), [])
        response = self.upload()
        self.assertEqual(response.status_code, 200)
        gpx, summary = self.saved_pair(response)
        self.assertEqual(gpx.read_bytes(), body())
        self.assertEqual(json.loads(summary.read_bytes())["num_points"], 2)

    def test_failed_file_sync_is_not_acknowledged(self):
        with mock.patch.object(self.ing, "_sync_directory"), \
                mock.patch.object(self.ing.os, "fsync", side_effect=OSError("sync failure")):
            self.assertEqual(self.upload().status_code, 500)
        self.assertEqual(list(self.root.glob("*/*.gpx")), [])
        self.assertEqual(list(self.root.glob("*/*.tmp")), [])

    def test_non_ascii_bad_token_is_unauthorized(self):
        self.assertEqual(self.upload(**{"Authorization": "Bearer caf\u00e9"}).status_code, 401)

    def test_invalid_numeric_fields_are_rejected_without_storing(self):
        for field, values in {"lat": ("nan", "inf", "91", "-91"),
                              "lon": ("nan", "-inf", "181", "-181"),
                              "speed": ("nan", "inf", "-1"),
                              "hdop": ("nan", "inf", "-1")}.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    self.assertEqual(self.upload(body(**{field: value})).status_code, 400)
        self.assertEqual(list(self.root.glob("*/*.gpx")), [])
        self.assertEqual(list(self.root.glob("*/*.json")), [])

    def test_unicode_trip_id_uses_viewer_compatible_filename(self):
        response = self.upload(**{"X-Trip-Id": "caf\u00e9-trip"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Path(response.get_json()["stored_as"]).stem, "caf_-trip")

    def run_workers(self, payloads):
        ctx = multiprocessing.get_context("spawn")
        barrier = ctx.Barrier(len(payloads))
        results = ctx.Queue()
        processes = [ctx.Process(target=upload_worker,
                                 args=(str(self.root), payload, barrier, results))
                     for payload in payloads]
        try:
            for process in processes:
                process.start()
            responses = [results.get(timeout=40) for _ in processes]
            for process in processes:
                process.join(timeout=10)
                self.assertEqual(process.exitcode, 0)
            return responses
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=5)
            results.close()

    def test_concurrent_conflicting_uploads_keep_one_consistent_pair(self):
        payloads = [body(i) for i in range(2, 6)]
        responses = self.run_workers(payloads)
        self.assertEqual(sorted(status for status, _ in responses), [200, 409, 409, 409], responses)
        gpx = next(self.root.glob("*/*.gpx"))
        stored = gpx.read_bytes()
        self.assertIn(stored, payloads)
        summary = json.loads(gpx.with_suffix(".json").read_bytes())
        self.assertEqual(summary["num_points"], stored.count(b"<trkpt "))
        self.assertEqual(list(self.root.glob("*/*.tmp")), [])

    def test_concurrent_identical_retries_all_succeed(self):
        responses = self.run_workers([body()] * 4)
        self.assertEqual([status for status, _ in responses], [200] * 4, responses)
        self.assertEqual(len(list(self.root.glob("*/*.gpx"))), 1)
        self.assertEqual(len(list(self.root.glob("*/*.json"))), 1)


if __name__ == "__main__":
    unittest.main()

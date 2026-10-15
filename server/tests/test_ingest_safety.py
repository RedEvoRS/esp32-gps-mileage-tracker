"""Regression tests for upload integrity and untrusted GPX values."""

import importlib.util
import json
import multiprocessing
import os
import tempfile
import unittest
from datetime import datetime, timezone
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


def body(points=2, lat="37", lon="-122", speed="10", hdop="1", timed=True):
    track = "".join(
        f'<trkpt lat="{lat}" lon="{lon}">'
        + (f'<time>2026-04-14T14:30:{i:02d}Z</time>' if timed else "")
        + f"<speed>{speed}</speed><hdop>{hdop}</hdop></trkpt>" for i in range(points)
    )
    return f"<gpx><trk><trkseg>{track}</trkseg></trk></gpx>".encode()


def upload_worker(root, payload, barrier, results, headers=None, instant=None):
    try:
        app = load_ingest(root)
        with mock.patch.object(app, "datetime", wraps=datetime) as clock:
            if instant is not None:
                clock.now.return_value = instant
            barrier.wait(timeout=20)
            response = app.app.test_client().post("/upload", data=payload, headers=headers or HEADERS)
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

    def test_retry_rebuilds_a_damaged_summary_when_the_gpx_matches(self):
        gpx, summary = self.saved_pair(self.upload())
        for damage in (b'{"trip_id": "audit-tr', b'\xff\xfe{}', b'[1]', b'{}',
                       b'{"trip_id": 1, "device_id": "test-device"}'):
            with self.subTest(damage=damage):
                summary.write_bytes(damage)
                # A different GPX under the same ID still can't take it over.
                self.assertEqual(self.upload(body(3)).status_code, 409)
                self.assertEqual(summary.read_bytes(), damage)
                with self.assertLogs(self.ing.app.logger, "WARNING") as logs:
                    self.assertEqual(self.upload().status_code, 200)
                self.assertIn("damaged summary", logs.output[0])
                record = json.loads(summary.read_bytes())
                self.assertEqual((record["trip_id"], record["device_id"], record["num_points"]),
                                 ("audit-trip", "test-device", 2))
                self.assertEqual(gpx.read_bytes(), body())

    def test_damaged_summary_without_its_gpx_is_409_and_kept(self):
        gpx, summary = self.saved_pair(self.upload())
        gpx.unlink()
        summary.write_bytes(b"[1]")
        response = self.upload()
        self.assertEqual(response.status_code, 409)
        self.assertIn("damaged summary", response.get_json()["error"])
        self.assertEqual(summary.read_bytes(), b"[1]")
        self.assertFalse(gpx.exists())

    def test_unreadable_summary_file_is_a_500_not_a_rebuild(self):
        gpx, summary = self.saved_pair(self.upload())
        before = summary.read_bytes()
        real_read = Path.read_bytes
        def read_bytes(path):
            if path == summary:
                raise PermissionError("denied")
            return real_read(path)
        with mock.patch.object(Path, "read_bytes", read_bytes):
            self.assertEqual(self.upload().status_code, 500)
        self.assertEqual(summary.read_bytes(), before)

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

    def test_fallback_trip_id_is_utc_start_time_and_file_name(self):
        # No X-Trip-Id (the firmware's case), with times given at an offset.
        headers = {k: v for k, v in HEADERS.items() if k != "X-Trip-Id"}
        payload = body().replace(b"Z</time>", b"-07:00</time>")
        response = self.client.post("/upload", data=payload, headers=headers)
        self.assertEqual(response.status_code, 200)
        gpx, summary = self.saved_pair(response)
        self.assertEqual(gpx.stem, "2026-04-14T21-30-00Z")
        self.assertEqual(json.loads(summary.read_bytes())["trip_id"], gpx.stem)
        self.assertEqual(response.get_json()["trip_id"], gpx.stem)

    def untimed_upload(self, instant, payload=None, **headers):
        # Firmware sends no X-Trip-Id. These instants straddle midnight in
        # DISPLAY_TZ, not UTC, so the default storage folder changes.
        request_headers = {k: v for k, v in HEADERS.items() if k != "X-Trip-Id"}
        with mock.patch.object(self.ing, "datetime", wraps=datetime) as clock:
            clock.now.return_value = instant
            return self.client.post("/upload", data=payload or body(timed=False),
                                    headers=request_headers | headers)

    def test_untimed_retry_preserves_original_month_and_receipt(self):
        for before, after, month in (
            ("2026-11-01T06:59:59+00:00", "2026-11-01T07:00:01+00:00", "2026-10"),
            ("2027-01-01T07:59:59+00:00", "2027-01-01T08:00:01+00:00", "2026-12"),
        ):
            with self.subTest(month=month):
                # Distinct bodies keep the two boundary cases independent.
                payload = body(points=2 if month == "2026-10" else 3, timed=False)
                response = self.untimed_upload(datetime.fromisoformat(before), payload)
                self.assertEqual(response.status_code, 200)
                gpx, summary = self.saved_pair(response)
                self.assertEqual(gpx.parent.name, month)
                original = (gpx.read_bytes(), summary.read_bytes())
                retry = self.untimed_upload(datetime.fromisoformat(after), payload)
                self.assertEqual(retry.status_code, 200)
                self.assertEqual(retry.json["stored_as"], response.json["stored_as"])
                self.assertEqual((gpx.read_bytes(), summary.read_bytes()), original)
                self.assertEqual(len(list(self.root.glob(f"*/{gpx.name}"))), 1)

    def test_untimed_retry_recovers_partial_or_damaged_pair_in_original_month(self):
        before = datetime(2026, 11, 1, 6, 59, 59, tzinfo=timezone.utc)
        after = datetime(2026, 11, 1, 7, 0, 1, tzinfo=timezone.utc)
        for points, damage in enumerate(("missing_summary", "damaged_summary", "missing_gpx"), 2):
            with self.subTest(damage=damage):
                payload = body(points=points, timed=False)
                if damage == "missing_summary":
                    with mock.patch.object(self.ing, "_atomic_write_json", side_effect=OSError("disk full")):
                        self.assertEqual(self.untimed_upload(before, payload).status_code, 500)
                    gpx = next(path for path in self.root.glob("*/*.gpx")
                               if path.read_bytes() == payload)
                    summary = gpx.with_suffix(".json")
                else:
                    response = self.untimed_upload(before, payload)
                    self.assertEqual(response.status_code, 200)
                    gpx, summary = self.saved_pair(response)
                    if damage == "damaged_summary":
                        summary.write_bytes(b'{"trip_id":')
                    else:
                        gpx.unlink()
                retry = self.untimed_upload(after, payload)
                self.assertEqual(retry.status_code, 200)
                self.assertEqual(self.saved_pair(retry), (gpx, summary))
                self.assertEqual(gpx.read_bytes(), payload)
                self.assertIsNone(json.loads(summary.read_bytes())["start_time"])
                self.assertEqual(len(list(self.root.glob(f"*/{gpx.name}"))), 1)

    def test_untimed_cross_month_conflicts_cannot_bypass_original_record(self):
        before = datetime(2026, 11, 1, 6, 59, 59, tzinfo=timezone.utc)
        after = datetime(2026, 11, 1, 7, 0, 1, tzinfo=timezone.utc)
        response = self.untimed_upload(before, **{"X-Trip-Id": "trip:id"})
        gpx, summary = self.saved_pair(response)
        original = (gpx.read_bytes(), summary.read_bytes())
        for payload, headers in (
            (body(3, timed=False), {"X-Trip-Id": "trip:id"}),
            (body(timed=False), {"X-Trip-Id": "trip:id", "X-Device-Id": "other"}),
            (body(timed=False), {"X-Trip-Id": "trip_id"}),
        ):
            with self.subTest(headers=headers):
                self.assertEqual(self.untimed_upload(after, payload, **headers).status_code, 409)
                self.assertEqual((gpx.read_bytes(), summary.read_bytes()), original)
        self.assertEqual(len(list(self.root.glob("*/*.gpx"))), 1)

    def test_untimed_damaged_summary_without_gpx_is_preserved_across_months(self):
        before = datetime(2026, 11, 1, 6, 59, 59, tzinfo=timezone.utc)
        after = datetime(2026, 11, 1, 7, 0, 1, tzinfo=timezone.utc)
        gpx, summary = self.saved_pair(self.untimed_upload(before))
        gpx.unlink()
        summary.write_bytes(b"[1]")
        self.assertEqual(self.untimed_upload(after).status_code, 409)
        self.assertEqual(summary.read_bytes(), b"[1]")
        self.assertEqual(list(self.root.glob("*/*.gpx")), [])

    def test_untimed_ambiguous_existing_months_are_not_modified(self):
        before = datetime(2026, 11, 1, 6, 59, 59, tzinfo=timezone.utc)
        after = datetime(2026, 12, 1, 8, 0, 1, tzinfo=timezone.utc)
        gpx, summary = self.saved_pair(self.untimed_upload(before))
        duplicate = self.root / "2026-11"
        duplicate.mkdir()
        (duplicate / gpx.name).write_bytes(gpx.read_bytes())
        (duplicate / summary.name).write_bytes(summary.read_bytes())
        original = {p: p.read_bytes() for p in self.root.glob("*/*")}
        self.assertEqual(self.untimed_upload(after).status_code, 409)
        self.assertEqual({p: p.read_bytes() for p in self.root.glob("*/*")}, original)

    def test_untimed_lookup_io_error_does_not_create_another_copy(self):
        before = datetime(2026, 11, 1, 6, 59, 59, tzinfo=timezone.utc)
        after = datetime(2026, 11, 1, 7, 0, 1, tzinfo=timezone.utc)
        gpx, summary = self.saved_pair(self.untimed_upload(before))
        original = (gpx.read_bytes(), summary.read_bytes())
        real_stat = Path.stat

        def stat(path, *args, **kwargs):
            if path == gpx:
                raise PermissionError("denied")
            return real_stat(path, *args, **kwargs)

        with mock.patch.object(Path, "stat", stat):
            self.assertEqual(self.untimed_upload(after).status_code, 500)
        self.assertEqual((gpx.read_bytes(), summary.read_bytes()), original)
        self.assertEqual(len(list(self.root.glob("*/*.gpx"))), 1)

    def run_workers(self, payloads, headers=None, instants=None):
        ctx = multiprocessing.get_context("spawn")
        barrier = ctx.Barrier(len(payloads))
        results = ctx.Queue()
        processes = [ctx.Process(target=upload_worker,
                                 args=(str(self.root), payload, barrier, results, headers, instant))
                     for payload, instant in zip(payloads, instants or [None] * len(payloads))]
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

    def test_concurrent_untimed_uploads_choose_one_month_under_the_lock(self):
        headers = {k: v for k, v in HEADERS.items() if k != "X-Trip-Id"}
        instants = [datetime(2026, 11, 1, 6, 59, 59, tzinfo=timezone.utc),
                    datetime(2026, 11, 1, 7, 0, 1, tzinfo=timezone.utc)] * 2
        responses = self.run_workers([body(timed=False)] * 4, headers, instants)
        self.assertEqual([status for status, _ in responses], [200] * 4, responses)
        self.assertEqual(len({record["stored_as"] for _, record in responses}), 1)
        self.assertEqual(len(list(self.root.glob("*/*.gpx"))), 1)
        self.assertEqual(len(list(self.root.glob("*/*.json"))), 1)


if __name__ == "__main__":
    unittest.main()

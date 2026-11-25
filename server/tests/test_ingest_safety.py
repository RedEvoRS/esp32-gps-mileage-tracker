"""Regression tests for upload integrity and untrusted GPX values."""

import importlib.util
import errno
import hashlib
import json
import multiprocessing
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from month_link_layouts import (GLOBAL_UPLOAD_LAYOUTS, MONTH_LINK_LAYOUTS, MONTH_PROBLEM_LAYOUTS, PERMISSION_MONTH_LAYOUTS,
                                WRITABLE_MONTH_LAYOUTS, make_layout, restricted_layout)


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

    def symlink(self, link, target, directory=False):
        try:
            link.symlink_to(target, target_is_directory=directory)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink creation unavailable: {type(exc).__name__}")

    def assert_storage_error(self, target):
        with self.assertLogs(self.ing.app.logger, "WARNING"):
            response = self.upload()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json(), {"error": "server error writing trip"})
        self.assertNotIn(str(target), response.get_data(as_text=True))

    def test_symlinked_target_month_cannot_write_outside_data_root(self):
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside)
            self.symlink(self.root / "2026-04", target, directory=True)
            real_stat = Path.stat
            outside_stats = []

            def stat(path, *args, **kwargs):
                if target in path.parents or path.parent == self.root / "2026-04":
                    outside_stats.append(path)
                    raise AssertionError("outside trip stat")
                return real_stat(path, *args, **kwargs)

            with mock.patch.object(Path, "stat", stat):
                with self.assertLogs(self.ing.app.logger, "ERROR"):
                    response = self.upload()
            self.assertEqual(response.status_code, 503)
            self.assertNotIn(str(target), response.get_data(as_text=True))
            self.assertEqual(outside_stats, [])
            self.assertEqual(list(target.iterdir()), [])

    @unittest.skipUnless(os.name == "posix", "POSIX no-follow upload lock")
    def test_upload_lock_symlink_never_creates_or_changes_its_target(self):
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / "private-lock"
            for present in (False, True):
                with self.subTest(present=present):
                    if present:
                        target.write_bytes(b"outside-lock-sentinel")
                        before = target.stat()
                    link = self.root / ".upload.lock"
                    self.symlink(link, target)
                    try:
                        self.assert_storage_error(target)
                        self.assertEqual(list(self.root.iterdir()), [link])
                        self.assertTrue(link.is_symlink())
                        if present:
                            self.assertEqual(target.read_bytes(), b"outside-lock-sentinel")
                            self.assertEqual(target.stat().st_mtime_ns, before.st_mtime_ns)
                        else:
                            self.assertFalse(target.exists())
                    finally:
                        link.unlink()

    def test_symlinked_other_month_refuses_without_trip_stat(self):
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside)
            (target / "2026-04-14T14-30-00Z.gpx").write_bytes(b"outside-trip-sentinel")
            self.symlink(self.root / "2026-03", target, directory=True)
            real_stat = Path.stat
            outside_stats = []

            def stat(path, *args, **kwargs):
                if target in path.parents or path.parent == self.root / "2026-03":
                    outside_stats.append(path)
                    raise AssertionError("outside trip stat")
                return real_stat(path, *args, **kwargs)

            with mock.patch.object(Path, "stat", stat), self.assertLogs(self.ing.app.logger, "ERROR") as logs:
                response = self.upload()
            self.assertEqual(response.status_code, 503)
            self.assertIn("2026-03", " ".join(logs.output))
            self.assertNotIn(str(target), " ".join(logs.output) + response.get_data(as_text=True))
            self.assertEqual(outside_stats, [])
            self.assertEqual(sorted(p.name for p in self.root.iterdir()), [".upload.lock", "2026-03"])
            self.assertEqual([p.read_bytes() for p in target.iterdir()], [b"outside-trip-sentinel"])

    def test_linked_month_retry_and_recovery(self):
        first = self.untimed_upload(datetime(2026, 3, 15, tzinfo=timezone.utc))
        self.assertEqual(first.status_code, 200)
        original = self.root / "2026-03"
        saved = self.root / "saved"
        original.rename(saved)
        with tempfile.TemporaryDirectory() as outside:
            self.symlink(original, Path(outside), directory=True)
            with self.assertLogs(self.ing.app.logger, "ERROR"):
                retry = self.untimed_upload(datetime(2026, 4, 15, tzinfo=timezone.utc))
            self.assertEqual(retry.status_code, 503)
            self.assertFalse((self.root / "2026-04").exists())
            self.assertEqual(self.upload(b"invalid").status_code, 400)
            from test_ingest_diagnostics import batch
            events = self.client.post("/v1/device-events", json=batch(), headers=HEADERS)
            self.assertEqual(events.status_code, 200)
            original.unlink()
        saved.rename(original)
        retry = self.untimed_upload(datetime(2026, 4, 15, tzinfo=timezone.utc))
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.json["stored_as"], first.json["stored_as"])
        self.assertFalse((self.root / "2026-04").exists())

    def test_internal_month_link_and_non_month_link(self):
        inside = self.root / "inside"
        inside.mkdir()
        self.symlink(self.root / "2026-03", inside, directory=True)
        with tempfile.TemporaryDirectory() as outside:
            self.symlink(self.root / "not-a-month", Path(outside), directory=True)
            self.assertEqual(self.upload().status_code, 200)

    def test_internal_alias_month_link_retry_is_not_a_conflict(self):
        # #725: a month link to another month folder in the data folder made
        # the same folder match twice, so a plain retry got 409.
        first = self.upload()
        self.assertEqual(first.status_code, 200)
        month = Path(first.json["stored_as"]).parent
        self.symlink(self.root / "2099-12", self.root / month, directory=True)
        before = sorted(p.name for p in (self.root / month).iterdir())
        retry = self.upload()
        self.assertEqual(retry.status_code, 200, retry.get_data(as_text=True))
        self.assertEqual(retry.json["stored_as"], first.json["stored_as"])
        self.assertEqual(sorted(p.name for p in (self.root / month).iterdir()), before)

    def test_new_trip_into_aliased_month_refuses(self):
        # #727: a new trip whose month folder links to another month folder
        # would be filed, and shown, under that other month.
        real = self.root / "2026-03"
        real.mkdir()
        self.symlink(self.root / "2026-04", real, directory=True)
        with self.assertLogs(self.ing.app.logger, "ERROR"):
            response = self.upload()
        self.assertEqual(response.status_code, 503)
        self.assertIn("month folder 2026-04", response.json["error"])
        self.assertEqual(list(real.iterdir()), [])

    def test_new_month_upload_statuses_match_shared_link_layouts(self):
        for label, layout in MONTH_LINK_LAYOUTS.items():
            root = self.root / label
            make_layout(root, layout, self.symlink)
            ingest = load_ingest(root)
            for month, status in layout["uploads"].items():
                with self.subTest(layout=label, month=month):
                    payload = body().replace(b"2026-04-14", (month + "-14").encode())
                    before = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
                    with mock.patch.object(ingest, "datetime", wraps=datetime) as clock:
                        clock.now.return_value = datetime(2027, 1, 15, tzinfo=timezone.utc)
                        response = ingest.app.test_client().post("/upload", data=payload, headers=HEADERS)
                    self.assertEqual(response.status_code, status, response.get_json())
                    if status == 503:
                        self.assertIn(f"month folder {month}", response.get_json()["error"])
                        after = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.name != ".upload.lock")
                        self.assertEqual(after, [p for p in before if p != ".upload.lock"])

    def test_new_trip_into_same_named_internal_link_is_stored(self):
        archive = self.root / "archive" / "2026-04"
        archive.mkdir(parents=True)
        self.symlink(self.root / "2026-04", archive, directory=True)
        response = self.upload()
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertTrue(any(p.suffix == ".gpx" for p in archive.iterdir()))

    def test_unrelated_month_uploads_match_shared_global_block_layouts(self):
        for label, layout in GLOBAL_UPLOAD_LAYOUTS.items():
            root = self.root / label
            make_layout(root, layout, self.symlink)
            ingest = load_ingest(root)
            with self.subTest(layout=label):
                response = ingest.app.test_client().post("/upload", data=body(), headers=HEADERS)
                self.assertEqual(response.status_code, 503 if layout["blocked"] else 200, response.get_json())
                if layout["blocked"]:
                    self.assertIn("month folder 2027-", response.get_json()["error"])
                    self.assertFalse((root / "2026-04").exists())
                    # Even a retry for an existing ID in an unrelated month
                    # must refuse until every unsafe month link is repaired.
                    (root / "2026-04").mkdir()
                    (root / "2026-04/2026-04-14T14-30-00Z.gpx").write_bytes(body())
                    response = ingest.app.test_client().post("/upload", data=body(), headers=HEADERS)
                    self.assertEqual(response.status_code, 503)
                    for name, _ in layout["links"]:
                        (root / name).unlink()
                    self.assertEqual(ingest.app.test_client().post("/upload", data=body(), headers=HEADERS).status_code, 200)

    def test_month_problems_match_shared_viewer_layouts(self):
        for label, layout in MONTH_PROBLEM_LAYOUTS.items():
            root = self.root / label
            make_layout(root, layout, self.symlink)
            ingest = load_ingest(root)
            for month, status in layout["uploads"].items():
                payload = body().replace(b"2026-04-14", (month + "-14").encode())
                with self.subTest(layout=label, month=month):
                    response = ingest.app.test_client().post("/upload", data=payload, headers=HEADERS)
                    self.assertEqual(response.status_code, status, response.get_json())
                    if status == 503:
                        self.assertIn(f"month folder {month}", response.get_json()["error"])

    def test_month_resolution_permission_error_refuses_all_uploads(self):
        month = self.root / "2026-03"
        month.mkdir()
        original = Path.resolve

        def denied(path, *args, **kwargs):
            if path == month:
                raise PermissionError("synthetic resolution denial")
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, "resolve", denied):
            response = self.upload()
        self.assertEqual(response.status_code, 503)
        self.assertIn("month folder 2026-03", response.get_json()["error"])

    @unittest.skipIf(os.name == "nt" or getattr(os, "geteuid", lambda: 0)() == 0, "requires non-root POSIX permissions")
    def test_permission_layouts_refuse_same_and_unrelated_month_until_repaired(self):
        for label, layout in PERMISSION_MONTH_LAYOUTS.items():
            root = self.root / label
            with restricted_layout(root, layout, self.symlink):
                ingest = load_ingest(root)
                for month in ("2026-02", "2026-04"):
                    payload = body().replace(b"2026-04-14", (month + "-14").encode())
                    with self.subTest(layout=label, month=month), self.assertLogs(ingest.app.logger, "ERROR") as logs:
                        response = ingest.app.test_client().post("/upload", data=payload, headers=HEADERS)
                        self.assertEqual(response.status_code, 503, response.get_json())
                        self.assertIn("month folder 2026-02", response.get_json()["error"])
                        self.assertIn("owner or mode", response.get_json()["error"])
                        self.assertEqual(len(logs.records), 1)
                        self.assertIsNone(logs.records[0].exc_info)
                        self.assertFalse((root / "2026-04").exists())
            # The same files, without a service restart or data deletion.
            for month in ("2026-02", "2026-04"):
                payload = body().replace(b"2026-04-14", (month + "-14").encode())
                self.assertEqual(ingest.app.test_client().post("/upload", data=payload, headers=HEADERS).status_code, 200)

    def test_month_scan_classifies_filesystem_errors_at_each_call(self):
        month = self.root / "2026-03"
        month.mkdir()
        gpx = month / "2026-04-14T14-30-00Z.gpx"
        for method, target in (("resolve", month), ("stat", month), ("resolve", gpx), ("stat", gpx),
                               ("iterdir", self.root)):
            original = getattr(Path, method)
            for code in (errno.EACCES, errno.EPERM, errno.EIO, errno.EMFILE, errno.ESTALE):
                def fail(path, *args, target=target, code=code, original=original, **kwargs):
                    if path == target:
                        raise OSError(code, "synthetic filesystem failure")
                    return original(path, *args, **kwargs)
                with self.subTest(method=method, target=target.name, errno=code), \
                        mock.patch.object(Path, method, fail), self.assertLogs(self.ing.app.logger, "ERROR"):
                    response = self.upload()
                expected = 503 if code in (errno.EACCES, errno.EPERM) and target != self.root else 500
                self.assertEqual(response.status_code, expected, response.get_json())
                if expected == 503:
                    self.assertIn("month folder 2026-03", response.get_json()["error"])

    @unittest.skipIf(os.name == 'nt' or getattr(os, 'geteuid', lambda: 0)() == 0, 'requires non-root POSIX permissions')
    def test_unwritable_month_refuses_only_its_uploads_without_partial_files_and_recovers(self):
        for label, layout in WRITABLE_MONTH_LAYOUTS.items():
            root = self.root / label
            with restricted_layout(root, layout, self.symlink):
                ingest = load_ingest(root)
                client = ingest.app.test_client()
                for month, status in layout['uploads'].items():
                    payload = body().replace(b'2026-04-14', (month + '-14').encode())
                    if status == 503:
                        with self.assertLogs(ingest.app.logger, 'ERROR') as logs:
                            response = client.post('/upload', data=payload, headers=HEADERS)
                        self.assertEqual(len(logs.records), 1)
                        self.assertIsNone(logs.records[0].exc_info)
                        self.assertEqual(response.get_json()['error'],
                                         f'month folder {month} cannot be written to; new trips for this month are refused '
                                         'until its owner and permissions are fixed')
                        self.assertEqual(list((root / month).iterdir()), [])
                    else:
                        response = client.post('/upload', data=payload, headers=HEADERS)
                    self.assertEqual(response.status_code, status)
            self.assertEqual(client.post('/upload', data=body().replace(b'2026-04-14', b'2026-02-14'),
                                         headers=HEADERS).status_code, 200)
            self.assertEqual(len(list((root / '2026-02').iterdir())), 2)

    def test_temp_creation_permission_refusal_preserves_disk_and_io_errors_as_500(self):
        for code, status in ((errno.EACCES, 503), (errno.EPERM, 503), (errno.ENOSPC, 500), (errno.EIO, 500)):
            with self.subTest(errno=code), mock.patch.object(self.ing.tempfile, 'mkstemp',
                                                           side_effect=OSError(code, 'synthetic write failure')), \
                    self.assertLogs(self.ing.app.logger, 'ERROR') as logs:
                response = self.upload()
            self.assertEqual(response.status_code, status)
            self.assertEqual(len(logs.records), 1)
            self.assertIsNone(logs.records[0].exc_info)
            self.assertEqual(list((self.root / '2026-04').iterdir()), [])

    def test_loop_refuses_when_resolve_leaves_it_unresolved(self):
        month = self.root / "2026-03"
        self.symlink(month, month, directory=True)
        original = Path.resolve

        def unresolved(path, *args, **kwargs):
            return path if path == month else original(path, *args, **kwargs)

        with mock.patch.object(Path, "resolve", unresolved):
            response = self.upload()
        self.assertEqual(response.status_code, 503)
        self.assertIn("month folder 2026-03", response.get_json()["error"])

    def test_dangling_outside_month_link_refuses(self):
        with tempfile.TemporaryDirectory() as outside:
            self.symlink(self.root / "2026-03", Path(outside) / "missing", directory=True)
            with self.assertLogs(self.ing.app.logger, "ERROR"):
                self.assertEqual(self.upload().status_code, 503)

    def test_new_trip_into_nonmonth_link_refuses_until_real_folder_restored(self):
        archive = self.root / "archive"
        archive.mkdir()
        (archive / "existing.gpx").write_bytes(b"existing-trip-sentinel")
        month = self.root / "2026-04"
        self.symlink(month, archive, directory=True)
        with self.assertLogs(self.ing.app.logger, "ERROR"):
            response = self.upload()
        self.assertEqual(response.status_code, 503)
        self.assertIn("2026-04", response.get_json()["error"])
        self.assertEqual([p.name for p in archive.iterdir()], ["existing.gpx"])
        self.assertEqual((archive / "existing.gpx").read_bytes(), b"existing-trip-sentinel")
        month.unlink()
        archive.rename(month)
        response = self.upload()
        self.assertEqual(response.status_code, 200)
        self.assertTrue((month / "existing.gpx").exists())

    def test_looping_month_link_refuses_with_the_folder_named(self):
        # #724: a link loop raised RuntimeError from resolve() and gave a
        # generic 500 whose log didn't name the folder.
        self.symlink(self.root / "2026-03", self.root / "2026-03", directory=True)
        with self.assertLogs(self.ing.app.logger, "ERROR") as logs:
            response = self.upload()
        self.assertEqual(response.status_code, 503)
        self.assertIn("2026-03", response.get_json()["error"])
        self.assertIn("month folder 2026-03", "\n".join(logs.output))

    def test_looping_trip_file_is_not_reported_as_a_linked_month(self):
        # Only a looping month folder is a "linked month"; a looping trip file
        # inside a real month keeps the generic storage error.
        first = self.upload()
        self.assertEqual(first.status_code, 200)
        gpx = self.root / first.json["stored_as"]
        loop = gpx.with_name("loop.gpx")
        gpx.rename(loop)
        self.symlink(gpx, gpx)
        with self.assertLogs(self.ing.app.logger, "ERROR"):
            response = self.upload()
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("month folder", response.get_data(as_text=True))

    def test_final_trip_symlinks_cannot_escape_data_root(self):
        month = self.root / "2026-04"
        month.mkdir()
        with tempfile.TemporaryDirectory() as outside:
            for suffix in (".gpx", ".json"):
                for present in (False, True):
                    with self.subTest(suffix=suffix, present=present):
                        target = Path(outside) / ("private" + suffix)
                        if present:
                            target.write_bytes(b"outside-trip-sentinel")
                        link = month / ("2026-04-14T14-30-00Z" + suffix)
                        self.symlink(link, target)
                        try:
                            with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("outside read")) as read, \
                                    mock.patch.object(self.ing, "_atomic_write_bytes",
                                                      side_effect=AssertionError("trip written")) as write:
                                self.assert_storage_error(target)
                            read.assert_not_called()
                            write.assert_not_called()
                            self.assertTrue(link.is_symlink())
                            self.assertEqual(list(month.iterdir()), [link])
                            if present:
                                self.assertEqual(target.read_bytes(), b"outside-trip-sentinel")
                                target.unlink()
                            else:
                                self.assertFalse(target.exists())
                        finally:
                            link.unlink()

    def test_rmc_whole_and_fractional_epochs_across_midnight(self):
        # Invented stationary coordinates; only the timestamp format matters.
        epochs = ["2026-04-14T23:59:59.990Z", "2026-04-15T00:00:00Z",
                  "2026-04-15T00:00:01.010Z", "2026-04-15T00:00:02.250Z"]
        track = "".join(
            '<trkpt lat="0" lon="0"><time>' + epoch
            + '</time><speed>10</speed><hdop>1</hdop></trkpt>' for epoch in epochs)
        payload = ('<gpx version="1.0" xmlns="http://www.topografix.com/GPX/1/0">'
                   '<trk><trkseg>' + track + '</trkseg></trk></gpx>').encode()
        parsed = self.ing.parse_gpx(payload)["segments"][0]
        times = [point["time"] for point in parsed]
        self.assertEqual([t.isoformat() for t in times], [
            "2026-04-14T23:59:59.990000+00:00", "2026-04-15T00:00:00+00:00",
            "2026-04-15T00:00:01.010000+00:00", "2026-04-15T00:00:02.250000+00:00"])
        for index, step in enumerate([0.01, 1.01, 1.24]):
            self.assertAlmostEqual((times[index + 1] - times[index]).total_seconds(), step)
        response = self.upload(payload)
        self.assertEqual(response.status_code, 200)
        gpx, summary = self.saved_pair(response)
        self.assertEqual(gpx.read_bytes(), payload)
        self.assertEqual(json.loads(summary.read_bytes())["timing_status"], "complete")

    def test_identical_retry_preserves_original_record(self):
        response = self.upload()
        self.assertEqual(response.status_code, 200)
        gpx, summary = self.saved_pair(response)
        before = (gpx.read_bytes(), summary.read_bytes())
        self.assertEqual(self.upload().status_code, 200)
        self.assertEqual((gpx.read_bytes(), summary.read_bytes()), before)

    def test_identical_retry_preserves_a_missing_receipt_time(self):
        payload = body(lat='0', lon='0')
        _, summary = self.saved_pair(self.upload(payload))
        saved = json.loads(summary.read_bytes())
        del saved['_received_at']
        summary.write_text(json.dumps(saved), encoding='utf-8')

        self.assertEqual(self.upload(payload).status_code, 200)
        self.assertNotIn('_received_at', json.loads(summary.read_bytes()))

    def test_summary_only_recovery_requires_the_original_gpx(self):
        original = body(lat="0", lon="0")
        gpx, summary = self.saved_pair(self.upload(original))
        before = summary.read_bytes()
        gpx.unlink()

        changed_track = b'lon="0.0001"'.join(original.rsplit(b'lon="0"', 1))
        self.assertEqual(self.upload(changed_track).status_code, 409)
        self.assertEqual(self.upload(original + b"\n").status_code, 409)
        self.assertFalse(gpx.exists())
        self.assertEqual(summary.read_bytes(), before)

        retry = self.upload(original)
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(self.saved_pair(retry), (gpx, summary))
        self.assertEqual(gpx.read_bytes(), original)
        self.assertEqual(summary.read_bytes(), before)
        self.assertEqual(json.loads(before)["gpx_sha256"], hashlib.sha256(original).hexdigest())

    def test_legacy_summary_without_gpx_requires_a_backup_copy(self):
        gpx, summary = self.saved_pair(self.upload())
        record = json.loads(summary.read_bytes())
        record.pop("gpx_sha256", None)
        summary.write_text(json.dumps(record), encoding="utf-8")
        before = summary.read_bytes()
        gpx.unlink()

        retry = self.upload()
        self.assertEqual(retry.status_code, 409)
        self.assertIn("no GPX digest", retry.json["error"])
        self.assertFalse(gpx.exists())
        self.assertEqual(summary.read_bytes(), before)

        gpx.write_bytes(body())
        self.assertEqual(self.upload().status_code, 200)
        self.assertIn("gpx_sha256", json.loads(summary.read_bytes()))

    def test_different_payload_cannot_overwrite_existing_trip(self):
        gpx, summary = self.saved_pair(self.upload())
        before = (gpx.read_bytes(), summary.read_bytes())
        self.assertEqual(self.upload(body(3)).status_code, 409)
        self.assertEqual((gpx.read_bytes(), summary.read_bytes()), before)

    def test_device_and_sanitized_id_collisions_preserve_existing_trip(self):
        response = self.upload(body(timed=False), **{"X-Trip-Id": "trip:id"})
        gpx, summary = self.saved_pair(response)
        before = (gpx.read_bytes(), summary.read_bytes())
        for headers in ({"X-Trip-Id": "trip_id"},
                        {"X-Trip-Id": "trip:id", "X-Device-Id": "other"}):
            self.assertEqual(self.upload(body(timed=False), **headers).status_code, 409)
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
                                 ("2026-04-14T14-30-00Z", "test-device", 2))
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
        response = self.upload(body(timed=False), **{"X-Trip-Id": "caf\u00e9-trip"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Path(response.get_json()["stored_as"]).stem, "caf_-trip")

    def test_custom_trip_ids_keep_the_same_filename_sanitizing(self):
        for trip_id, filename in (("AZaz09_-", "AZaz09_-"), ("../private", "___private"),
                                  ("C:\\private\\trip", "C__private_trip"),
                                  ("\\\\host\\share", "__host_share"),
                                  ("caf\u00e9-trip", "caf_-trip")):
            with self.subTest(trip_id=trip_id):
                response = self.upload(body(timed=False), **{"X-Trip-Id": trip_id})
                self.assertEqual(response.status_code, 200)
                gpx, summary = self.saved_pair(response)
                self.assertEqual(gpx.stem, filename)
                self.assertEqual(gpx.parent.parent, self.root)
                self.assertEqual(json.loads(summary.read_bytes())["trip_id"], trip_id)

    def test_trip_path_checks_reject_root_and_sibling_prefix(self):
        root = self.root.resolve()
        for path in (root, root.parent / (root.name + "-other") / "drive.gpx",
                     root / ".." / "private.gpx"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.ing._checked_trip_path(root, path)

    def test_linked_data_root_and_internal_trip_links_still_work(self):
        with tempfile.TemporaryDirectory() as aliases:
            alias = Path(aliases) / "linked-data"
            self.symlink(alias, self.root, directory=True)
            with mock.patch.object(self.ing, "DATA_ROOT", alias):
                response = self.upload()
            self.assertEqual(response.status_code, 200)
            gpx, summary = self.saved_pair(response)
            link = gpx.parent / "linked.gpx"
            self.symlink(link, gpx)
            self.assertEqual(self.ing._checked_trip_path(self.root.resolve(), alias / link.relative_to(self.root)),
                             gpx.resolve())
            self.assertTrue(summary.exists())

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
                raise OSError(errno.EIO, "synthetic disk error")
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
                     for payload, instant in zip(payloads, instants or [None] * len(payloads), strict=True)]
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

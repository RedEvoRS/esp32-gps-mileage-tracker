"""Regressions from the ingest/diagnostics October 2026 audit."""

import io
import json
import os
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from contextlib import closing
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from test_ingest_safety import load_ingest, APP_PATH
from test_ingest_diagnostics import batch, HEADERS


def gpx(instant="2026-01-01T00:00:00Z", *, creator="test", metadata=None, speed="1"):
    stamp = f"<time>{instant}</time>" if instant else ""
    meta = f"<metadata><time>{metadata}</time></metadata>" if metadata else ""
    return (f'<gpx creator="{creator}">{meta}<trk><trkseg><trkpt lat="{0}" lon="{0}">'
            f"{stamp}<speed>{speed}</speed></trkpt></trkseg></trk></gpx>").encode()


class IngestAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ing = load_ingest(self.root)
        self.client = self.ing.app.test_client()

    def upload(self, raw, **headers):
        return self.client.post("/upload", data=raw, headers=HEADERS | headers)

    def assert_no_trip(self):
        self.assertEqual(list(self.root.glob("*/*.gpx")), [])
        self.assertEqual(list(self.root.glob("*/*.json")), [])
        self.assertFalse((self.root / ".upload.lock").exists())

    def test_dtd_entities_rejected_in_utf8_and_utf16_before_expansion(self):
        for dtd in ('<!DOCTYPE gpx>', '<!DOCTYPE gpx [<!ENTITY a "expanded">]>',
                    '<!DOCTYPE gpx SYSTEM "file:///nonexistent-audit-file">'):
            for encoding in ("utf-8", "utf-16"):
                with self.subTest(dtd=dtd, encoding=encoding):
                    raw = (f'<?xml version="1.0" encoding="{encoding}"?>' + dtd
                           + gpx().decode()).encode(encoding)
                    self.assertEqual(self.upload(raw).status_code, 400)
        # No DTD is required by GPX; normal UTF-16 stays supported.
        raw = ('<?xml version="1.0" encoding="UTF-16"?>' + gpx().decode()).encode("utf-16")
        self.assert_no_trip()
        self.assertEqual(self.upload(raw).status_code, 200)

    def test_upload_text_limits(self):
        for raw, headers in ((gpx(creator="x" * 129), {}),
                             (gpx(), {"X-Device-Id": "x" * 129}),
                             (gpx(None), {"X-Trip-Id": "x" * 129})):
            self.assertEqual(self.upload(raw, **headers).status_code, 400)
        self.assert_no_trip()
        self.assertEqual(self.upload(gpx(creator="x" * 128), **{"X-Device-Id": "x" * 32}).status_code, 200)

    def test_unknown_encoding_and_derived_overflow_write_nothing(self):
        for raw in (b'<?xml version="1.0" encoding="invalid-encoding"?>' + gpx(),
                    gpx(speed="1e308"), gpx("9999-12-31T23:59:59-23:59")):
            with self.subTest(raw=raw[:60]):
                self.assertEqual(self.upload(raw).status_code, 400)
                self.assert_no_trip()

    def test_serialization_is_validated_before_storage(self):
        summary = self.ing.summarize(self.ing.parse_gpx(gpx()))
        summary["distance_miles"] = float("inf")
        with mock.patch.object(self.ing, "summarize", return_value=summary):
            self.assertEqual(self.upload(gpx()).status_code, 400)
        self.assert_no_trip()

    def test_derived_distance_overflow_is_refused(self):
        from common import trip_summary
        parsed = self.ing.parse_gpx(gpx())
        parsed["segments"][0] *= 3
        with mock.patch.object(trip_summary, "_step", return_value=(1e308, None)):
            with self.assertRaisesRegex(ValueError, "finite"):
                self.ing.summarize(parsed)

    def test_point_and_metadata_time_window_accepts_old_backlog(self):
        now = datetime(2026, 10, 23, tzinfo=timezone.utc)
        with mock.patch.object(self.ing, "datetime", wraps=datetime) as clock:
            clock.now.return_value = now
            for instant in ("0001-01-01T00:00:00Z", "1999-12-31T23:59:59Z",
                            (now + timedelta(days=1, microseconds=1)).isoformat(),
                            "9999-12-31T23:59:59Z"):
                for raw in (gpx(instant), gpx(None, metadata=instant)):
                    self.assertEqual(self.upload(raw).status_code, 400)
                    self.assert_no_trip()
            for instant in ("2000-01-01T00:00:00Z", "2001-01-01T00:00:00Z",
                            (now + timedelta(days=1)).isoformat()):
                self.assertEqual(self.upload(gpx(instant)).status_code, 200)

    def test_time_validation_includes_points_skipped_for_missing_coordinates(self):
        raw = gpx().replace(b"</trkseg>", b"<trkpt><time>0001-01-01T00:00:00Z</time></trkpt></trkseg>")
        self.assertEqual(self.upload(raw).status_code, 400)
        self.assert_no_trip()

    def test_timezone_change_reuses_timed_trip_folder_and_receipt(self):
        self.ing.DISPLAY_TZ = ZoneInfo("America/Chicago")
        raw = gpx("2026-07-01T02:50:00Z")
        first = self.upload(raw)
        self.assertEqual(first.status_code, 200)
        saved = self.root / first.json["stored_as"]
        before = saved.with_suffix(".json").read_bytes()
        self.assertEqual(saved.parent.name, "2026-06")
        self.ing.DISPLAY_TZ = ZoneInfo("Asia/Tokyo")
        retry = self.upload(raw)
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.json["stored_as"], first.json["stored_as"])
        self.assertEqual(saved.with_suffix(".json").read_bytes(), before)
        self.assertEqual(len(list(self.root.glob("*/*.gpx"))), 1)

    def test_timed_preclaim_header_is_ignored_and_untimed_header_retained(self):
        claimed = "2026-01-01T00-00-00Z"
        junk = self.upload(gpx("2026-01-02T00:00:00Z"), **{"X-Trip-Id": claimed})
        self.assertEqual(junk.status_code, 200)
        self.assertNotEqual(junk.json["trip_id"], claimed)
        real = self.upload(gpx())
        self.assertEqual(real.status_code, 200)
        self.assertEqual(real.json["trip_id"], claimed)
        for instant, expected in ((None, "manual-id"), ("2026-01-03T00:00:00Z", "2026-01-03T00-00-00Z")):
            response = self.upload(gpx(instant), **{"X-Trip-Id": "manual-id"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["trip_id"], expected)

    def test_untimed_header_cannot_preclaim_timestamp_namespace(self):
        response = self.upload(gpx(None), **{"X-Trip-Id": "2026-01-01T00-00-00Z"})
        self.assertEqual(response.status_code, 400)
        self.assert_no_trip()
        self.assertEqual(self.upload(gpx()).status_code, 200)

    def test_split_pieces_with_shared_boundary_get_distinct_timestamp_ids(self):
        # Firmware reseeds a size-split piece with the preceding piece's last
        # fix. Local NVS filenames are irrelevant to the server identity.
        point = '<trkpt lat="{lat}" lon="{lon}"><time>{}</time><speed>1</speed></trkpt>'
        ids = []
        for start, end in ((0, 10), (10, 20)):
            raw = ('<gpx><trk><trkseg>' + ''.join(point.format(f"2026-01-01T00:00:{s:02d}Z", lat=0, lon=0)
                   for s in (start, end)) + '</trkseg></trk></gpx>').encode()
            response = self.upload(raw)
            self.assertEqual(response.status_code, 200)
            ids.append(response.json["trip_id"])
        self.assertEqual(ids, ["2026-01-01T00-00-00Z", "2026-01-01T00-00-10Z"])

    def test_token_startup_rejects_whitespace_and_controls_without_echo(self):
        for token in ("", "   ", "bad token", "bad\ttoken", "bad\ntoken", "bad\x7ftoken", "bad\u0085token", "bad\x80token"):
            env = dict(os.environ, INGEST_AUTH_TOKEN=token, INGEST_DATA_ROOT=str(self.root), DISPLAY_TZ="UTC")
            probe = subprocess.run([sys.executable, "-c", "import runpy; runpy.run_path(" + repr(str(APP_PATH)) + ")"],
                                   env=env, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(probe.returncode, 0)
            self.assertIn("INGEST_AUTH_TOKEN", probe.stderr)
            if token.strip():
                self.assertNotIn(token, probe.stderr)

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits")
    def test_private_files_are_readable_by_same_uid_viewer(self):
        # Exercise ingest startup with a permissive parent umask.
        previous = os.umask(0)
        self.addCleanup(os.umask, previous)
        root = self.root / "data"
        ing = load_ingest(root)
        response = ing.app.test_client().post("/upload", data=gpx(), headers=HEADERS)
        self.assertEqual(response.status_code, 200)
        ing.DIAGNOSTICS_STORE.refresh_contact("tracker-1")
        saved = root / response.json["stored_as"]
        for directory in (root, saved.parent, root / "diagnostics"):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        for path in (saved, saved.with_suffix(".json"), ing.DIAGNOSTICS_STORE.path):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertTrue(path.read_bytes())
        with closing(sqlite3.connect(ing.DIAGNOSTICS_STORE.path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            self.assertEqual(db.execute("SELECT device_id FROM devices").fetchone(), ("tracker-1",))

    @unittest.skipUnless(hasattr(signal, "setitimer"), "POSIX sync-worker drain deadline")
    def test_reject_drains_interrupt_stalled_and_trickling_reads(self):
        class Slow(io.BytesIO):
            def read(self, size):
                time.sleep(0.3)
                return super().read(size)

        class Trickle(io.BytesIO):
            def read(self, size):
                time.sleep(0.02)
                return super().read(1)

        for drain in (self.ing._discard_body, self.ing._discard_diagnostics_body):
            for stream_type in (Slow, Trickle):
                with self.subTest(drain=drain.__name__, stream=stream_type.__name__):
                    stream = stream_type(b"x" * 30)
                    old_handler = signal.getsignal(signal.SIGALRM)
                    with self.ing.app.test_request_context(environ_overrides={"wsgi.input": stream, "CONTENT_LENGTH": "30"}), \
                            mock.patch.object(self.ing, "DISCARD_SECONDS", 0.05, create=True):
                        started = time.monotonic()
                        drain()
                        self.assertLess(time.monotonic() - started, 0.2)
                    self.assertEqual(signal.getsignal(signal.SIGALRM), old_handler)
                    self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))


class DiagnosticsAuditTests(unittest.TestCase):
    setUp = IngestAuditTests.setUp

    def post(self, body):
        return self.client.post("/v1/device-events", json=body, headers=HEADERS)

    def query(self, sql):
        with closing(sqlite3.connect(self.ing.DIAGNOSTICS_STORE.path)) as db:
            return db.execute(sql).fetchall()

    def test_excessive_snapshot_step_preserves_health_and_acknowledges_events(self):
        self.assertEqual(self.post(batch()).status_code, 200)
        forged = batch((1 << 63) - 1, 2)
        forged["checkin"]["active"] = []
        response = self.post(forged)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["accepted"], [{"boot_id": batch()["events"][0]["boot_id"], "sequence": 2}])
        self.assertEqual(self.query("SELECT status_sequence FROM devices"), [(1,)])
        self.assertIn((4005, "active"), self.query("SELECT code,state FROM device_conditions"))
        self.assertEqual(self.post(batch(2, 3)).status_code, 200)
        self.assertEqual(self.query("SELECT status_sequence FROM devices"), [(2,)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM events"), [(3,)])

    def test_step_boundary_first_contact_and_reenrollment(self):
        limit = 1_000_000
        body = batch(limit + 1)
        self.assertEqual(self.post(body).status_code, 200)
        self.assertEqual(self.query("SELECT status_sequence FROM devices"), [(None,)])
        for revision in (limit, 2 * limit, 2 * limit + 1):
            body = batch(revision)
            body["events"] = []
            self.assertEqual(self.post(body).status_code, 200)
            self.assertEqual(self.query("SELECT status_sequence FROM devices"), [(revision,)])
        too_far = batch(3 * limit + 2)
        too_far["events"] = []
        self.assertEqual(self.post(too_far).status_code, 200)
        self.assertEqual(self.query("SELECT status_sequence FROM devices"), [(2 * limit + 1,)])

    def test_admin_reset_is_compare_and_set_clears_health_and_preserves_history(self):
        self.assertEqual(self.post(batch()).status_code, 200)
        script = APP_PATH.with_name("reset_high_water.py")
        env = dict(os.environ, INGEST_DATA_ROOT=str(self.root), PYTHONPATH=str(APP_PATH.parents[1]))
        def reset(expected, value):
            return subprocess.run([sys.executable, str(script), "--device-id", "tracker-1",
                "--high-water", str(value), "--expect-high-water", str(expected)],
                env=env, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(reset(2, 0).returncode, 0)
        self.assertEqual(self.query("SELECT status_sequence FROM devices"), [(1,)])
        self.assertEqual(reset(1, 500).returncode, 0)
        self.assertEqual(self.query("SELECT status_sequence,snapshot_json,snapshot_received_at FROM devices"), [(500, None, None)])
        self.assertEqual(self.query("SELECT DISTINCT state,episode_id FROM device_conditions"), [("unknown", None)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM events"), [(1,)])
        self.assertEqual(self.post(batch(501, 2)).status_code, 200)
        self.assertEqual(self.query("SELECT status_sequence FROM devices"), [(501,)])
        self.assertNotEqual(reset(501, (1 << 63) - 1).returncode, 0)


if __name__ == "__main__":
    unittest.main()

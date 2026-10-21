"""Server stamp provenance, public token validation and service rendering."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from werkzeug.security import generate_password_hash

from test_round_trip import SERVER, load_service

sys.path.insert(0, str(SERVER))
from common.version import server_version, validate_version
from stamp_version import stamp


class VersionValidation(unittest.TestCase):
    def test_safe_ascii_token_and_length(self):
        for value in ("v0.01", "v0.01+abcdef1234", "v0.01+abcdef1234-dirty",
                      "unversioned", "a" * 64):
            self.assertEqual(validate_version(value), value)
        for value in (None, 1, [], "", "a" * 65, " v0.01", "v0.01\n",
                      "v0.01\r", "v0.01\x00", "v0.01/other", "<script>",
                      "v0.01\u2028", "v\u00e9", "-version"):
            with self.subTest(value=value):
                self.assertEqual(validate_version(value), "unversioned")

    def test_missing_environment_is_unversioned(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(server_version(), "unversioned")


@unittest.skipUnless(shutil.which("git"), "needs Git")
class VersionStamp(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.server = self.root / "server"
        self.server.mkdir()
        self.version = self.server / "VERSION"
        self.version.write_text("v0.01\n", encoding="ascii")
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "core.autocrlf", "false")
        self.git("add", ".")
        self.git("-c", "commit.gpgsign=false", "commit", "-qm", "Test fixture")
        self.commit = self.git("rev-parse", "HEAD")[:10]
        self.warnings = []

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root,
                                       stderr=subprocess.DEVNULL, text=True).strip()

    def stamp(self):
        return stamp(self.server, warn=self.warnings.append)

    def test_clean_and_untracked(self):
        self.assertEqual(self.stamp(), "v0.01+" + self.commit)
        (self.root / "untracked").write_text("not tracked")
        self.assertEqual(self.stamp(), "v0.01+" + self.commit)
        self.assertEqual(self.warnings, [])

    def test_tracked_changes_anywhere_and_staged_changes(self):
        outside = self.root / "tracked"
        outside.write_text("before")
        self.git("add", ".")
        self.git("-c", "commit.gpgsign=false", "commit", "-qm", "Fixture file")
        self.commit = self.git("rev-parse", "HEAD")[:10]
        outside.write_text("after")
        self.assertEqual(self.stamp(), "v0.01+" + self.commit + "-dirty")
        self.git("add", ".")
        self.assertEqual(self.stamp(), "v0.01+" + self.commit + "-dirty")
        outside.unlink()
        self.assertEqual(self.stamp(), "v0.01+" + self.commit + "-dirty")

    def test_crlf_and_invalid_release(self):
        self.version.write_bytes(b" \tv0.01\r\n")
        self.assertEqual(self.stamp(), "v0.01+" + self.commit + "-dirty")
        for value in (b"v0.1", b"v00.01", b"v100.01", b"<script>", b"v0.01\xff"):
            self.version.write_bytes(value)
            self.assertEqual(self.stamp(), "unversioned")
        self.version.unlink()
        self.assertEqual(self.stamp(), "unversioned")
        self.assertTrue(self.warnings)

    def test_git_missing_or_failing_falls_back_with_reason(self):
        for failure in (FileNotFoundError(), subprocess.CalledProcessError(128, "git"),
                        subprocess.TimeoutExpired("git", 10)):
            with patch("stamp_version.subprocess.check_output", side_effect=failure):
                self.assertEqual(self.stamp(), "v0.01")
        self.assertTrue(all("Git unavailable" in reason for reason in self.warnings))

    def test_copied_directory_does_not_borrow_unrelated_git_identity(self):
        copied = self.root / "copied-server"
        copied.mkdir()
        (copied / "VERSION").write_text("v0.01")
        self.assertEqual(stamp(copied, warn=self.warnings.append), "v0.01")
        self.assertTrue(self.warnings)

    def test_without_checkout(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "VERSION").write_text("v0.01")
            self.assertEqual(stamp(folder, warn=self.warnings.append), "v0.01")
        self.assertTrue(self.warnings)


class ServiceVersions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name)
        with patch.dict(os.environ, {
            "INGEST_DATA_ROOT": str(root / "data"), "INGEST_AUTH_TOKEN": "test-token",
            "VIEWER_DB_PATH": str(root / "annotations.db"), "VIEWER_USERNAME": "driver",
            "VIEWER_PASSWORD_HASH": generate_password_hash("test-password"),
            "VIEWER_SECRET_KEY": "version-test-session-key-" * 3, "DISPLAY_TZ": "UTC",
        }):
            cls.ingest = load_service("mileage-ingest")
            cls.viewer = load_service("mileage-viewer")
        month = cls.viewer.DATA_ROOT / "2026-10"
        month.mkdir(parents=True)
        (month / "sample.json").write_text(json.dumps({
            "trip_id": "sample", "start_time": "2026-10-01T12:00:00Z",
            "distance_miles": 1, "duration_seconds": 600, "num_points": 2,
        }))
        (month / "sample.gpx").write_text("<gpx/>")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_health_unchanged_status_and_service_with_validated_version(self):
        for value, expected in (("v0.01+abcdef1234-dirty", "v0.01+abcdef1234-dirty"),
                                ("", "unversioned"), ("<script>", "unversioned")):
            with patch.dict(os.environ, SERVER_VERSION=value):
                for name, module in (("mileage-ingest", self.ingest), ("mileage-viewer", self.viewer)):
                    response = module.app.test_client().get("/health")
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json, {"status": "ok", "service": name, "version": expected})

    def test_footers_on_all_pages_including_login(self):
        for value, expected in (("v0.01+abcdef1234", "v0.01+abcdef1234"),
                                ("<script>", "unversioned")):
            with patch.dict(os.environ, SERVER_VERSION=value):
                client = self.viewer.app.test_client()
                routes = ("/login", "/", "/trip/2026-10/sample", "/device-health")
                for route in routes:
                    if route != "/login":
                        with client.session_transaction() as session:
                            session.update(user="driver", pw_tag=self.viewer._password_tag())
                    response = client.get(route)
                    self.assertEqual(response.status_code, 200)
                    html = response.get_data(as_text=True)
                    self.assertEqual(html.count(f'<footer class="server-version">Server {expected}</footer>'), 1)

    def test_server_next_to_firmware_on_device_health(self):
        client = self.viewer.app.test_client()
        with client.session_transaction() as session:
            session.update(user="driver", pw_tag=self.viewer._password_tag())
        data = {"state": "ok", "devices": [{
            "device_id": "test-device", "snapshot": {"firmware_version": "v0.06+0123456789"},
            "groups": {},
        }], "events": [], "has_next": False}
        with patch.dict(os.environ, SERVER_VERSION="v0.01+abcdef1234"), \
                patch.object(self.viewer, "read_diagnostics", return_value=data):
            html = client.get("/device-health").get_data(as_text=True)
        self.assertRegex(html, r"<dt>Firmware</dt><dd>v0.06\+0123456789</dd>\s*<dt>Server</dt><dd>v0.01\+abcdef1234</dd>")

    def test_footer_on_http_errors_preserves_status_and_headers(self):
        client = self.viewer.app.test_client()
        with client.session_transaction() as session:
            session.update(user="driver", pw_tag=self.viewer._password_tag())
        with patch.dict(os.environ, SERVER_VERSION="v0.01"):
            response = client.get("/trip/2026-10/missing")
            self.assertEqual(response.status_code, 404)
            self.assertIn('<footer class="server-version">Server v0.01</footer>', response.text)
            response = client.put("/health")
            self.assertEqual(response.status_code, 405)
            self.assertIn("GET", response.headers["Allow"])
            self.assertIn('<footer class="server-version">Server v0.01</footer>', response.text)
            with patch.object(self.viewer, "read_diagnostics", return_value={"state": "unavailable"}):
                response = client.get("/device-health")
            self.assertEqual(response.status_code, 503)
            self.assertIn('<footer class="server-version">Server v0.01</footer>', response.text)


class BuildVersionWiring(unittest.TestCase):
    def test_all_python_images_receive_stamp_without_git_context(self):
        compose = (SERVER / "docker-compose.yml").read_text()
        self.assertEqual(compose.count("SERVER_VERSION: ${SERVER_VERSION:-unversioned}"), 3)
        for name in ("mileage-ingest", "mileage-viewer", "mileage-monitor"):
            dockerfile = (SERVER / name / "Dockerfile").read_text()
            self.assertIn("ARG SERVER_VERSION=unversioned\nENV SERVER_VERSION=${SERVER_VERSION}", dockerfile)
            self.assertIn("COPY common/ /app/common/", dockerfile)
        self.assertIn("!common/version.py", (SERVER / ".dockerignore").read_text())

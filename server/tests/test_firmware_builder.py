"""Builder safety and output contract; no Docker or third-party modules needed."""

import hashlib
from contextlib import closing
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch


HERE = Path(__file__).resolve().parents[1] / "firmware-builder"


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = load("builder")
checker = load("check_pins")


class FirmwareBuilderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = dict(ssid="example-ssid", password='example-"password\\', bssid="",
                             device_id="test-01", host="tracker.example", port="8090", token="a" * 64)

    def test_ci_pins_and_consumers_match(self):
        self.assertEqual(checker.check_repository(), [])

    def test_pin_drift_fails(self):
        workflow = (checker.ROOT / ".github/workflows/audit-tests.yml").read_text()
        pins = (HERE / "pins.env").read_text().replace("ARDUINO_CLI_VERSION=1.5.1", "ARDUINO_CLI_VERSION=0.0.0")
        self.assertIn("ARDUINO_CLI_VERSION differs from firmware-compile",
                      checker.check(workflow, pins, (HERE / "Dockerfile").read_text(), (HERE / "builder.py").read_text()))

    def test_settings_boundaries_and_injection(self):
        builder.validate_settings(self.settings)
        for key, values in {"device_id": ["A", "-abc", "a" * 33, "a\nb"],
                            "bssid": ["AA:BB", "GG:00:00:00:00:00"],
                            "host": ["http://example", "example:8090", "0.0.0.0", "bad/host", ""],
                            "port": ["0", "65536", "-1", "1;echo"], "ssid": ["", "x" * 33, "x\ny"],
                            "token": ["", "short"], "password": ["x\ny"]}.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    builder.validate_settings(dict(self.settings, **{key: value}))
        for device in ("a", "a" * 32, "a_0-z"):
            builder.validate_settings(dict(self.settings, device_id=device))
        builder.validate_settings(dict(self.settings, bssid=":".join(["AB"] * 6), password=""))
        self.assertEqual(builder.c_string('"\\é'), '"\\042\\134\\303\\251"')

    def test_headers_all_defines_and_no_enrollment_by_default(self):
        header = builder.secrets_header(self.settings)
        for define in ("WIFI_SSID", "WIFI_PASSWORD", "SERVER_HOST", "SERVER_PORT", "AUTH_TOKEN", "DEVICE_ID", "USE_BSSID_FILTER", "WIFI_BSSID"):
            self.assertIn(define, header)
        self.assertNotIn("DIAGNOSTICS_ENROLLMENT", header)
        self.assertNotIn(self.settings["password"], header)
        header = builder.secrets_header(self.settings, True, 0)
        self.assertIn("DIAGNOSTICS_ENROLLMENT_ID 1ULL", header)
        self.assertIn("DIAGNOSTICS_STATUS_HIGH_WATER 0ULL", header)
        with self.assertRaises(ValueError):
            builder.secrets_header(self.settings, True, None)

    def test_env_is_data_never_shell(self):
        path = self.root / ".env"
        path.write_text("INGEST_AUTH_TOKEN='literal$token'\nBIND_ADDR=tracker.example\n")
        self.assertEqual(builder.env_value(path, "INGEST_AUTH_TOKEN"), "literal$token")
        path.write_text('INGEST_AUTH_TOKEN="$(touch injected)"\n')
        with self.assertRaises(ValueError):
            builder.env_value(path, "INGEST_AUTH_TOKEN")

    def test_high_water_absent_null_existing_and_failed_reads(self):
        self.assertEqual(builder.read_high_water(self.root, "test-01"), dict(exists=False, high_water=0))
        (self.root / "diagnostics").mkdir()
        path = self.root / "diagnostics/events.db"
        with closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE devices(device_id TEXT, status_sequence INTEGER)")
            db.executemany("INSERT INTO devices VALUES (?,?)", [("test-01", None), ("test-02", 19), ("test-03", 0)])
            db.commit()
        before = path.read_bytes()
        self.assertEqual(builder.read_high_water(self.root, "test-01"), dict(exists=False, high_water=0))
        self.assertEqual(builder.read_high_water(self.root, "test-02"), dict(exists=True, high_water=19))
        self.assertEqual(builder.read_high_water(self.root, "test-03"), dict(exists=True, high_water=0))
        self.assertEqual(path.read_bytes(), before)
        path.write_bytes(b"corrupt database")
        with self.assertRaises(sqlite3.Error):
            builder.read_high_water(self.root, "test-01")
        with self.assertRaises(OSError):
            builder.read_high_water(self.root / "missing", "test-01")

    def test_enrollment_requires_fresh_card_unused_id_and_verified_read(self):
        builder.write_json(self.root / "plan.json", {"device_id": "test-01"})
        builder.private_write(self.root / "normal.h", builder.secrets_header(self.settings))
        with self.assertRaises(OSError):
            builder.enrollment(self.root, True)
        for exists, fresh, new, expected in ((False, "yes", "yes", "enroll"),
                                            (False, "yes", "no", "normal"),
                                            (False, "no", "yes", "normal"),
                                            (True, "yes", "yes", "normal")):
            builder.write_json(self.root / "high-water.json", {"exists": exists, "high_water": 0})
            with patch.dict(os.environ, TRACKER_FRESH_SD=fresh, TRACKER_NEW_BOARD=new):
                builder.enrollment(self.root, True)
            self.assertEqual(builder.read_json(self.root / "plan.json")["kind"], expected)

    def test_partition_limit_uses_smallest_app_slot(self):
        path = self.root / "partitions.csv"
        path.write_text("# comment\nfactory,app,factory,0x10000,1536K,\nota,0x00,ota_0,,1M,\ndata,data,spiffs,,2M,\n")
        self.assertEqual(builder.partition_limit(path), 1024 * 1024)

    def test_commit_prefers_git_and_falls_back_to_archive(self):
        commit = "0123456789" * 4
        (self.root / "firmware").mkdir()
        path = self.root / "firmware/COMMIT"
        path.write_text("$Format:%H$")
        with patch.object(builder.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, commit + "\n")):
            self.assertEqual(builder.source_commit(self.root), commit)
        with patch.object(builder.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "")):
            with self.assertRaises(ValueError):
                builder.source_commit(self.root)
            path.write_text(commit + "\n")
            self.assertEqual(builder.source_commit(self.root), commit)

    def test_high_water_includes_committed_wal_without_writing(self):
        (self.root / "diagnostics").mkdir()
        path = self.root / "diagnostics/events.db"
        with closing(sqlite3.connect(path)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("CREATE TABLE devices(device_id TEXT, status_sequence INTEGER)")
            writer.execute("INSERT INTO devices VALUES ('test-01', 37)")
            writer.commit()
            before = {p.name: p.read_bytes() for p in path.parent.iterdir() if not p.name.endswith("-shm")}
            self.assertEqual(builder.read_high_water(self.root, "test-01"), dict(exists=True, high_water=37))
            self.assertEqual({p.name: p.read_bytes() for p in path.parent.iterdir() if not p.name.endswith("-shm")}, before)

    def test_publish_normal_removes_stale_cleanup_and_restores_on_failure(self):
        private = self.root / "private"
        (private / "result/current").mkdir(parents=True)
        manifest = {"version": "v0.08+0123456789", "kind": "normal"}
        builder.write_json(private / "result/current/manifest.json", manifest)
        (self.root / "firmware-out/cleanup").mkdir(parents=True)
        (self.root / "firmware-out/cleanup/old").write_text("previous")
        original = Path.rename

        def fail_publish(path, target):
            if path == private / "result":
                raise OSError("simulated publication failure")
            return original(path, target)

        with patch.object(Path, "rename", fail_publish), self.assertRaises(OSError):
            builder.publish(self.root, private)
        self.assertEqual((self.root / "firmware-out/cleanup/old").read_text(), "previous")
        builder.publish(self.root, private)
        self.assertFalse((self.root / "firmware-out/cleanup").exists())
        self.assertEqual(builder.read_json(self.root / "firmware-out/current/manifest.json"), manifest)

    def test_manifest_uses_generated_offsets_flags_and_output_hashes(self):
        build, exported, output = self.root / "build", self.root / "export", self.root / "output"
        build.mkdir(); exported.mkdir()
        version, commit = "v0.08+0123456789", "0123456789" * 4
        mapping = [("0x0", "sketch.ino.bootloader.bin"), ("0x9000", "sketch.ino.partitions.bin"),
                   ("0xf000", "boot_app0.bin"), ("0x20000", "sketch.ino.bin")]
        (build / "flash_args").write_text("--flash-mode qio --flash-freq 40m --flash-size 8MB\n" +
                                           "\n".join(" ".join(part) for part in mapping))
        for _, filename in mapping:
            (exported / filename).write_bytes(b"fake image " + version.encode())
        (exported / "boot_app0.bin").rename(build / "boot_app0.bin")
        args = (output, build, exported, {"build.project_name": "sketch.ino", "build.mcu": "esp32s3"},
                {"device_id": "test-01"}, version, commit, "example-fqbn", "enroll")
        manifest = builder.write_manifest(*args)
        self.assertEqual(manifest["flash_mode"], "qio")
        self.assertEqual(manifest["flash_freq"], "40m")
        self.assertEqual(manifest["app_offset"], "0x20000")
        self.assertEqual(manifest["schema"], 1)
        self.assertEqual(len(manifest["parts"]), 4)
        for part in manifest["parts"]:
            self.assertEqual(part["sha256"], hashlib.sha256((output / part["file"]).read_bytes()).hexdigest())
        self.assertEqual(json.loads((output / "manifest.json").read_text()), manifest)
        if os.name != "nt":
            self.assertEqual(output.stat().st_mode & 0o777, 0o700)
            for path in output.iterdir():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        (build / "flash_args").write_text("--flash-mode dio\n")
        with self.assertRaises(ValueError):
            builder.write_manifest(*args)


if __name__ == "__main__":
    unittest.main()

"""Builder safety and output contract; no Docker or third-party modules needed."""

import hashlib
from contextlib import closing
import importlib.util
import json
import os
from pathlib import Path
import re
import select
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

try:
    import pty
    import termios
except ImportError:
    pty = termios = None


HERE = Path(__file__).resolve().parents[1] / "firmware-builder"


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = load("builder")
checker = load("check_pins")


@unittest.skipUnless(pty is not None and termios is not None, "needs POSIX pty and termios")
class FirmwareBuilderTerminalTests(unittest.TestCase):
    def run_prompt(self, secret, response, default=""):
        # fork() gives the child a controlling terminal, so ask() opens the
        # actual /dev/tty rather than a mock or the test runner's stdin.
        read_fd, write_fd = os.pipe()
        pid, master = pty.fork()
        if pid == 0:
            os.close(read_fd)
            try:
                fd = os.open("/dev/tty", os.O_RDWR)
                before = termios.tcgetattr(fd)
                result = {}
                try:
                    result["answer"] = builder.ask("Test prompt", default, secret)
                except BaseException as exc:
                    result["error"] = type(exc).__name__
                result["restored"] = termios.tcgetattr(fd) == before
                os.close(fd)
                os.write(write_fd, json.dumps(result).encode())
            finally:
                os._exit(0)

        os.close(write_fd)
        transcript = b""
        payload = b""
        sent = False
        deadline = time.monotonic() + 5
        try:
            while time.monotonic() < deadline:
                ready, _, _ = select.select([master, read_fd], [], [], 0.01)
                if master in ready:
                    try:
                        transcript += os.read(master, 4096)
                    except OSError:  # Linux reports EIO when the slave closes.
                        pass
                if read_fd in ready:
                    chunk = os.read(read_fd, 4096)
                    if not chunk:
                        break
                    payload += chunk
                if not sent and b": " in transcript:
                    # The prompt is written before ECHO is disabled. Wait for
                    # the actual terminal setting before typing a secret.
                    echo = bool(termios.tcgetattr(master)[3] & termios.ECHO)
                    if echo == (not secret):
                        os.write(master, response)
                        sent = True
            else:
                self.fail("ask() did not finish within five seconds")
            self.assertTrue(payload, "prompt child returned no result")
            result = json.loads(payload)
            self.assertTrue(sent, result)
            self.assertTrue(result["restored"], result)
            return result, transcript
        finally:
            # Always reap the child, including assertion failures/timeouts.
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(pid, 0)
            os.close(master)
            os.close(read_fd)

    def test_ask_normal_and_default_on_real_terminal(self):
        result, transcript = self.run_prompt(False, "example-é\n".encode())
        self.assertEqual(result.get("answer"), "example-é")
        self.assertIn("example-é".encode(), transcript)
        result, _ = self.run_prompt(False, b"\n", "example-default")
        self.assertEqual(result.get("answer"), "example-default")

    def test_ask_secret_has_no_echo_on_real_terminal(self):
        result, transcript = self.run_prompt(True, b"example-password\n")
        self.assertEqual(result.get("answer"), "example-password")
        self.assertNotIn(b"example-password", transcript)

    def test_ask_secret_restores_terminal_on_interrupt_and_eof(self):
        for response, error in ((b"\x03", "KeyboardInterrupt"), (b"\x04", "ValueError")):
            with self.subTest(error=error):
                result, _ = self.run_prompt(True, response)
                self.assertEqual(result.get("error"), error)


class FirmwareBuilderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = dict(ssid="example-ssid", password='example-"password\\', bssid="",
                             device_id="test-01", host="tracker.example", port="8090", token="a" * 64)

    def test_ci_pins_and_consumers_match(self):
        self.assertEqual(checker.check_repository(), [])

    def install_layer(self):
        dockerfile = (HERE / "Dockerfile").read_text()
        # Ignore comments before joining Dockerfile continuation lines.
        lines = [line for line in dockerfile.splitlines() if not line.lstrip().startswith("#")]
        instructions = re.split(r"\n(?=[A-Z]+ )", "\n".join(lines).replace("\\\n", ""))
        return next(part for part in instructions if "arduino-cli core install" in part)

    def test_tool_pruning_is_in_install_layer(self):
        layer = self.install_layer()
        self.assertIn("esp-x32|esp32s3-libs|esptool_py", layer)
        for token in ("for tool in /opt/arduino/packages/esp32/tools/*",
                      'rm -rf -- "$tool"', "/opt/arduino/packages/arduino/tools/dfu-util",
                      "rm -rf /opt/arduino/staging/*"):
            self.assertIn(token, layer)
            self.assertGreater(layer.index(token), layer.index("arduino-cli core install"))

    @unittest.skipUnless(shutil.which("sh"), "POSIX sh required for Dockerfile pruning execution")
    def test_pruning_keeps_complete_s3_packages_and_removes_unused_bytes(self):
        arduino = self.root / "arduino"
        tools = arduino / "packages/esp32/tools"
        kept = {"esp-x32", "esp32s3-libs", "esptool_py"}
        removed = {"esp-rv32", "esp32-libs", "esp32c3-libs", "esp32c5-libs", "esp32c6-libs",
                   "esp32h2-libs", "esp32p4-libs", "esp32p4_es-libs", "esp32s2-libs",
                   "xtensa-esp-elf-gdb", "riscv32-esp-elf-gdb", "openocd-esp32", "mkspiffs",
                   "mklittlefs", "future-chip-libs"}
        others = ["packages/arduino/tools/dfu-util", "packages/builtin/tools/ctags",
                  "packages/builtin/tools/serial-discovery", "staging/packages", "staging/libraries"]
        for path in [tools / name for name in kept | removed] + [arduino / name for name in others]:
            (path / "version/nested").mkdir(parents=True)
            (path / "version/nested/payload").write_bytes(b"must keep complete packages")
        layer = self.install_layer()
        prune = layer[layer.index("for tool in "):].replace("/opt/arduino", arduino.as_posix())
        subprocess.run(["sh", "-eu", "-c", prune], check=True, capture_output=True, text=True)
        self.assertEqual({p.name for p in tools.iterdir()}, kept)
        for name in kept:
            self.assertEqual((tools / name / "version/nested/payload").read_bytes(),
                             b"must keep complete packages")
        self.assertFalse((arduino / "packages/arduino/tools/dfu-util").exists())
        self.assertEqual(list((arduino / "staging").iterdir()), [])
        self.assertTrue((arduino / "packages/builtin/tools/ctags/version/nested/payload").is_file())
        self.assertTrue((arduino / "packages/builtin/tools/serial-discovery/version/nested/payload").is_file())

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

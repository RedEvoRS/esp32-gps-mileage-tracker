"""Builder safety and output contract; no Docker or third-party modules needed."""

import hashlib
from contextlib import closing
import importlib.util
import json
import os
from pathlib import Path
import re
import select
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
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


@unittest.skipUnless(os.name == "posix" and shutil.which("bash"), "needs POSIX bash")
class FirmwareBuilderShellTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.install = self.root / "install with spaces"
        self.scripts = self.install / "firmware-builder"
        shutil.copytree(HERE, self.scripts)
        (self.install / "mileage-ingest").mkdir()
        shutil.copyfile(HERE.parent / "mileage-ingest/Dockerfile", self.install / "mileage-ingest/Dockerfile")
        sketch = self.install / "firmware-src/firmware/04_trip_logger"
        sketch.mkdir(parents=True)
        (sketch / "04_trip_logger.ino").write_text("// fake source\n")
        (self.install / "data").mkdir()
        self.env_file = self.install / ".env"
        self.env_file.write_text("INGEST_AUTH_TOKEN='" + "a" * 64 + "'\nBIND_ADDR=tracker.example\n")
        self.env_file.chmod(0o600)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        # The tests fake root with FAKE_UID=0. Only real root can chown to
        # 1000:1000 (a non-root CI runner can't), so record chown calls instead.
        real_root = os.geteuid() == 0
        for command in ("bash", "dirname", "flock", "mktemp", "mkdir", "chmod", "find",
                        "grep", "awk", "cat", "mkfifo", "rm", "basename", "tr", "mv", "cp") \
                + (("chown",) if real_root else ()):
            target = shutil.which(command)
            self.assertIsNotNone(target, command)
            (self.bin / command).symlink_to(target)
        self.log = self.root / "docker.jsonl"
        self.env = dict(os.environ, PATH=str(self.bin), FAKE_LOG=str(self.log),
                        FAKE_INSTALL=str(self.install), FAKE_UID="0", MILEAGE_PLATFORM="unraid",
                        TRACKER_ACCEPT_DOWNLOAD="yes", TRACKER_WIFI_SSID="example-ssid",
                        TRACKER_WIFI_PASSWORD="example-password", TRACKER_DEVICE_ID="test-01",
                        TRACKER_SERVER_HOST="tracker.example", TRACKER_FRESH_SD="no")
        self.executable("id", "#!/bin/bash\nprintf '%s\\n' \"${FAKE_UID}\"\n")
        self.chown_log = self.root / "chown.log"
        if not real_root:
            self.executable("chown", "#!/bin/bash\nprintf '%s\\n' \"$*\" >> "
                            + shlex.quote(str(self.chown_log)) + "\n")
        for command in ("sudo", "apt-get", "dpkg", "systemctl", "journalctl"):
            self.executable(command, "#!/bin/bash\nprintf 'forbidden:%s\\n' \"$0\" >> \"$FAKE_LOG\"\nexit 98\n")
        self.executable("docker", "#!" + sys.executable + "\n" + r'''
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as stream:
    stream.write(json.dumps(args) + "\n")
if args[:2] == ["buildx", "version"]:
    sys.exit(int(os.environ.get("FAKE_NO_BUILDX", "0")))
if args[:2] == ["buildx", "inspect"]:
    print("Platforms: " + os.environ.get("FAKE_PLATFORMS", "linux/amd64, linux/arm64"))
    sys.exit(0)
if args[:2] == ["image", "inspect"]:
    sys.exit(1)
if args[0] == "build":
    sys.exit(0)
if args[:2] == ["rm", "-f"]:
    sys.exit(0)
assert args[0] == "run", args
mounts = {}
for i, value in enumerate(args):
    if value == "--mount":
        fields = dict(item.split("=", 1) for item in args[i + 1].split(",") if "=" in item)
        mounts[fields["dst"]] = Path(fields["src"])
install = Path(os.environ["FAKE_INSTALL"])
spec = importlib.util.spec_from_file_location("fake_builder", mounts.get("/builder/builder.py", install / "firmware-builder/builder.py"))
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)
os.umask(0o077)
if "python3" in args and "-" in args:
    result = subprocess.run([sys.executable, "-", str(mounts["/builder"])], stdin=sys.stdin)
    sys.exit(result.returncode)
if "configure" in args:
    if os.environ.get("FAKE_CONFIGURE_FAIL"):
        sys.exit(7)
    # Drain the real private FIFO and execute configure with the same input.
    with tempfile.TemporaryDirectory() as directory:
        view = Path(directory)
        (view / "firmware-src").symlink_to(install / "firmware-src", target_is_directory=True)
        (view / ".env").write_text(mounts["/install/.env"].read_text())
        builder.configure(view, mounts["/private"], True)
elif "high-water" in args:
    if os.environ.get("FAKE_HIGH_WATER_FAIL"):
        sys.exit(8)
    private = mounts["/private"]
    plan = builder.read_json(private / "plan.json")
    builder.write_json(private / "high-water.json", builder.read_high_water(mounts["/data"], plan["device_id"]))
elif "enrollment" in args:
    builder.enrollment(mounts["/private"], True)
elif "compile" in args:
    current = mounts["/result"] / "current"
    current.mkdir(mode=0o700)
    builder.write_json(current / "manifest.json", {"version": "v0.00+0123456789", "kind": "normal"})
    builder.private_write(current / "app.bin", "fake firmware")
elif "publish" in args:
    private = mounts["/private/result"].parent
    builder.publish(install, private, prepare=True)
else:
    raise AssertionError(args)
''')

    def executable(self, name, content):
        path = self.bin / name
        path.write_text(content)
        path.chmod(0o755)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def run_builder(self, *, host_python=False, interactive=False):
        if host_python:
            (self.bin / "python3").symlink_to(sys.executable)
        command = [str(self.bin / "bash"), str(self.scripts / "build.sh")]
        if not interactive:
            command.append("--non-interactive")
        if interactive:
            master, slave = pty.openpty()
            try:
                result = subprocess.run(command, env=self.env, stdin=slave, stdout=slave, stderr=slave, timeout=10)
                return result
            finally:
                os.close(master)
                os.close(slave)
        return subprocess.run(command, env=self.env, capture_output=True, text=True, timeout=10)

    def test_unraid_root_helpers_nonroot_with_host_python(self):
        result = self.run_builder(host_python=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        runs = [call for call in self.calls() if call[0] == "run"]
        self.assertEqual(len(runs), 2)
        for call in runs:
            self.assertEqual(call[call.index("--user") + 1], "1000:1000")
        self.assertIn("dst=/data,readonly", " ".join(runs[0]))
        self.assertEqual(self.env_file.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("a" * 64, result.stdout + result.stderr)
        for path in (self.install / "firmware-src", self.install / "firmware-out"):
            for item in [path, *path.rglob("*")]:
                self.assertEqual(item.stat().st_mode & 0o777, 0o700 if item.is_dir() else 0o600)
        self.assertEqual(list(self.install.glob(".firmware-build.*")), [])

    def test_debian_root_still_refused(self):
        self.env["MILEAGE_PLATFORM"] = "debian"
        result = self.run_builder()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("normal user", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_missing_python_uses_minimal_container_mounts_and_preserves_env(self):
        before = (self.env_file.stat().st_uid, self.env_file.stat().st_gid, self.env_file.stat().st_mode)
        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr.count("Firmware builder Python:"), 1)
        calls = self.calls()
        runs = [call for call in calls if call[0] == "run"]
        self.assertEqual(len(runs), 6)
        image = (self.install / "mileage-ingest/Dockerfile").read_text().splitlines()[0].split()[1]
        fallback = [call for call in runs if image in call]
        self.assertEqual(len(fallback), 4)
        for call in runs:
            self.assertEqual(call[call.index("--user") + 1], "1000:1000")
            self.assertEqual(call[call.index("--network") + 1], "none")
        for call in fallback:
            self.assertIn("-i", call)
            self.assertIn("-w", call)
            self.assertNotIn("-t", call)
            self.assertNotIn("a" * 64, " ".join(call))
            self.assertNotIn("src=" + str(self.scripts), " ".join(call))
        configure = next(call for call in fallback if "configure" in call)
        self.assertIn("env.pipe,dst=/install/.env,readonly", " ".join(configure))
        self.assertNotIn("src=" + str(self.env_file) + ",", " ".join(configure))
        publish = next(call for call in fallback if "publish" in call)
        self.assertIn("--prepare", publish)
        self.assertIn("dst=/private/result,readonly", " ".join(publish))
        self.assertNotIn("dst=/install,", " ".join(publish))
        self.assertEqual(before, (self.env_file.stat().st_uid, self.env_file.stat().st_gid, self.env_file.stat().st_mode))
        self.assertTrue((self.install / "firmware-out/current/app.bin").is_file())
        self.assertEqual(list(self.install.glob(".firmware-build.*")), [])

    def test_debian_fallback_helpers_keep_callers_uid(self):
        self.env.update(MILEAGE_PLATFORM="debian", FAKE_UID="1234")
        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stderr)
        for call in self.calls():
            if call[0] == "run":
                self.assertEqual(call[call.index("--user") + 1], "1234:1234")

    @unittest.skipUnless(pty is not None, "needs POSIX pty")
    def test_interactive_fallback_allocates_tty(self):
        result = self.run_builder(interactive=True)
        self.assertEqual(result.returncode, 0)
        for step in ("configure", "enrollment"):
            call = next(call for call in self.calls() if step in call)
            self.assertIn("-i", call)
            self.assertIn("-t", call)

    def test_failed_configure_cleans_blocked_env_writer(self):
        self.env["FAKE_CONFIGURE_FAIL"] = "1"
        result = self.run_builder()
        self.assertEqual(result.returncode, 7)
        self.assertEqual(list(self.install.glob(".firmware-build.*")), [])

    def test_optional_ntp_settings_reach_fallback_container(self):
        self.env.update(TRACKER_NTP_SERVER_1="clock.example", TRACKER_NTP_SERVER_2="192.0.2.123")
        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stderr)
        configure = next(call for call in self.calls() if call[0] == "run" and "configure" in call)
        for name in ("TRACKER_NTP_SERVER_1", "TRACKER_NTP_SERVER_2"):
            index = configure.index(name)
            self.assertEqual(configure[index - 1], "--env")
            self.assertNotIn(self.env[name], configure)  # values never become argv

    def test_fallback_publication_rolls_back_after_failed_rename(self):
        current = self.install / "firmware-out/current"
        current.mkdir(parents=True)
        (current / "old.bin").write_bytes(b"previous good firmware")
        self.bin.joinpath("mv").unlink()
        self.executable("mv", '#!/bin/bash\nif [[ $3 == */result ]]; then exit 33; fi\nexec /bin/mv "$@"\n')
        result = self.run_builder()
        self.assertEqual(result.returncode, 33, result.stderr)
        self.assertEqual((current / "old.bin").read_bytes(), b"previous good firmware")
        self.assertNotIn("is ready", result.stdout)
        self.assertEqual(list(self.install.glob(".firmware-build.*")), [])

    def test_fallback_publication_preserves_unexpected_existing_output(self):
        output = self.install / "firmware-out"
        output.mkdir()
        (output / "unrelated").write_text("keep")
        result = self.run_builder()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((output / "unrelated").read_text(), "keep")
        self.assertNotIn("is ready", result.stdout)

    def test_high_water_failure_never_proceeds_to_enrollment(self):
        self.env["FAKE_HIGH_WATER_FAIL"] = "1"
        result = self.run_builder()
        self.assertEqual(result.returncode, 8)
        self.assertFalse(any("enrollment" in call or "compile" in call for call in self.calls()))

    def test_unraid_private_source_rejects_symlinks_without_touching_target(self):
        outside = self.root / "outside"
        outside.write_text("unrelated")
        outside.chmod(0o644)
        (self.install / "firmware-src/link").symlink_to(outside)
        result = self.run_builder()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("without symlinks", result.stderr)
        self.assertEqual(outside.stat().st_mode & 0o777, 0o644)

    def test_buildx_and_amd64_checked_before_configuration(self):
        for variable, value, expected in (("FAKE_NO_BUILDX", "1", "Buildx/BuildKit"),
                                          ("FAKE_PLATFORMS", "linux/arm64", "linux/amd64")):
            with self.subTest(variable=variable):
                self.env[variable] = value
                result = self.run_builder()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected, result.stderr)
                self.assertIn("Unraid", result.stderr)
                self.assertFalse(any(call[0] == "run" for call in self.calls()))
                del self.env[variable]
                self.log.unlink()

    def test_unraid_detection_file_and_debian_override(self):
        version = self.root / "unraid-version"
        version.write_text('version="7.3.4"\n')
        self.env.pop("MILEAGE_PLATFORM")
        self.env["MILEAGE_UNRAID_VERSION_FILE"] = str(version)
        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stderr)


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

    def test_optional_ntp_validation_and_header(self):
        for values in ({}, {"ntp_server_1": "", "ntp_server_2": ""}):
            self.assertNotIn("#define NTP_SERVER_", builder.secrets_header(dict(self.settings, **values)))
        for key in ("ntp_server_1", "ntp_server_2"):
            for value in ("clock", "clock.example", "192.0.2.123", "a" * 63 + ".example"):
                with self.subTest(key=key, value=value):
                    header = builder.secrets_header(dict(self.settings, **{key: value}))
                    self.assertIn(f"#define {key.upper()} {builder.c_string(value)}\n", header)
                    other = "NTP_SERVER_2" if key.endswith("1") else "NTP_SERVER_1"
                    self.assertNotIn(f"#define {other}", header)
            for value in ("http://clock.example", "clock:123", "clock/path", "clock name", " clock",
                          "a" * 64 + ".example", ".clock", "clock..example", "-clock", "clock-",
                          "a." * 127, "::1", "0.0.0.0", "224.0.0.1", 'clock"', "clock\\",
                          "clock\n#define INJECTED 1", "clock\r", "clock\x00", "clöck.example"):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    builder.secrets_header(dict(self.settings, **{key: value}))

    def test_configure_reads_optional_ntp_in_both_modes_without_prompts(self):
        install, private = self.root / "install", self.root / "private"
        sketch = install / "firmware-src/firmware/04_trip_logger/04_trip_logger.ino"
        sketch.parent.mkdir(parents=True)
        sketch.write_text("// fake sketch\n")
        private.mkdir()
        (install / ".env").write_text("BIND_ADDR=tracker.example\nINGEST_AUTH_TOKEN=" + "a" * 64 + "\n")
        env = dict(TRACKER_ACCEPT_DOWNLOAD="yes", TRACKER_WIFI_SSID=self.settings["ssid"],
                   TRACKER_WIFI_PASSWORD=self.settings["password"], TRACKER_DEVICE_ID="test-01",
                   TRACKER_SERVER_HOST="tracker.example", TRACKER_NTP_SERVER_1="clock.example",
                   TRACKER_NTP_SERVER_2="192.0.2.123")
        answers = ["yes", self.settings["ssid"], self.settings["password"], "", "test-01", "tracker.example", "8090"]
        for noninteractive in (False, True):
            for optional in ({}, {"TRACKER_NTP_SERVER_1": "", "TRACKER_NTP_SERVER_2": ""},
                             {key: env[key] for key in ("TRACKER_NTP_SERVER_1", "TRACKER_NTP_SERVER_2")}):
                variables = {key: value for key, value in env.items() if not key.startswith("TRACKER_NTP_")}
                variables.update(optional)
                with self.subTest(noninteractive=noninteractive, optional=optional), \
                        patch.dict(os.environ, variables, clear=True), \
                        patch.object(builder, "ask", side_effect=answers) as ask:
                    builder.configure(install, private, noninteractive)
                    self.assertEqual(ask.call_count, 0 if noninteractive else 7)
                    expected = dict(self.settings, ntp_server_1=optional.get("TRACKER_NTP_SERVER_1", ""),
                                    ntp_server_2=optional.get("TRACKER_NTP_SERVER_2", ""))
                    self.assertEqual((private / "normal.h").read_text(), builder.secrets_header(expected))

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

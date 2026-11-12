"""Address lookup installer paths, using temporary files and no live services."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

SERVER = Path(__file__).resolve().parents[1]
HARNESS = r'''
source <(sed '$d' "$INSTALL")
SCRIPT_DIR=$SERVER
cd "$WORK"
trap settings_stop EXIT
MODE=${TEST_MODE:-install}

setup_geocoder
if [[ ${TEST_START:-no} == yes ]]; then
    check_port_free() { :; }
    compose() {
        if [[ $1 == up ]]; then
            grep '^VIEWER_GEOCODER_INTERVAL=' viewer.env
        fi
    }
    start_services
fi
printf 'FAILURES=%s\n' "$FAILURES"
'''


@unittest.skipIf(os.name == "nt" or shutil.which("bash") is None, "needs a POSIX shell")
class InstallerGeocoderTests(unittest.TestCase):
    def run_step(self, initial="KEEP=unchanged\n", answers="", interactive=False, **presets):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "viewer.env"
            path.write_text(initial)
            env = {k: v for k, v in os.environ.items() if not k.startswith("INSTALL_")}
            env.update(INSTALL=str(SERVER / "install.sh"), SERVER=str(SERVER), WORK=root,
                       INTERACTIVE="yes" if interactive else "no", **presets)
            if interactive:
                import pty
                master, slave = pty.openpty()
                try:
                    os.write(master, answers.encode())
                    process = subprocess.Popen(["bash", "-c", HARNESS], stdin=slave, env=env,
                                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    out, err = process.communicate(timeout=30)
                    result = subprocess.CompletedProcess(process.args, process.returncode, out, err)
                finally:
                    os.close(master)
                    os.close(slave)
            else:
                result = subprocess.run(["bash", "-c", HARNESS], input=answers, env=env,
                                        capture_output=True, text=True, timeout=30)
            return result, path.read_text(), path.stat().st_mode & 0o777

    def test_no_writes(self):
        for args in ({"INSTALL_GEOCODER": "none"}, {}, {"interactive": True, "answers": "n\n"}):
            result, text, _ = self.run_step(**args)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(text, "KEEP=unchanged\n")

    def test_self_hosted_and_permissions(self):
        for platform in ("debian", "unraid"):
            result, text, mode = self.run_step(INSTALL_GEOCODER="self-hosted",
                INSTALL_GEOCODER_URL="http://provider/reverse", INSTALL_GEOCODER_AUTO_LOOKUP="true",
                MILEAGE_PLATFORM=platform)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("VIEWER_GEOCODER_INTERVAL=0.1", text)
            self.assertIn("VIEWER_GEOCODER_AUTO_LOOKUP=true", text)
            self.assertIn("KEEP=unchanged", text)
            self.assertEqual(mode, 0o600)

        result, text, _ = self.run_step(interactive=True,
            answers="y\nself-hosted\nhttp://provider/reverse\nn\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("VIEWER_GEOCODER_AUTO_LOOKUP=false", text)
        self.assertIn("Turn on place suggestions?", result.stdout)
        self.assertIn("Look places up automatically when a trip opens?", result.stdout)

    def test_changed_settings_reach_service_start(self):
        result, _, _ = self.run_step(INSTALL_GEOCODER="self-hosted",
            INSTALL_GEOCODER_URL="http://provider/reverse", TEST_START="yes")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("| VIEWER_GEOCODER_INTERVAL=0.1", result.stdout)

    def test_third_party(self):
        result, text, _ = self.run_step(INSTALL_GEOCODER="third-party",
            INSTALL_GEOCODER_URL="https://example.com/reverse", INSTALL_GEOCODER_INTERVAL="1.1",
            INSTALL_GEOCODER_USER_AGENT="Tracker/1.0 (example.com)")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("VIEWER_GEOCODER_INTERVAL=1.1", text)
        self.assertIn("VIEWER_GEOCODER_AUTO_LOOKUP=false", text)
        for phrase in ("usage policy", "one request per second", "blocked", "every trip opened"):
            self.assertIn(phrase, result.stdout)

        result, text, _ = self.run_step(interactive=True,
            answers="y\nthird-party\nhttps://example.com/reverse\n\n\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("VIEWER_GEOCODER_INTERVAL=1.1", text)
        self.assertIn("VIEWER_GEOCODER_USER_AGENT='GPSMileageTracker/1.0'", text)

    def test_rerun_keeps(self):
        initial = "VIEWER_GEOCODER_URL='http://provider/reverse'\nVIEWER_GEOCODER_INTERVAL=0.1\n"
        for args in ({}, {"interactive": True, "answers": "n\n"}):
            result, text, _ = self.run_step(initial, **args)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(text, initial)

    def test_older_noninteractive(self):
        result, text, _ = self.run_step("VIEWER_GEOCODER_URL=http://provider/reverse\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("VIEWER_GEOCODER_INTERVAL=1.1", text)
        self.assertIn("INSTALL_GEOCODER=self-hosted", result.stdout)

    def test_older_interactive(self):
        result, text, _ = self.run_step("VIEWER_GEOCODER_URL=http://provider/reverse\n",
            interactive=True, answers="self-hosted\n\nn\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Provider kind", result.stdout)
        self.assertIn("VIEWER_GEOCODER_INTERVAL=0.1", text)

    def test_bad_presets_do_not_write(self):
        for values in ({"GEOCODER": "bad"}, {"GEOCODER_URL": "http://provider/reverse"},
                       {"GEOCODER": "none", "GEOCODER_INTERVAL": "1"},
                       {"GEOCODER_INTERVAL": "nan"}, {"GEOCODER_INTERVAL": "0.5"},
                       {"GEOCODER_INTERVAL": "61"}, {"GEOCODER_AUTO_LOOKUP": "yes"},
                       {"GEOCODER_AUTO_LOOKUP": "true"},
                       {"GEOCODER_USER_AGENT": "bad\nagent"},
                       {"GEOCODER_USER_AGENT": "\nTracker/1.0"},
                       {"GEOCODER_URL": "\nhttps://example.com/reverse"},
                       {"GEOCODER_URL": "https://nominatim.openstreetmap.org/reverse"},
                       {"GEOCODER_URL": "ftp://example.com/reverse"}):
            presets = {"INSTALL_GEOCODER": "third-party", "INSTALL_GEOCODER_URL": "https://example.com/reverse",
                       "INSTALL_GEOCODER_INTERVAL": "1.1", "INSTALL_GEOCODER_USER_AGENT": "Tracker/1.0"}
            if set(values) == {"GEOCODER_URL"} and values["GEOCODER_URL"].startswith("http://provider"):
                presets = {}
            presets.update({"INSTALL_" + k: v for k, v in values.items()})
            result, text, _ = self.run_step(**presets)
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(text, "KEEP=unchanged\n")

    def test_check_warning_and_report(self):
        for host, warning in (("example.com", True), ("provider", False), ("100.64.0.1", False),
                              ("[::1]", False)):
            initial = f"VIEWER_GEOCODER_URL=http://{host}/reverse\nVIEWER_GEOCODER_INTERVAL=0.1\n"
            result, text, _ = self.run_step(initial, TEST_MODE="check")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(text, initial)
            self.assertIn("Address lookup is on", result.stdout)
            self.assertEqual("under 1 second" in result.stdout, warning)
            self.assertIn("FAILURES=0", result.stdout)

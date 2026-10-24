"""Run the real opt-in proxy checks, URL selection and env rewrite in Bash."""

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
MODE=check
BIND_ADDR=$TEST_BIND
ip() {
    printf '2: eth0    inet 192.0.2.1/24 scope global eth0\n'
    printf '2: eth0    inet6 2001:db8::1/64 scope global eth0\n'
}
setup_viewer_proxy
set_service_urls
printf 'INGEST=%s\nVIEWER=%s\nFAILURES=%s\n' "$INGEST_URL" "$VIEWER_URL" "$FAILURES"
# Updating an unrelated setting must preserve all opt-in keys exactly.
env_set .env DISPLAY_TZ UTC
'''


@unittest.skipIf(os.name == "nt" or shutil.which("bash") is None, "needs a POSIX shell")
class InstallerProxyTests(unittest.TestCase):
    def run_check(self, bind="192.0.2.1", **settings):
        with tempfile.TemporaryDirectory() as root:
            envfile = Path(root) / ".env"
            original = "".join(f"{key}={value}\n" for key, value in settings.items())
            envfile.write_text(original)
            result = subprocess.run(["bash", "-c", HARNESS], env={**os.environ,
                "INSTALL": str(SERVER / "install.sh"), "SERVER": str(SERVER),
                "WORK": root, "TEST_BIND": bind}, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(envfile.read_text(), original + "DISPLAY_TZ=UTC\n")
            return result.stdout

    def test_defaults_follow_bind_address_or_loopback_health_target(self):
        for bind, target in (("192.0.2.1", "192.0.2.1"), ("", "127.0.0.1")):
            output = self.run_check(bind=bind)
            self.assertIn(f"VIEWER=http://{target}:8091", output)
            self.assertIn("FAILURES=0", output)

    def test_loopback_override_only_changes_viewer_and_preserves_settings(self):
        output = self.run_check(VIEWER_BIND_ADDR="127.0.0.1", VIEWER_COOKIE_SECURE="true",
                                VIEWER_TRUSTED_PROXIES=" 192.0.2.1,2001:db8::/64 ")
        self.assertIn("INGEST=http://192.0.2.1:8090", output)
        self.assertIn("VIEWER=http://127.0.0.1:8091", output)
        self.assertIn("FAILURES=0", output)
        self.assertNotIn("non-loopback address", output)

    def test_ipv6_loopback_and_assigned_addresses(self):
        for addr in ("::1", "2001:db8::1"):
            output = self.run_check(VIEWER_BIND_ADDR=addr)
            self.assertIn(f"VIEWER=http://[{addr}]:8091", output)
            self.assertIn("FAILURES=0", output)

    def test_invalid_or_unassigned_bind_address_is_a_problem(self):
        for addr in ("192.0.2.99", "2001:db8::99", "hostname", "0.0.0.0"):
            output = self.run_check(VIEWER_BIND_ADDR=addr)
            self.assertIn("FAILURES=1", output)
            self.assertIn("VIEWER_BIND_ADDR isn't an address", output)

    def test_secure_cookie_on_lan_or_wildcard_warns_without_failure(self):
        for bind in ("192.0.2.1", ""):
            output = self.run_check(bind=bind, VIEWER_COOKIE_SECURE="true")
            self.assertIn("non-loopback address", output)
            self.assertIn("FAILURES=0", output)

    def test_invalid_proxy_or_cookie_is_a_clear_problem(self):
        for settings, key in (({"VIEWER_TRUSTED_PROXIES": "bad"}, "VIEWER_TRUSTED_PROXIES"),
                              ({"VIEWER_COOKIE_SECURE": "yes"}, "VIEWER_COOKIE_SECURE")):
            output = self.run_check(**settings)
            self.assertIn("FAILURES=1", output)
            self.assertIn("Invalid viewer proxy configuration: " + key, output)

    def test_port_conflicts_follow_viewer_override_and_include_ipv6_wildcard(self):
        script = r'''
source <(sed '$d' "$INSTALL")
BIND_ADDR=192.0.2.1
VIEWER_BIND_ADDR=127.0.0.1
container_state() { printf 'stopped\n'; }
ss() { printf 'LISTEN 0 128 %s:8091 0.0.0.0:*\n' "$TEST_LISTEN"; }
check_port_free mileage-viewer 8091
printf 'PORT_FREE\n'
'''
        for listening, conflict in (("192.0.2.1", False), ("127.0.0.1", True),
                                    ("0.0.0.0", True), ("[::]", True)):
            result = subprocess.run(["bash", "-c", script], env={**os.environ,
                "INSTALL": str(SERVER / "install.sh"), "TEST_LISTEN": listening},
                capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 1 if conflict else 0, result.stdout + result.stderr)
            self.assertEqual("already in use" in result.stdout, conflict)

    def test_secure_cookie_skips_http_auth_tests_and_keeps_health_and_binding(self):
        script = r'''
source <(sed '$d' "$INSTALL")
cd "$WORK"
MODE=check
VIEWER_COOKIE_SECURE=true
VIEWER_PASSWORD=synthetic-password
EVERYDAY_PASSWORD=synthetic-password
VIEWER_URL=http://192.0.2.1:8091
INGEST_URL=http://192.0.2.1:8090
TEST_ID=install-check-test
TEST_TRIP=data/2026-01/install-check-test
TEST_UPLOAD=sample.gpx
verify_health_versions() { printf 'HEALTH_CHECKED\n'; }
http_status() { printf 401; }
upload_test_trip() { :; }
jq() { cat > /dev/null; }
curl() { printf '302 http://192.0.2.1:8091/login'; }
ask_secret() { printf 'UNEXPECTED_PROMPT\n'; exit 1; }
verify_viewer() { printf 'UNEXPECTED_LOGIN\n'; exit 1; }
verify_everyday_login() { printf 'UNEXPECTED_LOGIN\n'; exit 1; }
report_everyday_logins() { :; }
remove_test_trip() { :; }
verify_binding() { printf 'BINDING_CHECKED\n'; }
verify_end_to_end
printf 'FAILURES=%s\n' "$FAILURES"
'''
        with tempfile.TemporaryDirectory() as root:
            work = Path(root)
            (work / "data/2026-01").mkdir(parents=True)
            (work / "sample.gpx").write_text("synthetic test body")
            shutil.copyfile(work / "sample.gpx", work / "data/2026-01/install-check-test.gpx")
            (work / "data/2026-01/install-check-test.json").write_text("{}")
            (work / "viewer.env").write_text("VIEWER_USERNAME=driver\n")
            result = subprocess.run(["bash", "-c", script], env={**os.environ,
                "INSTALL": str(SERVER / "install.sh"), "WORK": root},
                capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for expected in ("HEALTH_CHECKED", "BINDING_CHECKED", "Skipped authenticated viewer tests",
                             "HTTPS name", "FAILURES=0"):
                self.assertIn(expected, result.stdout)
            self.assertNotIn("UNEXPECTED", result.stdout)

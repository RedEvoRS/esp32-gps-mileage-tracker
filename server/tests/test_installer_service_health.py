"""Exercise the installer's health diagnostics without changing real services."""

import os
from pathlib import Path
import shutil
import subprocess
import textwrap
import unittest


INSTALL = Path(__file__).resolve().parents[1] / "install.sh"

HARNESS = textwrap.dedent("""\
    source <(sed '$d' "$INSTALL")
    MODE=$FAKE_MODE
    docker() {
        case $1 in
            inspect)
                if [[ $3 == '{{.State.Error}}' ]]; then
                    printf '%s\\n' "$FAKE_ERROR"
                elif [[ $3 == '{{.State.ExitCode}}' ]]; then
                    if [[ $FAKE_EXIT_CODE == unavailable ]]; then return 1; fi
                    printf '%s\\n' "$FAKE_EXIT_CODE"
                else
                    printf '%s %s 0\\n' "$FAKE_STATUS" "$FAKE_HEALTH"
                fi
                ;;
            compose) printf '%s\\n' 'old service log' ;;
            *) return 1 ;;
        esac
    }
    if wait_healthy "$FAKE_SERVICE"; then result=0; else result=1; fi
    printf 'RESULT=%s\\nFAILURES=%s\\n' "$result" "$FAILURES"
    """)


@unittest.skipIf(os.name == "nt" or shutil.which("bash") is None, "needs a POSIX shell")
class InstallerServiceHealth(unittest.TestCase):
    def run_health(self, service="mileage-viewer", status="exited", health="none",
                   exit_code="0", error="", mode="check"):
        env = dict(os.environ, INSTALL=str(INSTALL), FAKE_SERVICE=service,
                   FAKE_STATUS=status, FAKE_HEALTH=health, FAKE_EXIT_CODE=exit_code,
                   FAKE_ERROR=error, FAKE_MODE=mode)
        return subprocess.run(["bash", "-c", HARNESS], env=env, text=True,
                              capture_output=True, timeout=10)

    def assert_failed_check(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("RESULT=1\nFAILURES=1", result.stdout)
        self.assertIn("docker compose up -d", result.stdout)

    def test_clean_stop_reports_stopped_for_both_services(self):
        for service in ("mileage-viewer", "mileage-ingest"):
            with self.subTest(service=service):
                result = self.run_health(service=service)
                self.assert_failed_check(result)
                self.assertIn(f"{service} is stopped (exit code 0)", result.stdout)
                self.assertNotIn("usual cause", result.stdout)
                self.assertNotIn("viewer.env", result.stdout)
                self.assertNotIn("old service log", result.stdout)

    def test_install_mode_still_stops_on_a_stopped_service(self):
        result = self.run_health(mode="install")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("mileage-viewer is stopped", result.stdout)
        self.assertIn("Install stopped.", result.stdout)
        self.assertNotIn("RESULT=", result.stdout)

    def test_nonzero_exit_keeps_service_specific_hints_and_logs(self):
        for service, hint in (("mileage-viewer", "viewer.env"),
                              ("mileage-ingest", "INGEST_AUTH_TOKEN")):
            with self.subTest(service=service):
                result = self.run_health(service=service, exit_code="1")
                self.assert_failed_check(result)
                self.assertIn("isn't healthy", result.stdout)
                self.assertIn(hint, result.stdout)
                self.assertIn("old service log", result.stdout)
                self.assertNotIn("is stopped", result.stdout)

    def test_docker_error_overrides_a_zero_exit_code(self):
        error = "cannot assign requested address"
        result = self.run_health(error=error)
        self.assert_failed_check(result)
        self.assertIn(f"Docker says: {error}", result.stdout)
        self.assertIn("BIND_ADDR isn't on this server", result.stdout)
        self.assertNotIn("is stopped", result.stdout)

    def test_other_states_or_unknown_exit_code_are_not_clean_stops(self):
        for status, health, exit_code in (("running", "unhealthy", "0"),
                                          ("restarting", "none", "1"),
                                          ("created", "none", "0"),
                                          ("exited", "none", "unavailable")):
            with self.subTest(status=status, exit_code=exit_code):
                result = self.run_health(status=status, health=health,
                                         exit_code=exit_code)
                self.assert_failed_check(result)
                self.assertIn("isn't healthy", result.stdout)
                self.assertNotIn("is stopped", result.stdout)

    def test_healthy_service_still_passes(self):
        result = self.run_health(status="running", health="healthy")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("mileage-viewer is healthy", result.stdout)
        self.assertIn("RESULT=0\nFAILURES=0", result.stdout)


if __name__ == "__main__":
    unittest.main()

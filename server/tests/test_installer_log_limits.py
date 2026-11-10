"""Inspect real installer logic with fake containers, without changing Docker."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SERVER = Path(__file__).resolve().parents[1]
INSTALL = SERVER / "install.sh"
BOUNDED = '{"Type":"json-file","Config":{"max-size":"10m","max-file":"3"}}'

HARNESS = r'''
source <(sed '$d' "$INSTALL")
MODE=check
docker() {
    [[ $1 == inspect && $2 == -f ]] || return 90
    local id=${!#} svc
    svc=${id#cid-}
    printf '%s %s\n' "$3" "$id" >> "$CALLS"
    case $3 in
        '{{.State.Status}}')
            if [[ $svc == "$STOPPED" ]]; then echo exited; else echo running; fi ;;
        '{{json .HostConfig.LogConfig}}')
            if [[ $svc == "$BAD_SERVICE" ]]; then
                if [[ $INSPECT_FAIL == yes ]]; then echo private-inspect-error >&2; return 1; fi
                printf '%s\n' "$BAD_CONFIG"
            else
                printf '%s\n' "$BOUNDED"
            fi ;;
        *) return 91 ;;
    esac
}
compose() {
    [[ $* == '--profile monitor --profile ntfy ps -a -q '* ]] || return 92
    if [[ $OPTIONALS == yes ]]; then printf 'cid-%s\n' "${!#}"; fi
}
verify_log_limits
printf 'WARNINGS=%s\nFAILURES=%s\n' "${#WARNINGS[@]}" "$FAILURES"
'''


class LogLimitWiring(unittest.TestCase):
    def test_check_runs_log_inspection_after_optional_service_setup(self):
        main = INSTALL.read_text(encoding="utf-8").split("main() {", 1)[1]
        self.assertIn("setup_outputs\n    verify_log_limits\n    summary", main)


@unittest.skipIf(os.name == "nt" or not shutil.which("bash") or not shutil.which("jq"),
                 "needs POSIX bash and jq (WSL)")
class InstallerLogLimits(unittest.TestCase):
    def run_check(self, platform, **settings):
        with tempfile.TemporaryDirectory() as folder:
            calls = Path(folder) / "calls"
            env = dict(os.environ, INSTALL=str(INSTALL), MILEAGE_PLATFORM=platform,
                       CALLS=str(calls), BOUNDED=BOUNDED, BAD_SERVICE="", BAD_CONFIG="",
                       INSPECT_FAIL="no", OPTIONALS="yes", STOPPED="")
            env.update(settings)
            result = subprocess.run(["bash", "-c", HARNESS], env=env, text=True,
                                    capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stderr, "")
            self.assertIn("FAILURES=0", result.stdout)
            return result.stdout, calls.read_text()

    def test_all_four_running_services_have_limits_on_both_platforms(self):
        for platform in ("debian", "unraid"):
            with self.subTest(platform=platform):
                out, calls = self.run_check(platform)
                self.assertIn("WARNINGS=0", out)
                self.assertEqual(out.count("logs are limited to three 10 MB files"), 4)
                self.assertIn("LogConfig}} cid-mileage-monitor", calls)
                self.assertIn("LogConfig}} cid-ntfy", calls)

    def test_unlimited_or_different_settings_warn_with_recreation_fix(self):
        for platform in ("debian", "unraid"):
            for service in ("mileage-ingest", "mileage-viewer", "mileage-monitor", "ntfy"):
                for config in ('{"Type":"json-file","Config":{}}',
                               '{"Type":"json-file","Config":{"max-size":"10m"}}',
                               '{"Type":"json-file","Config":{"max-size":"20m","max-file":"3"}}',
                               '{"Type":"journald","Config":{}}', 'invalid-private-config'):
                    with self.subTest(platform=platform, service=service, config=config):
                        out, _ = self.run_check(platform, BAD_SERVICE=service, BAD_CONFIG=config)
                        self.assertIn("WARNINGS=1", out)
                        self.assertIn(f"{service} isn't using the three-file, 10 MB container log limit", out)
                        self.assertIn(f"up -d --force-recreate --no-deps {service}", out)
                        self.assertIn("Existing logs aren't trimmed in place", out)
                        self.assertNotIn(config, out)

    def test_absent_optional_and_stopped_containers_are_skipped(self):
        for platform in ("debian", "unraid"):
            with self.subTest(platform=platform):
                out, calls = self.run_check(platform, OPTIONALS="no", STOPPED="mileage-viewer")
                self.assertIn("WARNINGS=0", out)
                self.assertEqual(calls.count("HostConfig.LogConfig"), 1)
                self.assertNotIn("cid-", calls)

    def test_inspect_failure_warns_without_printing_docker_output(self):
        for platform in ("debian", "unraid"):
            with self.subTest(platform=platform):
                out, _ = self.run_check(platform, BAD_SERVICE="mileage-viewer", INSPECT_FAIL="yes")
                self.assertIn("Couldn't check mileage-viewer's container log limit", out)
                self.assertIn("WARNINGS=1", out)
                self.assertNotIn("private-inspect-error", out)

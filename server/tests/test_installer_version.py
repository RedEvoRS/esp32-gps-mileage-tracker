"""Run the real installer health/version functions against fake services."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import textwrap
import unittest


SERVER = Path(__file__).resolve().parents[1]
HARNESS = textwrap.dedent(r"""
    source <(sed '$d' "$INSTALL")
    MODE=check
    INGEST_URL=http://ingest.invalid
    VIEWER_URL=http://viewer.invalid
    curl() {
        case ${*: -1} in
            "$INGEST_URL/health") printf '%s' "$INGEST_BODY" ;;
            "$VIEWER_URL/health") printf '%s' "$VIEWER_BODY" ;;
            *) return 1 ;;
        esac
    }
    if verify_health_versions; then result=0; else result=1; fi
    printf 'RESULT=%s\nFAILURES=%s\nWARNINGS=%s\n' "$result" "$FAILURES" "${#WARNINGS[@]}"
""")


@unittest.skipIf(os.name == "nt" or not shutil.which("bash") or not shutil.which("jq"),
                 "needs a POSIX shell and jq")
class InstallerVersion(unittest.TestCase):
    def run_versions(self, ingest, viewer):
        env = dict(os.environ, INSTALL=str(SERVER / "install.sh"),
                   INGEST_BODY=ingest, VIEWER_BODY=viewer)
        result = subprocess.run(["bash", "-c", HARNESS], env=env, text=True,
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    @staticmethod
    def health(version):
        return json.dumps({"status": "ok", "version": version})

    def test_matching_versions_are_printed_without_warning(self):
        version = "v0.01+abcdef1234"
        output = self.run_versions(self.health(version), self.health(version))
        for service in ("mileage-ingest", "mileage-viewer"):
            self.assertIn(f"{service} version: {version}", output)
        self.assertIn("RESULT=0\nFAILURES=0\nWARNINGS=0", output)

    def test_mismatch_warns_without_failing_check(self):
        output = self.run_versions(self.health("v0.01+abcdef1234"), self.health("v0.02+0123456789"))
        self.assertIn("warn  Ingest and viewer report different server versions", output)
        self.assertIn("RESULT=0\nFAILURES=0\nWARNINGS=1", output)

    def test_old_missing_and_invalid_values_print_only_safe_fallback(self):
        for version in (None, 123, "<script>", "v0.01\n", "a" * 65):
            with self.subTest(version=version):
                output = self.run_versions('{"status":"ok"}', self.health(version))
                self.assertIn("mileage-ingest version: unversioned", output)
                self.assertIn("mileage-viewer version: unversioned", output)
                self.assertIn("RESULT=0\nFAILURES=0\nWARNINGS=1", output)
                self.assertNotIn("<script>", output)

    def test_failed_health_keeps_failure_convention(self):
        output = self.run_versions(self.health("v0.01"), '{"status":"failed"}')
        self.assertIn("RESULT=1\nFAILURES=1", output)

    def test_installer_build_computes_stamp_and_passes_it_through_sudo(self):
        script = textwrap.dedent(r"""
            source <(sed '$d' "$INSTALL")
            python3() { printf 'v0.01+abcdef1234'; }
            docker() { printf 'BUILD_STAMP=%s ARGS=%s\n' "$SERVER_VERSION" "$*"; }
            build_images
            # Exercise the sudo/group wrapper without invoking sudo or Docker.
            hop() { "$@"; }
            env() { printf 'HOP_ARGS=%s\n' "$*"; }
            AS_ME=(hop)
            compose build
        """)
        result = subprocess.run(["bash", "-c", script], env=dict(os.environ, INSTALL=str(SERVER / "install.sh")),
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("BUILD_STAMP=v0.01+abcdef1234 ARGS=compose build", result.stdout)
        self.assertIn("SERVER_VERSION=v0.01+abcdef1234 docker compose build", result.stdout)


class InstallerVersionLayout(unittest.TestCase):
    def test_check_calls_version_health_check_and_preflight_requires_helper(self):
        install = (SERVER / "install.sh").read_text(encoding="utf-8")
        self.assertIn('BUILD_SERVER_VERSION=$(host_python stamp "$SCRIPT_DIR/stamp_version.py")', install)
        self.assertIn("verify_health_versions || return 1", install)
        self.assertIn("for f in VERSION stamp_version.py common/version.py", install)

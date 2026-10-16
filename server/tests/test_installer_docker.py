"""Distinguish Docker image download failures from container startup failures."""

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
    installed=$FAKE_INSTALLED
    install_packages() { :; }
    install_docker() { installed=yes; }
    command() {
        if [[ $* == '-v docker' ]]; then
            [[ $installed == yes ]]
        else
            builtin command "$@"
        fi
    }
    systemctl() { printf 'active\\n'; }
    docker() {
        printf 'DOCKER: %s\\n' "$*" >&2
        case "$*" in
            'compose version --short') printf '2.35.0\\n' ;;
            --version) printf 'Docker version 28.0.0, build test\\n' ;;
            info) return 0 ;;
            'pull hello-world')
                if [[ $FAKE_PULL_STATUS != 0 ]]; then
                    echo 'registry download error' >&2
                    return "$FAKE_PULL_STATUS"
                fi ;;
            'run --rm hello-world')
                if [[ $FAKE_RUN_STATUS != 0 ]]; then
                    echo 'overlay mount error' >&2
                    return "$FAKE_RUN_STATUS"
                fi ;;
            *) return 2 ;;
        esac
    }
    setup_docker
    printf 'DONE\\n'
    """)


@unittest.skipIf(os.name == "nt" or shutil.which("bash") is None, "needs a POSIX shell")
class InstallerDocker(unittest.TestCase):
    def run_step(self, *, installed="no", mode="install", pull_status=0, run_status=0):
        env = dict(os.environ, INSTALL=str(INSTALL), FAKE_INSTALLED=installed,
                   FAKE_MODE=mode, FAKE_PULL_STATUS=str(pull_status),
                   FAKE_RUN_STATUS=str(run_status))
        return subprocess.run(["bash", "-c", HARNESS], env=env, text=True,
                              capture_output=True, timeout=10)

    def test_fresh_install_downloads_before_running(self):
        proc = self.run_step()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("Docker can download and run a container", proc.stdout)
        self.assertIn("DONE", proc.stdout)
        calls = proc.stderr.splitlines()
        self.assertEqual(calls[-2:], ["DOCKER: pull hello-world",
                                     "DOCKER: run --rm hello-world"])

    def test_download_failure_has_network_hint_and_does_not_run(self):
        proc = self.run_step(pull_status=1)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("couldn't download hello-world", proc.stdout)
        self.assertIn("internet access and DNS", proc.stdout)
        self.assertIn("registry download error", proc.stderr)
        self.assertNotIn("DOCKER: run", proc.stderr)
        self.assertNotIn("DONE", proc.stdout)

    def test_startup_failure_preserves_error_without_network_hint(self):
        proc = self.run_step(run_status=125)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("couldn't run hello-world", proc.stdout)
        self.assertIn("image downloaded successfully", proc.stdout)
        self.assertIn("overlay mount error", proc.stderr)
        self.assertNotIn("internet access", proc.stdout)
        self.assertNotIn("DNS", proc.stdout)
        self.assertNotIn("DONE", proc.stdout)

    def test_existing_install_does_not_download_or_run_in_either_mode(self):
        for mode in ("install", "check"):
            with self.subTest(mode=mode):
                proc = self.run_step(installed="yes", mode=mode)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertIn("DONE", proc.stdout)
                self.assertNotIn("hello-world", proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()

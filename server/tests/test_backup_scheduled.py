"""Run the real cron wrapper with a controlled backup and command shims."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

from test_backup import BACKUP, tools_missing


@unittest.skipIf(tools_missing(), tools_missing())
class ScheduledBackup(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.install = self.tmp / "install with spaces"
        self.scripts = self.install / "backup"
        self.scripts.mkdir(parents=True)
        for name in ("scheduled.sh", "lib.sh"):
            shutil.copyfile(BACKUP / name, self.scripts / name)
        self.backups = self.tmp / "backups"
        self.backups.mkdir()
        self.conf = self.tmp / "private.conf"
        self.conf.write_text(f"BACKUP_DIR='{self.backups}'\nRSYNC_SSH='private-ssh-sentinel'\n")
        self.conf.chmod(0o600)
        self.env = dict(os.environ, MILEAGE_BACKUP_CONF=str(self.conf),
                        MILEAGE_PLATFORM="unraid", CALLS=str(self.tmp / "calls"),
                        STARTED=str(self.tmp / "started"), RELEASE=str(self.tmp / "release"))
        self.env.pop("JOURNAL_STREAM", None)
        self.shims = self.tmp / "bin"
        self.shims.mkdir()
        self.env["PATH"] = f"{self.shims}:{self.env['PATH']}"
        for command in ("sudo", "apt-get", "dpkg", "systemctl", "journalctl"):
            self.shim(command, 'echo "forbidden command" >> "$CALLS"; exit 99\n')
        for name, skip in (("nice", 2), ("ionice", 1), ("timeout", 1)):
            self.shim(name, f'echo "{name} $*" >> "$CALLS"\nshift {skip}\nexec "$@"\n')
        self.shim("sleep", 'echo "sleep $*" >> "$CALLS"\n')
        self.fake_backup('echo "backup output"; exit "${BACKUP_EXIT:-0}"\n')
        self.state = self.scripts / "state/last-attempt"
        self.log = self.scripts / "logs/backup.log"

    def shim(self, name, body):
        path = self.shims / name
        path.write_text("#!/bin/bash\n" + body)
        path.chmod(0o755)

    def fake_backup(self, body):
        (self.scripts / "backup.sh").write_text("#!/bin/bash\n" + body)

    def run_wrapper(self, *args):
        return subprocess.run(["bash", str(self.scripts / "scheduled.sh"), *args],
                              env=self.env, capture_output=True, text=True, timeout=15)

    def fields(self, path):
        return dict(line.split("=", 1) for line in path.read_text().splitlines())

    def status(self):
        result = self.run_wrapper("--status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertNotIn("private-ssh-sentinel", result.stdout)
        return dict(line.split("=", 1) for line in result.stdout.splitlines())

    def test_status_before_first_run_has_exact_fields_and_creates_nothing(self):
        self.assertEqual(self.status(), {
            "last_attempt_start": "", "last_attempt_end": "", "last_attempt_status": "",
            "last_success": "", "log": str(self.log)})
        self.assertFalse(self.state.parent.exists())
        self.assertFalse(self.log.parent.exists())

    def test_status_returns_zero_when_config_is_missing_or_invalid(self):
        for config in ("missing", "invalid"):
            if config == "missing":
                self.conf.unlink()
            else:
                self.conf.write_text("BACKUP_DIR=\n")
            self.assertEqual(self.status()["last_success"], "")

    def test_success_records_utc_state_and_private_log_in_install_folder(self):
        result = self.run_wrapper()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout + result.stderr, "")
        state = self.fields(self.state)
        self.assertEqual(state["status"], "0")
        for key in ("start", "end"):
            self.assertRegex(state[key], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(self.log.read_text(), "backup output\n")
        for path, mode in ((self.state.parent, 0o700), (self.log.parent, 0o700),
                           (self.state, 0o600), (self.log, 0o600)):
            self.assertEqual(path.stat().st_mode & 0o777, mode)
        calls = (self.tmp / "calls").read_text().splitlines()
        self.assertTrue(calls[0].startswith("nice -n 10 ionice -c3 timeout 6h bash "))
        self.assertTrue(calls[1].startswith("ionice -c3 timeout 6h bash "))
        self.assertTrue(calls[2].startswith("timeout 6h bash "))
        self.assertNotIn("private-ssh-sentinel", self.log.read_text())
        status = self.status()
        self.assertEqual(status["last_attempt_status"], "0")

    def test_failure_and_timeout_preserve_exit_code_and_old_success(self):
        success = "2026-01-01T00:00:00Z"
        (self.backups / "last-success").write_text(success + "\n")
        for code in (1, 23, 124):
            self.env["BACKUP_EXIT"] = str(code)
            result = self.run_wrapper()
            self.assertEqual(result.returncode, code)
            self.assertEqual(self.status()["last_attempt_status"], str(code))
            self.assertEqual(self.status()["last_success"], success)

    def test_real_timeout_records_124(self):
        timeout = shutil.which("timeout")
        self.shim("timeout", f'shift\nexec {timeout} 0.1 "$@"\n')
        self.fake_backup('/bin/sleep 5\n')
        self.assertEqual(self.run_wrapper().returncode, 124)
        self.assertEqual(self.status()["last_attempt_status"], "124")
        self.assertNotEqual(self.status()["last_attempt_end"], "")

    def test_real_backup_config_failure_is_recorded(self):
        shutil.copyfile(BACKUP / "backup.sh", self.scripts / "backup.sh")
        self.conf.unlink()
        result = self.run_wrapper("--if-overdue", "26")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.status()["last_attempt_status"], "1")
        self.assertIn("no config", self.log.read_text())
        self.assertTrue(self.state.exists())

    def test_missing_mount_failure_still_has_state_and_log_on_install_disk(self):
        shutil.copyfile(BACKUP / "backup.sh", self.scripts / "backup.sh")
        mount = self.tmp / "absent-mount"
        self.conf.write_text(f"BACKUP_DIR='{mount}/backups'\nBACKUP_MOUNT='{mount}'\n"
                             "REMOTE=/unused-remote\n")
        result = self.run_wrapper("--if-overdue", "26")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.status()["last_attempt_status"], "1")
        self.assertIn("BACKUP_DIR mount is not mounted", self.log.read_text())
        self.assertFalse(mount.exists())

    def test_recent_success_skips_without_creating_state_or_logs(self):
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        (self.backups / "last-success").write_text(now + "\n")
        result = self.run_wrapper("--if-overdue", "26")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout + result.stderr, "")
        self.assertFalse(self.state.exists())
        self.assertFalse(self.log.exists())

    def test_old_missing_or_invalid_success_is_overdue(self):
        for contents in (None, "2020-01-01T00:00:00Z\n", "invalid\n"):
            with self.subTest(contents=contents):
                path = self.backups / "last-success"
                if contents is not None:
                    path.write_text(contents)
                result = self.run_wrapper("--if-overdue", "26")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.fields(self.state)["status"], "0")

    def test_random_delay_stays_in_inclusive_range(self):
        for limit in (1, 900):
            self.assertEqual(self.run_wrapper("--delay", str(limit)).returncode, 0)
        sleeps = [int(line.split()[1]) for line in (self.tmp / "calls").read_text().splitlines()
                  if line.startswith("sleep ")]
        self.assertEqual(len(sleeps), 2)
        for delay, limit in zip(sleeps, (1, 900), strict=True):
            self.assertGreaterEqual(delay, 0)
            self.assertLessEqual(delay, limit)

    def test_rotation_keeps_three_private_logs(self):
        self.log.parent.mkdir()
        self.log.write_text("x" * (1048576 + 1))
        self.log.chmod(0o644)
        for index in (1, 2, 3):
            Path(f"{self.log}.{index}").write_text(str(index))
        self.assertEqual(self.run_wrapper().returncode, 0)
        self.assertEqual(Path(f"{self.log}.1").stat().st_size, 1048577)
        self.assertEqual(Path(f"{self.log}.2").read_text(), "1")
        self.assertEqual(Path(f"{self.log}.3").read_text(), "2")
        self.assertEqual(self.log.read_text(), "backup output\n")
        for path in self.log.parent.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_output_that_crosses_limit_is_rotated_after_run(self):
        self.fake_backup("head -c 1048577 /dev/zero | tr '\\0' x\n")
        self.assertEqual(self.run_wrapper().returncode, 0)
        self.assertEqual(Path(f"{self.log}.1").stat().st_size, 1048577)
        self.assertEqual(self.log.stat().st_size, 0)

    def test_noisy_running_backup_rotates_before_it_finishes(self):
        self.fake_backup('head -c 5000000 /dev/zero | tr "\\0" x\ntouch "$STARTED"\n'
                         'while [[ ! -f $RELEASE ]]; do /bin/sleep 0.05; done\n')
        process = subprocess.Popen(["bash", str(self.scripts / "scheduled.sh")], env=self.env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 10
            while not (self.tmp / "started").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue((self.tmp / "started").exists())
            self.assertEqual(self.status()["last_attempt_status"], "running")
            self.assertTrue(Path(f"{self.log}.3").exists())
            self.assertLessEqual(sum(p.stat().st_size for p in self.log.parent.iterdir()),
                                 4 * (1048576 + 65536))
        finally:
            (self.tmp / "release").touch()
            process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0)

    def test_missing_ionice_uses_nice_and_timeout(self):
        startup = self.tmp / "bash-env"
        startup.write_text('command() {\n'
                           '    if [[ $* == "-v ionice" ]]; then return 1; fi\n'
                           '    builtin command "$@"\n}\n')
        self.env["BASH_ENV"] = str(startup)
        self.assertEqual(self.run_wrapper().returncode, 0)
        calls = (self.tmp / "calls").read_text()
        self.assertIn("nice -n 10 timeout 6h bash", calls)
        self.assertNotIn("ionice", calls)

    def test_overdue_busy_backup_preserves_previous_record_or_absence(self):
        self.fake_backup('echo "another backup is already running"; exit 75\n')
        self.assertEqual(self.run_wrapper("--if-overdue", "26").returncode, 0)
        self.assertFalse(self.state.exists())
        previous = "start=2026-01-01T00:00:00Z\nend=2026-01-01T01:00:00Z\nstatus=23\n"
        self.state.write_text(previous)
        self.assertEqual(self.run_wrapper("--if-overdue", "26").returncode, 0)
        self.assertEqual(self.state.read_text(), previous)
        self.assertFalse(list(self.state.parent.glob(".previous.*")))
        self.assertEqual(self.run_wrapper().returncode, 75)
        self.assertEqual(self.fields(self.state)["status"], "75")

    def test_simultaneous_nightly_and_catchup_start_only_one_backup(self):
        self.fake_backup('touch "$STARTED"\nwhile [[ ! -f $RELEASE ]]; do /bin/sleep 0.05; done\n')
        process = subprocess.Popen(["bash", str(self.scripts / "scheduled.sh")], env=self.env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 5
            while not (self.tmp / "started").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue((self.tmp / "started").exists())
            self.assertEqual(self.status()["last_attempt_status"], "running")
            before = self.state.read_text()
            self.assertEqual(self.run_wrapper("--if-overdue", "26").returncode, 0)
            self.assertEqual(self.state.read_text(), before)
            self.assertEqual((self.tmp / "calls").read_text().count("nice -n 10"), 1)
        finally:
            (self.tmp / "release").touch()
            process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(self.status()["last_attempt_status"], "0")

    def test_invalid_arguments_never_start_backup(self):
        for args in (("--delay",), ("--delay", "-1"), ("--if-overdue", "x"),
                     ("--if-overdue", "99999999999"), ("--bogus",),
                     ("--status", "--if-overdue", "26")):
            self.assertEqual(self.run_wrapper(*args).returncode, 2)
        self.assertFalse(self.state.exists())


if __name__ == "__main__":
    unittest.main()

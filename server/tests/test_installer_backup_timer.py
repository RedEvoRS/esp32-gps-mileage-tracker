"""install.sh puts the backup timer right after the install moves (#46).

Sources the real install.sh functions (everything but the final `main`)
into bash and runs setup_backups against a fake systemd: unit files go
to a temporary UNIT_DIR, and the fake systemctl loads ExecStart from
them on daemon-reload, the way systemd does, and `start` runs it. Each
install's backup.sh and restore.sh are stubs that log where they ran
from. Needs bash, so it skips on Windows.
"""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap
import unittest


SERVER = Path(__file__).resolve().parents[1]
INSTALL = SERVER / "install.sh"
USER = "tester"

HARNESS = textwrap.dedent("""\
    source <(sed '$d' "$INSTALL")
    MODE=$RUN_MODE
    ME=$FAKE_USER
    CONTAINER_UID=$(id -u)
    SCRIPT_DIR=$RUN_DIR
    UNIT_DIR=$FAKE_UNITS
    DEFAULT_DIR=$FAKE_DEFAULT
    sudo() { "$@"; }
    SUDO_READY=yes
    journalctl() { :; }
    # ExecStart as systemd would load it: the last one in the drop-in if
    # there is one, otherwise the template's, with %i filled in.
    load_exec() {
        local file=$UNIT_DIR/mileage-backup@.service cmd
        [[ -f $(backup_dropin) ]] && file=$(backup_dropin)
        cmd=$(grep '^ExecStart=.' "$file" | tail -n 1)
        cmd=${cmd#ExecStart=}
        cmd=${cmd//\\"/}
        cmd=${cmd//\\/home\\/%i\\/mileage-tracker/$FAKE_DEFAULT}
        cmd=${cmd//%%/%}
        printf '{ path=/bin/bash ; argv[]=%s ; ignore_errors=no }' "$cmd"
    }
    systemctl() {
        case $1 in
            daemon-reload) load_exec > "$STATE/exec" ;;
            enable) touch "$STATE/enabled" ;;
            is-enabled) [[ -f $STATE/enabled ]] && echo enabled || { echo disabled; return 1; } ;;
            is-active) [[ -f $STATE/enabled ]] && echo active || { echo inactive; return 3; } ;;
            is-failed) return 1 ;;
            start)  # runs what's loaded, as systemd would
                local cmd
                cmd=$(cat "$STATE/exec")
                cmd=${cmd#*argv[]=}
                echo systemd >> "$STATE/log"
                ${cmd%% ;*} ;;
            show)
                case $3 in
                    ExecStart) cat "$STATE/exec" 2> /dev/null ;;
                    *) echo tomorrow ;;
                esac ;;
        esac
    }
    cd "$SCRIPT_DIR"
    setup_backups
    echo "FAILURES=$FAILURES"
    """)


@unittest.skipIf(os.name == "nt" or shutil.which("bash") is None,
                 "needs bash")
class InstallerBackupTimer(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.units = self.root / "units"
        self.units.mkdir()
        self.state = self.root / "state"
        self.state.mkdir()
        self.backups = self.root / "backups"
        self.backups.mkdir()
        (self.backups / "last-success").write_text("2099-01-01T00:00:00Z")
        self.default = self.make_install(f"home/{USER}/mileage-tracker")
        self.a = self.make_install("srv/a/mileage-tracker")
        self.b = self.make_install("srv/b/mileage-tracker")
        self.dropin = (self.units / f"mileage-backup@{USER}.service.d"
                       / "install-path.conf")

    def make_install(self, rel):
        path = self.root / rel
        (path / "backup").mkdir(parents=True)
        for name in ("mileage-backup@.service", "mileage-backup@.timer"):
            shutil.copy(SERVER / "backup" / name, path / "backup" / name)
        (path / "backup" / "backup.sh").write_text(textwrap.dedent(f"""\
            case ${{1:-}} in
                --check-target) [[ ! -f "$STATE/unmarked" ]]; exit ;;
                --mark-target) rm -f "$STATE/unmarked"; echo "mark {path}" >> "$STATE/log"; exit 0 ;;
            esac
            echo "backup {path}" >> "$STATE/log"
            """))
        (path / "backup" / "restore.sh").write_text(
            f'echo "restore {path}" >> "$STATE/log"\n')
        (path / "data").mkdir()
        self.write_conf(path, "backup@nas.invalid:/backups/mileage")
        return path

    def write_conf(self, path, remote):
        (path / "backup" / "backup.conf").write_text(
            f"REMOTE='{remote}'\n"
            f"BACKUP_DIR='{self.backups}'\n")

    def run_setup(self, where, mode="install", extra_env=None):
        log = self.state / "log"
        if log.exists():
            log.unlink()
        env = dict(os.environ, INSTALL=str(INSTALL), RUN_MODE=mode,
                   RUN_DIR=str(where), FAKE_UNITS=str(self.units),
                   FAKE_DEFAULT=str(self.default), STATE=str(self.state),
                   FAKE_USER=USER)
        env.update({"INSTALL_SAME_DISK": "y", **(extra_env or {})})
        proc = subprocess.run(["bash", "-c", HARNESS], env=env, text=True,
                              capture_output=True, timeout=60)
        out = proc.stdout + proc.stderr
        lines = log.read_text().splitlines() if log.exists() else []
        return proc.returncode, out, lines

    def assert_runs(self, where):
        code, out, _ = self.run_setup(where, mode="check")
        self.assertEqual(code, 0, out)
        self.assertIn("FAILURES=0", out)
        self.assertIn(f"argv[]=/bin/bash {where}/backup/backup.sh ;",
                      (self.state / "exec").read_text())

    def test_moving_the_install_repoints_the_running_timer(self):
        code, out, _ = self.run_setup(self.a)
        self.assertEqual(code, 0, out)
        self.assert_runs(self.a)

        # The issue's case: moved to B, timer still on, rerun.
        code, out, log = self.run_setup(self.b)
        self.assertEqual(code, 0, out)
        self.assertIn("runs backup.sh from somewhere else", out)
        # A backup and restore check from B, then the backup again
        # through systemd, also from B.
        self.assertEqual(log, [f"backup {self.b}", f"restore {self.b}",
                               "systemd", f"backup {self.b}"])
        self.assertIn(str(self.b), self.dropin.read_text())
        self.assert_runs(self.b)

    def test_moving_back_to_the_default_path_removes_the_drop_in(self):
        self.run_setup(self.a)
        self.assertTrue(self.dropin.exists())
        code, out, log = self.run_setup(self.default)
        self.assertEqual(code, 0, out)
        self.assertFalse(self.dropin.exists())
        self.assertEqual(log[-2:], ["systemd", f"backup {self.default}"])
        self.assert_runs(self.default)

    def test_rerun_in_place_leaves_a_current_timer_alone(self):
        self.run_setup(self.a)
        code, out, log = self.run_setup(self.a)
        self.assertEqual(code, 0, out)
        self.assertEqual(log, [])

    def test_check_reports_a_timer_running_another_copy(self):
        self.run_setup(self.a)
        code, out, log = self.run_setup(self.b, mode="check")
        self.assertIn("doesn't back up", out)
        self.assertIn("FAILURES=1", out)
        # --check only reports: the timer still points at A.
        self.assertIn(str(self.a), self.dropin.read_text())
        self.assertNotIn(f"backup {self.b}", log)

    # --- unit files follow the update (#470) and the mount rule (#466) ---

    def installed(self, name="mileage-backup@.service"):
        return self.units / name

    def test_update_reinstalls_unit_files_that_differ_from_the_shipped_ones(self):
        self.run_setup(self.a)
        shipped = (self.a / "backup/mileage-backup@.service").read_text()
        self.installed().write_text("old unit\n")
        self.installed("mileage-backup@.timer").write_text("old timer\n")
        code, out, log = self.run_setup(self.a)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.installed().read_text(), shipped)
        self.assertEqual(self.installed("mileage-backup@.timer").read_text(),
                         (self.a / "backup/mileage-backup@.timer").read_text())
        self.assertIn("Updated the backup's systemd files", out)
        # No new backup run for a file refresh.
        self.assertEqual(log, [])
        code, out, log = self.run_setup(self.a)
        self.assertNotIn("Updating them", out)

    def test_check_warns_about_stale_unit_files_without_changing_them(self):
        self.run_setup(self.a)
        self.installed().write_text("old unit\n")
        code, out, _ = self.run_setup(self.a, mode="check")
        self.assertIn("differ from the ones", out)
        self.assertIn("FAILURES=0", out)
        self.assertEqual(self.installed().read_text(), "old unit\n")

    def mounts_dropin(self):
        return self.units / f"mileage-backup@{USER}.service.d" / "backup-mounts.conf"

    def test_local_destination_requires_its_mount_through_a_drop_in(self):
        mount = self.root / "mnt/backup-share/mileage"
        self.write_conf(self.a, str(mount))
        code, out, _ = self.run_setup(self.a)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.mounts_dropin().read_text(),
                         f"[Unit]\nRequiresMountsFor={mount} {self.backups}\n")
        # Back to an SSH destination: only BACKUP_DIR is left to require.
        self.write_conf(self.a, "backup@nas.invalid:/backups/mileage")
        code, out, _ = self.run_setup(self.a)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.mounts_dropin().read_text(),
                         f"[Unit]\nRequiresMountsFor={self.backups}\n")

    # --- the destination marker file (#466) ---

    def test_a_rerun_marks_a_destination_that_has_no_marker_yet(self):
        self.run_setup(self.a)
        (self.state / "unmarked").touch()
        code, out, log = self.run_setup(self.a)
        self.assertEqual(code, 0, out)
        self.assertEqual(log, [f"mark {self.a}"])
        self.assertIn("Marked", out)

    def test_a_marked_destination_is_left_alone(self):
        self.run_setup(self.a)
        code, out, log = self.run_setup(self.a)
        self.assertEqual(log, [])

    def test_a_rerun_does_not_mark_an_unmounted_local_destination(self):
        # A local folder on the same disk as the data is what an unmounted
        # share looks like. The default answer to "use it anyway?" is no.
        self.run_setup(self.a)
        target = self.root / "mnt/backup-share"
        self.write_conf(self.a, str(target))
        (self.state / "unmarked").touch()
        env_answer = {"INSTALL_SAME_DISK": "n"}
        code, out, log = self.run_setup(self.a, extra_env=env_answer)
        self.assertEqual(code, 0, out)
        self.assertEqual(log, [])
        self.assertIn("nightly backup will refuse", out)

    def test_check_fails_when_the_destination_has_no_marker(self):
        self.run_setup(self.a)
        (self.state / "unmarked").touch()
        code, out, _ = self.run_setup(self.a, mode="check")
        self.assertIn("FAILURES=1", out)
        self.assertIn("--mark-target", out)

    def test_a_similar_path_is_not_mistaken_for_this_one(self):
        # /srv/a/mileage-tracker must not match /srv/a/mileage-tracker-old.
        old = self.make_install("srv/a/mileage-tracker-old")
        self.run_setup(old)
        code, out, _ = self.run_setup(self.a, mode="check")
        self.assertIn("doesn't back up", out)


SSH_HARNESS = textwrap.dedent("""\
    source <(sed '$d' "$INSTALL")
    ME=tester
    ssh() { return 0; }
    ssh-keygen() { return 0; }
    ssh-copy-id() { return 0; }
    ssh_target_ok user@nas.invalid:/backups/mileage > /dev/null
    echo "CMD=$RSYNC_SSH_CMD"
    """)


@unittest.skipIf(os.name == "nt" or shutil.which("bash") is None,
                 "needs bash")
class InstallerBackupSshCommand(unittest.TestCase):
    def test_generated_ssh_command_has_keepalive_and_connect_limits(self):
        home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, home)
        (home / ".ssh").mkdir()
        (home / ".ssh/mileage_backup").write_text("key")
        env = dict(os.environ, INSTALL=str(INSTALL), HOME=str(home))
        proc = subprocess.run(["bash", "-c", SSH_HARNESS], env=env, text=True,
                              capture_output=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        cmd = [line for line in proc.stdout.splitlines() if line.startswith("CMD=")][0]
        for option in ("IdentitiesOnly=yes", "BatchMode=yes", "ServerAliveInterval=30",
                       "ServerAliveCountMax=4", "ConnectTimeout=30"):
            self.assertIn(option, cmd)


if __name__ == "__main__":
    unittest.main()

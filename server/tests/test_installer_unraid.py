"""Unraid installer branches with fake Docker, mounts and cron; no host changes."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest


SERVER = Path(__file__).resolve().parents[1]
INSTALL = SERVER / "install.sh"

HARNESS = r'''
source <(sed '$d' "$INSTALL")
cd "$WORK"
SCRIPT_DIR=$WORK
forbidden() { echo "$*" >> "$WORK/forbidden"; exit 99; }
sudo() { forbidden sudo "$@"; }
apt-get() { forbidden apt-get "$@"; }
dpkg() { forbidden dpkg "$@"; }
dpkg-query() { forbidden dpkg-query "$@"; }
systemctl() { forbidden systemctl "$@"; }
journalctl() { forbidden journalctl "$@"; }
timedatectl() { forbidden timedatectl "$@"; }
crontab() { cat "$LOADED"; }
findmnt() {
    case "$*" in
        *--mountpoint*) [[ ${MOUNT_MISSING:-no} != yes ]] ;;
        *FSTYPE*) printf '%s\n' "${MOUNT_FS:-btrfs}" ;;
        *TARGET*) printf '%s\n' "${MOUNT_TARGET:-/mnt/pool}" ;;
        *) return 88 ;;
    esac
}
docker() {
    printf '%s\n' "$*" >> "$WORK/docker-calls"
    case "$*" in
        'compose version --short') echo "${COMPOSE_VERSION:-5.6.0}" ;;
        info) return "${DOCKER_STATUS:-0}" ;;
        'info --format {{.DockerRootDir}}') echo "$WORK/docker-storage" ;;
        *RestartPolicy*) echo "${POLICY:-always}" ;;
        *'ps -a -q ntfy'|*'ps -a -q mileage-monitor')
            if [[ ${OPTIONAL:-no} == yes ]]; then echo optional-container; fi ;;
        'compose build') echo "BUILD_STAMP=$SERVER_VERSION" ;;
        'compose exec -T mileage-viewer printenv DISPLAY_TZ') echo UTC ;;
        run*)
            printf '%s\n' "$@" > "$WORK/python-args"
            while [[ $1 != python3 ]]; do shift; done
            shift
            if [[ $1 == /work/stamp_version.py ]]; then set -- "$WORK/stamp_version.py" "${@:2}"; fi
            PYTHONPATH="$WORK" "$REAL_PYTHON" "$@" ;;
        *) return 88 ;;
    esac
}
df() {
    echo "$*" >> "$WORK/df-calls"
    printf 'Filesystem 1024-blocks Used Available Capacity Mounted\nfixture 9999999 0 9000000 0%% /\n'
}
eval "$STEP"
printf 'FAILURES=%s\n' "$FAILURES"
'''


@unittest.skipIf(os.name == "nt" or not shutil.which("bash"), "needs POSIX bash (WSL)")
class InstallerUnraid(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="unraid-installer-")
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        (self.work / "data").mkdir()
        (self.work / "backup").mkdir()
        (self.work / "docker-storage").mkdir()
        self.loaded = self.work / "loaded-cron"
        self.loaded.write_text("")
        self.cron = self.work / "cron"
        self.cron.mkdir()
        update = self.work / "update_cron"
        update.write_text('#!/bin/bash\ncat "$MILEAGE_UNRAID_CRON_DIR"/*.cron > "$LOADED"\necho update >> "$WORK/update-calls"\n')
        update.chmod(0o755)
        self.cfg = self.work / "docker.cfg"
        self.cfg.write_text('DOCKER_ENABLED="yes"\n')
        self.env = dict(os.environ, INSTALL=str(INSTALL), WORK=str(self.work),
                        MILEAGE_PLATFORM="unraid", REAL_PYTHON=sys.executable,
                        MILEAGE_UNRAID_DOCKER_CFG=str(self.cfg),
                        MILEAGE_UNRAID_CRON_DIR=str(self.cron),
                        MILEAGE_UPDATE_CRON=str(update), LOADED=str(self.loaded))

    def run_step(self, step, ok=True, **env):
        result = subprocess.run(["bash", "-c", HARNESS], text=True, capture_output=True,
                                env=dict(self.env, STEP=step, **env), timeout=30)
        self.assertFalse((self.work / "forbidden").exists(), result.stdout + result.stderr)
        if ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout + result.stderr

    def conf(self, remote="/mnt/remote/snapshots", backup="/mnt/pool/snapshots"):
        (self.work / "backup/backup.conf").write_text(
            f"REMOTE='{remote}'\nBACKUP_DIR='{backup}'\n"
            "BACKUP_MOUNT='/mnt/pool'\nREMOTE_MOUNT='/mnt/pool'\n")

    def scheduled(self, status="0", success="2099-01-01T00:00:00Z"):
        (self.work / "backup/scheduled.sh").write_text(textwrap.dedent(f'''\
            if [[ ${{1:-}} == --status ]]; then
                printf 'last_attempt_start=\nlast_attempt_end=\nlast_attempt_status={status}\nlast_success={success}\nlog=%s/backup/logs/backup.log\n' "$WORK"
            else
                echo scheduled >> "$WORK/backup-calls"
            fi
            '''))

    def test_privilege_planning_never_prompts_or_uses_debian_tools(self):
        for mode in ("install", "check"):
            out = self.run_step(f'MODE={mode}; need_sudo; plan_sudo; sudo_for test; ask_root_options; as_root printf direct')
            self.assertIn("direct", out)
            self.assertNotIn("Set up backups?", out)
            self.assertNotIn("Make Docker wait", out)

    def test_configure_sets_restart_policy_only_on_unraid(self):
        for platform in ("unraid", "debian"):
            (self.work / ".env").write_text("INGEST_AUTH_TOKEN=" + "a" * 32 + "\n")
            self.run_step('setup_bind_addr() { :; }; setup_viewer_proxy() { :; }; setup_display_tz() { :; }; setup_env',
                          MILEAGE_PLATFORM=platform)
            self.assertEqual("RESTART_POLICY=always" in (self.work / ".env").read_text(), platform == "unraid")

    def test_missing_required_command_and_coreutils_features_give_unraid_hints(self):
        out = self.run_step(r'''
            command() { if [[ $* == '-v ssh-keygen' ]]; then return 1; else builtin command "$@"; fi; }
            check_unraid_commands
            ''', ok=False)
        self.assertIn("Missing required Unraid command: ssh-keygen", out)
        out = self.run_step("stat() { return 1; }; check_unraid_commands", ok=False)
        self.assertIn("GNU stat -c", out)
        out = self.run_step("date() { return 1; }; check_unraid_commands", ok=False)
        self.assertIn("GNU date -d", out)

    def test_unraid_install_and_check_follow_main_without_debian_commands(self):
        self.conf()
        self.scheduled()
        (self.work / "backup/backup.sh").write_text("exit 0\n")
        (self.work / "backup/restore.sh").write_text('echo restore >> "$WORK/backup-calls"\n')
        for mode in ("install", "check"):
            out = self.run_step(f'MODE={mode}; ' + r'''
                preflight() { plan_sudo; }
                configure() { :; }
                build_images() { :; }
                setup_viewer_login() { :; }
                setup_everyday_login() { :; }
                start_services() { :; }
                verify_services() { return 0; }
                verify_end_to_end() { :; }
                setup_outputs() { :; }
                main
                ''')
            self.assertIn("Boot recovery remains unverified", out)
            self.assertNotIn("sudo reboot", out)

    def test_root_preflight_allows_backups_with_real_container_uid_mismatch(self):
        if os.geteuid() != 0:
            self.skipTest("requires root for the real EUID preflight gate")
        shutil.copytree(SERVER, self.work, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("data", "__pycache__", ".env", "viewer.env", "monitor.env", "backup.conf"))
        self.scheduled()
        out = self.run_step(r'''
            SCRIPT_DIR=/mnt/pool/appdata/fixture
            stat() { local args=("$@"); args[${#args[@]}-1]=${args[${#args[@]}-1]/$SCRIPT_DIR/$WORK}; command stat "${args[@]}"; }
            preflight
            printf 'DATA_WRAPPER=%s\n' "${DATA_SUDO[*]}"
            ''')
        self.assertIn("Root can back up data/", out)
        self.assertIn("DATA_WRAPPER=\n", out)

    def test_debian_still_refuses_root(self):
        if os.geteuid() != 0:
            self.skipTest("requires root for the real EUID preflight gate")
        self.assertIn("normal login user", self.run_step("MILEAGE_PLATFORM=debian; preflight", ok=False))

    def test_compose_any_major_at_least_two_and_two_storage_filesystems(self):
        for version in ("2.38.2", "5.6.0", "v10.0.0"):
            self.run_step("setup_unraid_docker", COMPOSE_VERSION=version)
        calls = (self.work / "df-calls").read_text()
        self.assertIn(str(self.work / "docker-storage"), calls)
        self.assertIn(str(self.work), calls)
        for version in ("1.29.2", "garbage"):
            self.assertIn("Compose Manager Plus", self.run_step("setup_unraid_docker", ok=False, COMPOSE_VERSION=version))

    def test_daemon_failure_has_unraid_fix(self):
        out = self.run_step("setup_unraid_docker", ok=False, DOCKER_STATUS="1")
        self.assertIn("Settings > Docker", out)
        self.assertNotIn("docker group", out)

    def test_restart_policies_are_checked_without_systemd_in_both_modes(self):
        for mode in ("install", "check"):
            out = self.run_step(f'MODE={mode}; verify_reboots; echo BOOT=$BOOT_UNVERIFIED', OPTIONAL="yes")
            self.assertIn("BOOT=yes", out)
            self.assertIn("unverified", out)
        out = self.run_step("MODE=check; verify_reboots", POLICY="unless-stopped")
        self.assertIn("FAILURES=2", out)
        self.cfg.write_text('DOCKER_ENABLED="no"\n')
        self.assertIn("DOCKER_ENABLED isn't yes", self.run_step("MODE=check; verify_reboots"))

    def test_debian_future_compose_major_is_accepted(self):
        self.run_step('MILEAGE_PLATFORM=debian; install_packages() { :; }; have_systemd() { return 1; }; setup_docker', COMPOSE_VERSION="10.0.0")

    def test_python_fallback_stamps_validates_proxy_and_parses_addresses(self):
        for name in ("stamp_version.py", "VERSION", "COMMIT"):
            shutil.copy(SERVER / name, self.work / name)
        (self.work / "COMMIT").write_text("a" * 40)
        shutil.copytree(SERVER / "common", self.work / "common")
        (self.work / "mileage-ingest").mkdir()
        shutil.copy(SERVER / "mileage-ingest/Dockerfile", self.work / "mileage-ingest/Dockerfile")
        (self.work / ".env").write_text("VIEWER_BIND_ADDR=127.0.0.1\nVIEWER_TRUSTED_PROXIES=192.0.2.1\n")
        out = self.run_step(r'''
            command() { if [[ $* == '-v python3' ]]; then return 1; else builtin command "$@"; fi; }
            select_python
            build_images
            setup_viewer_proxy
            printf '2: test inet 192.0.2.1/24\n' | host_python address -c 'import ipaddress,sys; print(ipaddress.ip_address(sys.stdin.read().split()[3].split("/")[0]))'
            ''')
        self.assertIn("BUILD_STAMP=v0.08+aaaaaaaaaa", out)
        self.assertEqual(out.count("Installer Python: container"), 1)
        calls = (self.work / "docker-calls").read_text()
        self.assertIn("--network none --user 1000:1000", calls)
        self.assertIn("/common/version.py:/work/common/version.py:ro", calls)
        self.assertIn("/common/settings.py:/work/common/settings.py:ro", calls)
        self.assertNotIn("/.env:", calls)
        self.assertNotIn("/data:", calls)
        self.assertIn("192.0.2.1", out)

    def test_python_fallback_rejects_bad_proxy_settings(self):
        shutil.copytree(SERVER / "common", self.work / "common")
        (self.work / "mileage-ingest").mkdir()
        shutil.copy(SERVER / "mileage-ingest/Dockerfile", self.work / "mileage-ingest/Dockerfile")
        (self.work / ".env").write_text("VIEWER_TRUSTED_PROXIES=bad\n")
        self.assertIn("Invalid viewer proxy configuration", self.run_step("PYTHON_ROUTE=container; setup_viewer_proxy", ok=False))

    def test_mount_guard_refuses_ram_missing_mount_and_volatile_paths_before_creation(self):
        for env in ({"MOUNT_TARGET": "/"}, {"MOUNT_FS": "tmpfs"},
                    {"MOUNT_FS": "rootfs"}, {"MOUNT_MISSING": "yes"}):
            self.run_step('mkdir() { forbidden mkdir "$@"; }; local_target_ok /mnt/unmounted/backup', ok=False, **env)
        for path in ("/boot/backups", "/root/backups", "/tmp/backups"):
            self.run_step(f'unraid_mount {path}', ok=False)

    def test_records_both_mounts_and_warns_about_same_disk_and_same_host(self):
        self.conf()
        out = self.run_step("prepare_unraid_backup_paths")
        conf = (self.work / "backup/backup.conf").read_text()
        self.assertIn("BACKUP_MOUNT='/mnt/pool'", conf)
        self.assertIn("REMOTE_MOUNT='/mnt/pool'", conf)
        self.assertIn("same filesystem", out)
        self.assertIn("isn't an off-host copy", out)

    def test_local_backup_dir_has_no_home_default_and_accepts_preset(self):
        self.conf(backup="")
        self.run_step("prepare_unraid_backup_paths", ok=False)
        self.assertNotIn("/root/mileage-backups", (self.work / "backup/backup.conf").read_text())
        self.run_step("prepare_unraid_backup_paths", INSTALL_BACKUP_DIR="/mnt/pool/snapshots")

    def test_cron_is_exact_idempotent_loaded_and_relocatable(self):
        self.run_step("install_unraid_cron; install_unraid_cron")
        file = self.cron / "mileage-tracker-backup.cron"
        expected = ("# Mileage tracker backup. Written by install.sh; rerun it to change.\n"
                    f"30 3 * * * /bin/bash {self.work}/backup/scheduled.sh --delay 900\n"
                    f"15 * * * * /bin/bash {self.work}/backup/scheduled.sh --if-overdue 26\n")
        self.assertEqual(file.read_text(), expected)
        moved = self.work / "moved"
        self.run_step('SCRIPT_DIR="$WORK/moved"; install_unraid_cron')
        self.assertIn(str(moved), file.read_text())
        self.loaded.write_text("")
        self.run_step('SCRIPT_DIR="$WORK/moved"; unraid_cron_current', ok=False)

    def test_root_backup_setup_runs_wrapper_before_loading_cron_and_rerun_is_idle(self):
        self.conf()
        self.scheduled()
        (self.work / "backup/backup.sh").write_text('exit 0\n')
        (self.work / "backup/restore.sh").write_text('echo restore >> "$WORK/backup-calls"\n')
        out = self.run_step("setup_backups; setup_backups")
        self.assertNotIn("Skipped backups", out)
        self.assertEqual((self.work / "backup-calls").read_text().splitlines(), ["scheduled", "restore"])
        self.assertEqual((self.work / "update-calls").read_text().splitlines(), ["update"])
        out = self.run_step("MODE=check; setup_backups")
        self.assertIn("FAILURES=0", out)
        self.assertEqual((self.work / "backup-calls").read_text().splitlines(), ["scheduled", "restore", "restore"])

    def test_persistent_ssh_key_and_known_hosts_used_for_every_connection(self):
        out = self.run_step(r'''
            ssh-keygen() {
                while [[ $1 != -f ]]; do shift; done
                printf synthetic > "$2"; printf public > "$2.pub"
            }
            ssh() { printf '%s\n' "$*" >> "$WORK/ssh-calls"; [[ -f "$WORK/copied" ]]; }
            ssh-copy-id() { printf '%s\n' "$*" >> "$WORK/ssh-calls"; touch "$WORK/copied"; }
            unraid_ssh_target_ok backup@host.invalid:/snapshots
            printf 'RSYNC=%s\n' "$RSYNC_SSH_CMD"
            ''')
        calls = (self.work / "ssh-calls").read_text()
        self.assertNotIn("/root/.ssh", calls)
        self.assertIn(str(self.work / "backup/ssh/id_ed25519.pub"), calls)
        self.assertIn(f"UserKnownHostsFile={self.work}/backup/ssh/known_hosts", calls)
        self.assertIn(f"RSYNC=ssh -i {self.work}/backup/ssh/id_ed25519 -o IdentitiesOnly=yes -o UserKnownHostsFile={self.work}/backup/ssh/known_hosts", out)
        self.assertEqual((self.work / "backup/ssh").stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.work / "backup/ssh/id_ed25519").stat().st_mode & 0o777, 0o600)

    def test_recent_success_does_not_hide_failed_last_attempt_and_shows_log_tail(self):
        self.conf()
        self.scheduled(status="23")
        logs = self.work / "backup/logs"
        logs.mkdir()
        (logs / "backup.log").write_text("".join(f"backup line {i}\n" for i in range(20)))
        out = self.run_step("install_unraid_cron; MODE=check; verify_unraid_backups")
        self.assertIn("last scheduled backup failed", out)
        self.assertIn("FAILURES=1", out)
        self.assertIn(str(logs / "backup.log"), out)
        self.assertIn("backup line 19", out)
        self.assertNotIn("backup line 9\n", out)

    def test_stale_missing_invalid_success_and_missing_loaded_cron_fail(self):
        self.conf()
        for success in ("", "invalid", "2000-01-01T00:00:00Z"):
            self.scheduled(success=success)
            out = self.run_step("MODE=check; verify_unraid_backups")
            self.assertIn("No good backup within the last two days", out)
            self.assertIn("loaded crontab", out)

    def test_no_ss_warns_and_leaves_port_check_to_docker(self):
        out = self.run_step(r'''
            command() { if [[ $* == '-v ss' ]]; then return 1; else builtin command "$@"; fi; }
            container_state() { echo missing; }
            check_port_free mileage-ingest 8090
            ''')
        self.assertIn("Docker will check port 8090", out)

    def test_timezone_never_calls_timedatectl(self):
        self.run_step("host_tz; tz_names >/dev/null")

    def test_data_symlink_is_refused_without_changing_target(self):
        (self.work / "data").rmdir()
        outside = self.work / "outside"
        outside.mkdir()
        before = outside.stat()
        (self.work / "data").symlink_to(outside, target_is_directory=True)
        self.run_step("setup_data_dir", ok=False)
        self.assertEqual(outside.stat().st_mode, before.st_mode)

    def test_actual_root_owns_no_trip_files_and_does_not_follow_nested_links(self):
        if os.geteuid() != 0:
            self.skipTest("requires root in disposable test process for actual chown")
        outside = self.work / "outside"
        outside.mkdir()
        nested = outside / "keep"
        nested.write_text("synthetic")
        original_uid = nested.stat().st_uid
        (self.work / "data/link").symlink_to(outside, target_is_directory=True)
        self.run_step("setup_data_dir")
        self.assertEqual((self.work / "data").stat().st_uid, 1000)
        self.assertEqual((self.work / "data").stat().st_mode & 0o777, 0o700)
        self.assertEqual(nested.stat().st_uid, original_uid)


if __name__ == "__main__":
    unittest.main()

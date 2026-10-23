"""install.sh asks for sudo before the work, and only if it will need it (#22).

Sources the real install.sh functions (everything but the final `main`)
into bash and runs plan_sudo (preflight) or ask_root_options (after
Configuration) against fakes for apt, Docker, systemd and the backup
timer. need_sudo is replaced by a stub that records the ask. Needs bash,
so it skips on Windows.
"""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap
import unittest


INSTALL = Path(__file__).resolve().parents[1] / "install.sh"

HARNESS = textwrap.dedent("""\
    source <(sed '$d' "$INSTALL")
    MODE=$FAKE_MODE
    CONTAINER_UID=${FAKE_CONTAINER_UID:-$(id -u)}
    BIND_ADDR=$FAKE_BIND
    if [[ $FAKE_OTHER_UID == yes ]]; then DATA_SUDO=(sudo); fi
    need_sudo() { printf 'ASKED: %s\\n' "$1"; SUDO_READY=yes; }
    have_apt() { true; }
    have_systemd() { true; }
    dpkg-query() {
        if [[ " $FAKE_MISSING " == *" ${*: -1} "* ]]; then return 1; fi
        printf 'install ok installed'
    }
    command() {
        if [[ $* == '-v docker' ]]; then
            [[ $FAKE_DOCKER != missing ]]
        else
            builtin command "$@"
        fi
    }
    docker() { [[ $FAKE_DOCKER == ok ]]; }
    systemctl() {
        case $1 in
            is-active) echo active ;;
            is-enabled) echo enabled ;;
            *) return 0 ;;
        esac
    }
    wait_online_unit() { printf 'fake-wait-online.service'; }
    wait_online_enabled() { [[ $FAKE_WAIT_ONLINE == enabled ]]; }
    backups_configured() { [[ $FAKE_BACKUPS != none ]]; }
    backup_units_current() { true; }
    timer_ok() { [[ $FAKE_BACKUPS == timer ]]; }
    timer_current() { true; }
    cd "$RUN_DIR"
    eval "$FAKE_STEP"
    printf 'WANT_WAIT_ONLINE=%s WANT_BACKUPS=%s\\n' "$WANT_WAIT_ONLINE" "$WANT_BACKUPS"
    printf 'DONE\\n'
    """)


@unittest.skipIf(os.name == "nt" or shutil.which("bash") is None, "needs bash")
class InstallerSudo(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        (self.dir / "data").mkdir()

    def run_step(self, step, *, stdin="", mode="install", missing="", docker="ok",
                 other_uid="no", container_uid="", backups="timer", bind="",
                 wait_online="enabled", **answers):
        env = {k: v for k, v in os.environ.items() if not k.startswith("INSTALL_")}
        env.update(INSTALL=str(INSTALL), RUN_DIR=str(self.dir), FAKE_STEP=step,
                   FAKE_MODE=mode, FAKE_MISSING=missing, FAKE_DOCKER=docker,
                   FAKE_OTHER_UID=other_uid, FAKE_CONTAINER_UID=container_uid,
                   FAKE_BACKUPS=backups, FAKE_BIND=bind,
                   FAKE_WAIT_ONLINE=wait_online, **answers)
        proc = subprocess.run(["bash", "-c", HARNESS], env=env, input=stdin,
                              text=True, capture_output=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("DONE", proc.stdout)
        return proc.stdout

    def plan(self, **kw):
        return self.run_step("plan_sudo", **kw)

    def options(self, stdin, **kw):
        return self.run_step("ask_root_options", stdin=stdin, **kw)

    def assertAsksFor(self, out, reason):
        self.assertEqual(out.count("ASKED"), 1, out)
        self.assertIn(reason, out)

    # --- plan_sudo, in preflight: what will need root regardless ---

    def test_finished_install_does_not_ask_in_either_mode(self):
        for mode in ("install", "check"):
            with self.subTest(mode=mode):
                out = self.plan(mode=mode)
                self.assertNotIn("ASKED", out)
                self.assertIn("Nothing needs root so far", out)

    def test_missing_packages_ask_up_front(self):
        self.assertAsksFor(self.plan(missing="jq rsync"), "install packages: jq rsync")

    def test_missing_docker_asks_up_front(self):
        self.assertAsksFor(self.plan(docker="missing"), "install Docker")

    def test_docker_without_group_asks_in_either_mode(self):
        for mode in ("install", "check"):
            with self.subTest(mode=mode):
                self.assertAsksFor(self.plan(mode=mode, docker="denied"),
                                   "isn't in the docker group")

    def test_other_uid_asks_for_the_test_trip(self):
        self.assertAsksFor(self.plan(mode="check", other_uid="yes"),
                           "delete the test trip")

    def test_data_dir_owned_by_someone_else_asks(self):
        self.assertAsksFor(self.plan(container_uid="99999"), "hand data/ to UID 99999")

    def test_broken_backup_timer_asks_on_install_only(self):
        self.assertAsksFor(self.plan(backups="configured"), "set up the daily backup timer")
        self.assertNotIn("ASKED", self.plan(mode="check", backups="configured"))

    def test_optional_steps_are_not_asked_about_in_preflight(self):
        out = self.plan(backups="none", bind="192.0.2.10", wait_online="disabled")
        self.assertNotIn("ASKED", out)
        self.assertNotIn("Set up backups?", out)

    # --- ask_root_options, after Configuration, before the build ---

    def test_nothing_optional_left_asks_nothing(self):
        out = self.options("", bind="192.0.2.10")
        self.assertNotIn("ASKED", out)
        self.assertNotIn("Make Docker wait", out)
        self.assertNotIn("Set up backups?", out)

    def test_declining_both_optional_steps_never_asks_for_sudo(self):
        out = self.options("n\nn\n", backups="none", bind="192.0.2.10",
                           wait_online="disabled")
        self.assertNotIn("ASKED", out)
        self.assertIn("WANT_WAIT_ONLINE=no WANT_BACKUPS=no", out)

    def test_newly_chosen_bind_address_asks_before_the_build(self):
        # No BIND_ADDR in .env at preflight; Configuration just chose one.
        self.assertNotIn("ASKED", self.plan(wait_online="disabled"))
        out = self.options("y\n", bind="192.0.2.10", wait_online="disabled")
        self.assertAsksFor(out, "make Docker wait for the network at boot")
        self.assertIn("WANT_WAIT_ONLINE=yes", out)

    def test_no_bind_address_skips_the_wait_online_question(self):
        out = self.options("", bind="", wait_online="disabled")
        self.assertNotIn("ASKED", out)
        self.assertIn("WANT_WAIT_ONLINE= ", out)

    def test_yes_to_backups_asks_once_with_the_reason(self):
        out = self.options("y\n", backups="none", bind="192.0.2.10")
        self.assertAsksFor(out, "set up the daily backup timer")
        self.assertIn("WANT_BACKUPS=yes", out)

    def test_backup_answer_can_come_from_the_environment(self):
        out = self.options("", backups="none", INSTALL_BACKUPS="n")
        self.assertNotIn("ASKED", out)
        self.assertIn("WANT_BACKUPS=no", out)

    def test_backups_not_offered_to_another_uid(self):
        out = self.options("", backups="none", container_uid="99999")
        self.assertNotIn("Set up backups?", out)
        self.assertNotIn("ASKED", out)

    def test_already_asked_in_preflight_does_not_ask_again(self):
        harness_out = self.run_step(
            "plan_sudo; ask_root_options", stdin="y\n", missing="jq",
            backups="none", bind="192.0.2.10")
        self.assertEqual(harness_out.count("ASKED"), 1, harness_out)
        self.assertIn("WANT_BACKUPS=yes", harness_out)


if __name__ == "__main__":
    unittest.main()

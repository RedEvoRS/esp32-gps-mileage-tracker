"""install.sh asks for sudo up front only when a step will need it (#22).

Sources the real install.sh functions (everything but the final `main`)
into bash and runs plan_sudo against fakes for apt, Docker and the
backup timer. need_sudo is replaced by a stub that records the ask.
Needs bash, so it skips on Windows.
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
    CONTAINER_UID=$(id -u)
    if [[ $FAKE_OTHER_UID == yes ]]; then DATA_SUDO=(sudo); fi
    need_sudo() { printf 'ASKED: %s\\n' "$1"; }
    have_apt() { true; }
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
    backups_configured() { [[ $FAKE_BACKUPS != none ]]; }
    timer_ok() { [[ $FAKE_BACKUPS == timer ]]; }
    timer_current() { true; }
    cd "$RUN_DIR"
    plan_sudo
    printf 'DONE\\n'
    """)


@unittest.skipIf(os.name == "nt" or shutil.which("bash") is None, "needs bash")
class InstallerPlanSudo(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        (self.dir / "data").mkdir()

    def plan(self, *, mode="install", missing="", docker="ok", other_uid="no",
             backups="timer", harness=HARNESS, **answers):
        env = {k: v for k, v in os.environ.items() if not k.startswith("INSTALL_")}
        env.update(INSTALL=str(INSTALL), RUN_DIR=str(self.dir),
                   FAKE_MODE=mode, FAKE_MISSING=missing, FAKE_DOCKER=docker,
                   FAKE_OTHER_UID=other_uid, FAKE_BACKUPS=backups, **answers)
        proc = subprocess.run(["bash", "-c", harness], env=env, text=True,
                              capture_output=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("DONE", proc.stdout)
        return proc.stdout

    def assertNoAsk(self, out):
        self.assertNotIn("ASKED", out)
        self.assertIn("Nothing needs root this time", out)

    def assertAsksFor(self, out, reason):
        self.assertIn("ASKED", out)
        self.assertIn(reason, out)

    def test_finished_install_does_not_ask_in_either_mode(self):
        for mode in ("install", "check"):
            with self.subTest(mode=mode):
                self.assertNoAsk(self.plan(mode=mode))

    def test_missing_packages_ask_up_front(self):
        out = self.plan(missing="jq rsync")
        self.assertAsksFor(out, "install packages: jq rsync")

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
        harness = HARNESS.replace("CONTAINER_UID=$(id -u)", "CONTAINER_UID=99999")
        self.assertAsksFor(self.plan(harness=harness), "hand data/ to UID 99999")

    def test_backup_timer_missing_asks_on_install_only(self):
        self.assertAsksFor(self.plan(backups="configured"), "set up the daily backup timer")
        self.assertNoAsk(self.plan(mode="check", backups="configured"))

    def test_first_backup_setup_asks_unless_declined_in_advance(self):
        self.assertAsksFor(self.plan(backups="none"), "if you set up backups")
        self.assertNoAsk(self.plan(backups="none", INSTALL_BACKUPS="n"))


if __name__ == "__main__":
    unittest.main()

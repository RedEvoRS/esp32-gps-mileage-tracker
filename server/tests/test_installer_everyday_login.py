"""install.sh's admin login, the optional everyday login, the login test for
it, the --check listing and the closing summary (#429).

Sources the real install.sh functions (everything but the final `main`)
into bash, with `compose` replaced by a stand-in for manage_users.py and
`curl` by a fake viewer, so nothing needs Docker or a server. The stand-ins
write down what they were given (arguments, stdin), so the tests can check
that a password only ever travels on stdin. Needs bash, so it skips on
Windows.
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
PASSWORD = "everyday-pass-123"
ADMIN_PASSWORD = "admin-pass-456"
HASH = "scrypt:32768:8:1$c2FsdA$0123456789abcdef"

HARNESS = textwrap.dedent("""\
    source <(sed '$d' "$INSTALL")
    cd "$WORK"
    SCRIPT_DIR=$WORK
    BIND_ADDR=192.0.2.1
    MODE=${TEST_MODE:-install}
    VIEWER_URL=http://viewer.invalid
    # No real terminal in tests; TERMINAL=1 pretends there is one, with
    # the answers on stdin.
    have_terminal() { [[ -n ${TERMINAL:-} ]]; }
    container_state() { printf '%s' "${FAKE_STATE:-running}"; }
    # manage_users.py and the hash maker, as seen through `docker compose`.
    # FAKE_ADD_RCS is a list of exit codes for successive `add` calls.
    compose() {
        printf '%s\\n' "$*" >> compose.log
        case " $* " in
            *" manage_users.py list "*)
                printf '%b' "${FAKE_LIST:-}"
                return "${FAKE_LIST_RC:-0}"
                ;;
            *" manage_users.py add "*)
                cat >> add.stdin
                [[ -z ${FAKE_ERR:-} ]] || printf '%s\n' "$FAKE_ERR" >&2
                local rcs
                [[ -f add.rcs ]] || printf '%s' "${FAKE_ADD_RCS:-0}" > add.rcs
                rcs=$(< add.rcs)
                if [[ $rcs == *" "* ]]; then printf '%s' "${rcs#* }" > add.rcs; fi
                return "${rcs%% *}"
                ;;
            *" python -c "*)
                cat > /dev/null
                printf '%s\\n' "$FAKE_HASH"
                ;;
        esac
    }
    # The viewer: a login page with a CSRF token, a login that answers
    # LOGIN_CODE, and pages that answer ROOT_CODE and ADMIN_CODE.
    curl() {
        local out= fmt= url=${*: -1} arg prev=
        printf '%s\\n' "$*" >> curl.log
        for arg in "$@"; do
            case $prev in
                -o) out=$arg ;;
                -w) fmt=$arg ;;
            esac
            if [[ $arg == password@- ]]; then cat > login.stdin; fi
            prev=$arg
        done
        local body='<input type="hidden" name="csrf_token" value="tok123">'
        local code=200
        case $url in
            */login) [[ -z $fmt ]] || { printf '%s' "${LOGIN_CODE:-302 http://viewer.invalid/}"; return 0; } ;;
            */logout) code=302 ;;
            */admin) code=${ADMIN_CODE:-403} ;;
            *) code=${ROOT_CODE:-200} ;;
        esac
        if [[ -n $out && $out != /dev/null ]]; then printf '%s' "$body" > "$out"
        elif [[ -z $out ]]; then printf '%s' "$body"; fi
        if [[ $fmt == *http_code* ]]; then printf '%s' "$code"; fi
    }
    """)


def bash_missing():
    if os.name == "nt":
        return "needs a POSIX shell"
    if shutil.which("bash") is None:
        return "bash not installed"
    return None


@unittest.skipIf(bash_missing(), bash_missing())
class InstallerEverydayLoginTests(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="everyday login "))
        self.addCleanup(shutil.rmtree, self.work)
        shutil.copy(SERVER / "viewer.env.example", self.work / "viewer.env.example")

    def viewer_env(self, user="admin", hash_value="replace-me"):
        (self.work / "viewer.env").write_text(
            f"VIEWER_USERNAME={user}\nVIEWER_PASSWORD_HASH={hash_value}\n")

    def run_script(self, script, stdin="", status=0, **env):
        env = {**os.environ, "INSTALL": str(INSTALL), "WORK": str(self.work),
               "FAKE_HASH": HASH, **env}
        # None removes a variable, so a developer's own INSTALL_* answers
        # can't leak into the tests.
        env = {name: value for name, value in env.items() if value is not None}
        proc = subprocess.run(["bash", "-c", HARNESS + script], input=stdin, env=env,
                              text=True, capture_output=True, timeout=60)
        self.assertEqual(proc.returncode, status, proc.stdout + proc.stderr)
        return proc.stdout + proc.stderr

    def read(self, name):
        path = self.work / name
        return path.read_text() if path.exists() else ""

    def everyday(self, status=0, stdin="", **env):
        # Run the optional step, then show what it left behind.
        output = self.run_script(
            'setup_everyday_login\n'
            'printf "USER=%s PASSWORD=%s\\n" "$EVERYDAY_USER" "$EVERYDAY_PASSWORD"\n'
            'printf "LOGINS=%s\\n" "$(everyday_text)"\n',
            stdin=stdin, status=status, **env)
        return output

    def clean_env(self):
        return {name: None for name in os.environ if name.startswith("INSTALL_")}

    # --- Admin wording ------------------------------------------------

    def admin_login(self, stdin="", **env):
        return self.run_script(
            'setup_viewer_login\n', stdin=stdin,
            **{**self.clean_env(), "INSTALL_VIEWER_PASSWORD": ADMIN_PASSWORD, **env})

    def test_fresh_install_suggests_admin_as_the_username(self):
        self.viewer_env(user="replace-me")
        output = self.admin_login(stdin="\n")
        self.assertIn("Admin username [admin]", output)
        self.assertIn("Admin password (at least 8 characters): (from INSTALL_VIEWER_PASSWORD)", output)
        self.assertIn("Admin login set for admin", output)
        self.assertIn("maintainer's login", output)
        self.assertIn("VIEWER_USERNAME=admin", self.read("viewer.env"))
        self.assertNotIn(ADMIN_PASSWORD, self.read("viewer.env"))

    def test_existing_username_stays_the_default(self):
        self.viewer_env(user="maintainer")
        output = self.admin_login(stdin="\n")
        self.assertIn("Admin username [maintainer]", output)
        self.assertIn("VIEWER_USERNAME=maintainer", self.read("viewer.env"))

    def test_admin_presets_still_work(self):
        self.viewer_env(user="replace-me")
        output = self.admin_login(INSTALL_VIEWER_USER="boss")
        self.assertIn("(from INSTALL_VIEWER_USER)", output)
        self.assertIn("VIEWER_USERNAME=boss", self.read("viewer.env"))

    def test_kept_admin_login_is_named_admin(self):
        self.viewer_env(user="maintainer", hash_value=f"'{HASH}'")
        output = self.admin_login(INSTALL_KEEP_LOGIN="y")
        self.assertIn("The admin login for maintainer is already set up. Keep it?", output)
        self.assertIn("Kept the admin login for maintainer", output)

    # --- The optional step ----------------------------------------------

    def test_skipped_without_terminal_and_presets(self):
        self.viewer_env()
        output = self.everyday(**self.clean_env())
        self.assertIn("Skipped adding an everyday login", output)
        self.assertIn("INSTALL_ADD_EDITOR=y", output)
        self.assertNotIn(" add ", self.read("compose.log"))
        self.assertIn("USER= PASSWORD=", output)

    def test_preset_no_adds_nothing(self):
        self.viewer_env()
        output = self.everyday(**{**self.clean_env(), "INSTALL_ADD_EDITOR": "n"})
        self.assertIn("(from INSTALL_ADD_EDITOR)", output)
        self.assertIn("No everyday login added", output)
        self.assertNotIn(" add ", self.read("compose.log"))

    def preset(self, **extra):
        return {**self.clean_env(), "INSTALL_ADD_EDITOR": "y", "INSTALL_EDITOR_USER": "helper",
                "INSTALL_EDITOR_PASSWORD": PASSWORD, **extra}

    def test_preset_path_sends_the_password_on_stdin_only(self):
        self.viewer_env()
        output = self.everyday(**self.preset())
        self.assertIn("Everyday login added for helper", output)
        self.assertIn(f"USER=helper PASSWORD={PASSWORD}", output.replace(
            "(from INSTALL_EDITOR_PASSWORD)", ""))
        log = self.read("compose.log")
        self.assertIn("run --rm --no-deps -T mileage-viewer python manage_users.py add helper", log)
        self.assertNotIn(PASSWORD, log)
        self.assertEqual(self.read("add.stdin"), PASSWORD)
        # Only the test's own PASSWORD= line may show it.
        self.assertEqual(output.count(PASSWORD), 1)

    def test_existing_logins_are_listed_and_kept(self):
        self.viewer_env()
        output = self.everyday(**{**self.clean_env(), "INSTALL_ADD_EDITOR": "n",
                                  "FAKE_LIST": "alice\\tenabled\\nbob\\tdisabled\\n"})
        self.assertIn("Everyday logins already set up, and kept: alice, bob (disabled)", output)
        self.assertIn("Add another everyday login? [y/N] n (from INSTALL_ADD_EDITOR)", output)
        self.assertNotIn(" add ", self.read("compose.log"))

    def test_terminal_default_is_yes_when_there_are_none(self):
        self.viewer_env()
        stdin = f"\nhelper\n{PASSWORD}\n{PASSWORD}\n"
        output = self.everyday(stdin=stdin, TERMINAL="1", **self.clean_env())
        self.assertIn("Add an everyday login for the person who'll annotate trips? [Y/n]", output)
        self.assertIn("Everyday login added for helper", output)
        self.assertEqual(self.read("add.stdin"), PASSWORD)
        self.assertNotIn(PASSWORD, output.replace(f"PASSWORD={PASSWORD}", ""))

    def test_terminal_default_is_no_when_some_exist(self):
        self.viewer_env()
        output = self.everyday(stdin="\n", TERMINAL="1", FAKE_LIST="alice\\tenabled\\n",
                               **self.clean_env())
        self.assertIn("Add another everyday login? [y/N]", output)
        self.assertNotIn(" add ", self.read("compose.log"))

    def test_terminal_asks_again_for_a_bad_or_admin_username(self):
        self.viewer_env(user="Boss")
        stdin = f"y\nbad name\nboss\nhelper\n{PASSWORD}\n{PASSWORD}\n"
        output = self.everyday(stdin=stdin, TERMINAL="1", **self.clean_env())
        self.assertEqual(output.count("not the admin's name (Boss)"), 2)
        self.assertIn("Everyday login added for helper", output)

    def test_terminal_asks_again_for_a_short_or_mismatched_password(self):
        self.viewer_env()
        stdin = f"y\nhelper\nshort\n{PASSWORD}\nother-pass-123\n{PASSWORD}\n{PASSWORD}\n"
        output = self.everyday(stdin=stdin, TERMINAL="1", **self.clean_env())
        self.assertIn("That's too short", output)
        self.assertIn("Those didn't match", output)
        self.assertEqual(self.read("add.stdin"), PASSWORD)

    def test_already_existing_login_is_kept_without_failing(self):
        self.viewer_env()
        output = self.everyday(**self.preset(FAKE_ADD_RCS="5", FAKE_ERR="that login already exists"))
        self.assertIn("An everyday login named helper already exists", output)
        self.assertIn("its password wasn't changed", output)
        # No password was set by this run, so there's nothing to log in with.
        self.assertIn("USER= PASSWORD=\n", output)

    def test_rejected_login_stops_an_install_with_presets(self):
        self.viewer_env()
        output = self.everyday(status=1, **self.preset(
            FAKE_ADD_RCS="3", FAKE_ERR="manage_users: the name is taken by the admin"))
        self.assertIn("the name is taken by the admin", output)
        self.assertIn("turned down the everyday login for helper", output)
        self.assertIn("INSTALL_EDITOR_USER", output)
        self.assertNotIn(PASSWORD, output)

    def test_rejected_login_asks_again_in_a_terminal(self):
        self.viewer_env()
        stdin = f"y\nhelper\n{PASSWORD}\n{PASSWORD}\nhelper2\n{PASSWORD}\n{PASSWORD}\n"
        output = self.everyday(stdin=stdin, TERMINAL="1", FAKE_ADD_RCS="3 0",
                               FAKE_ERR="manage_users: nope", **self.clean_env())
        self.assertIn("The viewer turned that down. Try again.", output)
        self.assertIn("Everyday login added for helper2", output)
        self.assertEqual(self.read("compose.log").count(" add "), 2)

    def test_other_failures_stop_with_the_commands_message(self):
        self.viewer_env()
        output = self.everyday(status=1, **self.preset(
            FAKE_ADD_RCS="1", FAKE_ERR="manage_users: database is locked"))
        self.assertIn("database is locked", output)
        self.assertIn("Couldn't create the everyday login for helper", output)
        self.assertNotIn(PASSWORD, output)

    def test_unlistable_logins_stop_an_install(self):
        self.viewer_env()
        output = self.everyday(status=1, FAKE_LIST_RC="1", **self.preset())
        self.assertIn("Couldn't list the everyday logins", output)

    def test_bad_presets_stop_an_install(self):
        self.viewer_env(user="Boss")
        for name, value, text in (
                ("INSTALL_EDITOR_USER", "boss", "isn't a usable username"),
                ("INSTALL_EDITOR_USER", "bad name", "isn't a usable username"),
                ("INSTALL_EDITOR_PASSWORD", "short", "shorter than 8"),
                ("INSTALL_EDITOR_PASSWORD", "x" * 1025, "longer than 1024")):
            with self.subTest(name=name, value=value[:12]):
                output = self.everyday(status=1, **self.preset(**{name: value}))
                self.assertIn(text, output)

    def test_main_runs_the_step_after_the_admin_login(self):
        script = INSTALL.read_text()
        self.assertRegex(script, r"setup_viewer_login\n\s+setup_everyday_login\n\s+start_services")

    # --- The login test -------------------------------------------------

    def verify(self, status=0, **env):
        return self.run_script(
            f'EVERYDAY_PASSWORD={PASSWORD}\nverify_everyday_login helper || true\n'
            'printf "FAILURES=%s\\n" "$FAILURES"\n',
            status=status, TEST_MODE="check", **env)

    def test_everyday_login_sees_the_trip_list_and_is_refused_by_admin(self):
        output = self.verify()
        self.assertIn("Logged in to the viewer as the everyday login helper", output)
        self.assertIn("The everyday login sees the trip list", output)
        self.assertIn("The admin page refuses the everyday login (403)", output)
        self.assertIn("FAILURES=0", output)
        # The password went to the login form on stdin, not into the arguments.
        self.assertEqual(self.read("login.stdin"), PASSWORD)
        self.assertNotIn(PASSWORD, self.read("curl.log"))
        self.assertNotIn(PASSWORD, output)
        self.assertEqual(self.read("curl.log").count("/login"), 2)  # page, then one attempt
        self.assertIn("/logout", self.read("curl.log"))

    def test_everyday_login_that_reaches_admin_is_a_problem(self):
        output = self.verify(ADMIN_CODE="200")
        self.assertIn("The everyday login helper could open the admin page", output)
        self.assertIn("FAILURES=1", output)

    def test_unexpected_admin_status_is_a_problem(self):
        output = self.verify(ADMIN_CODE="500")
        self.assertIn("HTTP 500 instead of 403", output)
        self.assertIn("FAILURES=1", output)

    def test_trip_list_that_does_not_load_is_a_problem(self):
        output = self.verify(ROOT_CODE="500")
        self.assertIn("The trip list didn't load for the everyday login helper (HTTP 500)", output)
        self.assertIn("FAILURES=1", output)

    def test_refused_password_is_a_problem_and_stops_there(self):
        output = self.verify(LOGIN_CODE="401 ")
        self.assertIn("turned down the password for the everyday login helper", output)
        self.assertIn("FAILURES=1", output)
        self.assertNotIn("/admin", self.read("curl.log"))

    def test_lockout_and_other_login_failures_are_problems(self):
        output = self.verify(LOGIN_CODE="429 ")
        self.assertIn("locked this server out", output)
        output = self.verify(LOGIN_CODE="500 ")
        self.assertIn("as the everyday login helper failed (HTTP 500)", output)

    # --- --check ----------------------------------------------------------

    def report(self, **env):
        return self.run_script(
            'report_everyday_logins\nprintf "FAILURES=%s WARNINGS=%s\\n" "$FAILURES" "${#WARNINGS[@]}"\n',
            TEST_MODE="check", **env)

    def test_check_lists_the_everyday_logins_from_the_running_viewer(self):
        output = self.report(FAKE_LIST="alice\\tenabled\\nbob\\tdisabled\\n")
        self.assertIn("Everyday logins: alice, bob (disabled)", output)
        self.assertIn("FAILURES=0 WARNINGS=0", output)
        self.assertIn("exec -T mileage-viewer python manage_users.py list", self.read("compose.log"))

    def test_check_says_where_to_add_the_first_one(self):
        output = self.report()
        self.assertIn("No everyday logins yet", output)
        self.assertIn("http://viewer.invalid/admin", output)
        self.assertIn("FAILURES=0 WARNINGS=0", output)

    def test_check_with_a_stopped_viewer_says_nothing(self):
        output = self.report(FAKE_STATE="exited")
        self.assertNotIn("ogin", output)
        self.assertEqual(self.read("compose.log"), "")
        self.assertIn("FAILURES=0 WARNINGS=0", output)

    def test_check_never_fails_when_the_list_cannot_be_had(self):
        output = self.report(FAKE_LIST_RC="1")
        self.assertIn("Couldn't list the everyday logins", output)
        self.assertIn("FAILURES=0 WARNINGS=0", output)

    # --- Summary ----------------------------------------------------------

    def summary(self, logins=()):
        (self.work / ".env").write_text("INGEST_AUTH_TOKEN=x\n")
        array = " ".join(f"'{name}'" for name in logins)
        return self.run_script(f'EVERYDAY_LOGINS=({array})\nsummary\n')

    def test_summary_names_the_admin_the_everyday_logins_and_the_admin_page(self):
        self.viewer_env(user="maintainer")
        output = self.summary(["helper", "guest (disabled)"])
        self.assertIn("The admin login is maintainer", output)
        self.assertIn("Everyday logins: helper, guest (disabled)", output)
        self.assertIn("http://192.0.2.1:8091/admin", output)

    def test_summary_without_everyday_logins_points_to_the_admin_page(self):
        self.viewer_env(user="maintainer")
        output = self.summary()
        self.assertIn("There are no everyday logins yet.", output)
        self.assertIn("http://192.0.2.1:8091/admin", output)

    def test_check_summary_is_unchanged(self):
        self.viewer_env()
        output = self.run_script('summary\n', TEST_MODE="check")
        self.assertIn("Everything checks out", output)
        self.assertNotIn("admin", output.lower())


if __name__ == "__main__":
    unittest.main()

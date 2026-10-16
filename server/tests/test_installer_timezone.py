"""install.sh asks for the display timezone instead of assuming one.

Sources the real install.sh functions (everything but the final `main`)
into bash, with timedatectl replaced by a fake that knows a few zones,
then runs setup_display_tz against a scratch .env with scripted answers.
Needs bash, so it skips on Windows.
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

ZONES = ["America/Chicago", "America/Denver", "America/Los_Angeles",
         "America/New_York", "Europe/London", "UTC"]

HARNESS = textwrap.dedent("""\
    source <(sed '$d' "$INSTALL")
    MODE=${FAKE_MODE:-install}
    timedatectl() {
        case $1 in
            list-timezones) printf '%s\\n' $FAKE_ZONES ;;
            show) printf '%s\\n' "$FAKE_HOST_TZ" ;;
        esac
    }
    cd "$WORK"
    setup_display_tz
    printf 'FAILURES=%s\\n' "$FAILURES"
    if (( FAILURES == 0 )); then MODE=check summary; fi
    """)


@unittest.skipIf(os.name == "nt" or shutil.which("bash") is None, "needs a POSIX shell")
class InstallerTimezone(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work)
        self.env = self.work / ".env"
        self.env.write_text("INGEST_AUTH_TOKEN=x\nDISPLAY_TZ=\n")

    def run_step(self, answers="", host_tz="UTC", mode="install", **extra):
        env = {**os.environ, "INSTALL": str(INSTALL), "WORK": str(self.work),
               "FAKE_ZONES": " ".join(ZONES), "FAKE_HOST_TZ": host_tz,
               "FAKE_MODE": mode, **extra}
        env.pop("INSTALL_DISPLAY_TZ", None)
        env.update(extra)
        return subprocess.run(["bash", "-c", HARNESS], input=answers, env=env,
                              capture_output=True, text=True, timeout=30)

    def zone_in_env(self):
        lines = [l for l in self.env.read_text().splitlines() if l.startswith("DISPLAY_TZ=")]
        self.assertEqual(len(lines), 1, lines)
        return lines[0].split("=", 1)[1]

    def test_offers_the_servers_own_zone(self):
        result = self.run_step("\n", host_tz="America/Denver")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("[America/Denver]", result.stdout)
        self.assertEqual(self.zone_in_env(), "America/Denver")

    def test_no_default_when_the_server_runs_on_utc(self):
        # Enter alone is refused; a search with one hit becomes the default.
        result = self.run_step("\nchicago\n\n", host_tz="UTC")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("[UTC]", result.stdout)
        self.assertIn("There's no default timezone", result.stdout)
        self.assertIn("That matches America/Chicago", result.stdout)
        self.assertEqual(self.zone_in_env(), "America/Chicago")

    def test_search_lists_several_matches(self):
        result = self.run_step("america\nEurope/London\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("America/New_York", result.stdout)
        self.assertEqual(self.zone_in_env(), "Europe/London")

    def test_any_capitalisation_of_a_real_name(self):
        result = self.run_step("america/new_york\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.zone_in_env(), "America/New_York")

    def test_preset_answer_and_bad_preset(self):
        result = self.run_step(INSTALL_DISPLAY_TZ="Europe/London")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.zone_in_env(), "Europe/London")

        self.env.write_text("DISPLAY_TZ=\n")
        result = self.run_step(INSTALL_DISPLAY_TZ="Pacific")
        self.assertEqual(result.returncode, 1)
        self.assertIn("INSTALL_DISPLAY_TZ=Pacific isn't a timezone name", result.stdout)
        self.assertEqual(self.zone_in_env(), "")

    def test_keeps_a_zone_already_set(self):
        self.env.write_text("DISPLAY_TZ=America/Chicago\n")
        result = self.run_step("", host_tz="America/Denver")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("Timezone", result.stdout.replace("DISPLAY_TZ", ""))
        self.assertEqual(self.zone_in_env(), "America/Chicago")

    def test_asks_again_for_a_zone_that_isnt_real(self):
        self.env.write_text("DISPLAY_TZ=Mars/Base\n")
        result = self.run_step("Europe/London\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("DISPLAY_TZ=Mars/Base in .env isn't a timezone name", result.stdout)
        self.assertEqual(self.zone_in_env(), "Europe/London")
        self.assertNotIn("Worth a look:", result.stdout)

    def test_check_mode_reports_and_changes_nothing(self):
        for value in ("", "Mars/Base"):
            with self.subTest(value=value):
                self.env.write_text(f"DISPLAY_TZ={value}\n")
                result = self.run_step(mode="check")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("FAIL", result.stdout)
                self.assertIn("FAILURES=1", result.stdout)
                self.assertEqual(self.zone_in_env(), value)


if __name__ == "__main__":
    unittest.main()

"""Checks for links directly inside the installer's data directory."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

INSTALL = Path(__file__).resolve().parents[1] / "install.sh"
HARNESS = r'''
source <(sed '$d' "$INSTALL")
MODE=$TEST_MODE
CONTAINER_UID=$(id -u)
cd "$WORK"
setup_data_dir
printf 'LATER SECTION\nFAILURES=%s\n' "$FAILURES"
'''


@unittest.skipIf(os.name == "nt" or shutil.which("bash") is None, "needs a POSIX shell")
class InstallerDataLinks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.data = self.work / "data"
        self.data.mkdir(mode=0o700)

    def run_setup(self, mode):
        env = dict(os.environ, INSTALL=str(INSTALL), WORK=str(self.work), TEST_MODE=mode)
        return subprocess.run(["bash", "-c", HARNESS], env=env, text=True,
                              capture_output=True, timeout=30)

    def test_check_reports_link_and_continues(self):
        (self.data / "2026-01").symlink_to("missing")
        result = self.run_setup("check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("FAIL  data/2026-01 is a symbolic link", result.stdout)
        self.assertIn("viewer won't read trips", result.stdout)
        self.assertIn("backups copy only the link", result.stdout)
        self.assertIn("LATER SECTION\nFAILURES=1", result.stdout)

    def test_install_warns_and_continues(self):
        (self.data / "2026-01").symlink_to("missing")
        result = self.run_setup("install")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("warn  data/2026-01 is a symbolic link", result.stdout)
        self.assertIn("LATER SECTION\nFAILURES=0", result.stdout)

    def test_no_links(self):
        (self.data / "2026-01").mkdir()
        (self.data / "2026-01" / "nested").symlink_to("missing")
        result = self.run_setup("check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("symbolic link", result.stdout)
        self.assertIn("FAILURES=0", result.stdout)

    def test_hostile_name_is_escaped(self):
        (self.data / "bad\n\x1b[31m").symlink_to("missing")
        result = self.run_setup("check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(r"bad\n\E[31m", result.stdout)
        self.assertNotIn("\x1b", result.stdout)
        self.assertIn("FAILURES=1", result.stdout)

    def test_data_itself_link_still_stops(self):
        self.data.rmdir()
        self.data.symlink_to("missing")
        result = self.run_setup("check")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("data/ must be a real directory, never a symlink.", result.stdout)
        self.assertNotIn("LATER SECTION", result.stdout)

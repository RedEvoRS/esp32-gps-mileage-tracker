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
SUDO_READY=${TEST_SUDO_READY:-no}
CONTAINER_UID=$(id -u)
cd "$WORK"
"${TEST_STEP:-setup_data_dir}"
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

    def run_setup(self, mode, **extra):
        env = dict(os.environ, INSTALL=str(INSTALL), WORK=str(self.work), TEST_MODE=mode, **extra)
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

    def test_link_scan_descends_into_linked_data_only(self):
        real_data = self.work / "real-data"
        self.data.rename(real_data)
        self.data.symlink_to(real_data, target_is_directory=True)
        outside = self.work / "outside"
        outside.mkdir()
        (outside / "not-scanned").symlink_to("missing")
        (real_data / "2026-01").symlink_to(outside, target_is_directory=True)
        result = self.run_setup("check", TEST_STEP="check_data_links")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("FAIL  data/2026-01 is a symbolic link", result.stdout)
        self.assertNotIn("not-scanned", result.stdout)
        self.assertIn("LATER SECTION\nFAILURES=1", result.stdout)
        # Normal setup must still reject a linked data root before scanning.
        result = self.run_setup("check")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("data/ must be a real directory, never a symlink.", result.stdout)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root can always list data/")
    def test_unreadable_data_uses_sudo_only_when_this_run_has_it(self):
        # A fake sudo stands in for root: it logs its arguments and prints the
        # NUL-separated listing that root would see.
        bin_dir = self.work / "bin"
        bin_dir.mkdir()
        log = self.work / "sudo.log"
        fake = bin_dir / "sudo"
        fake.write_text("#!/bin/sh\n"
                        f"printf '%s\\n' \"$*\" >> '{log}'\n"
                        "printf 'data/2026-01\\0'\n")
        fake.chmod(0o755)
        self.data.chmod(0)
        self.addCleanup(self.data.chmod, 0o700)
        path = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
        result = self.run_setup("check", PATH=path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("weren't checked", result.stdout)
        self.assertIn("FAILURES=0", result.stdout)
        self.assertFalse(log.exists(), "check mode must not start a sudo prompt")
        result = self.run_setup("check", PATH=path, TEST_SUDO_READY="yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("FAIL  data/2026-01 is a symbolic link", result.stdout)
        self.assertIn("FAILURES=1", result.stdout)
        self.assertIn("find data/ -mindepth 1 -maxdepth 1 -type l -print0", log.read_text())

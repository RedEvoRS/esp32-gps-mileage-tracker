import unittest
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from ci_paths import relevant


class CIPathsTests(unittest.TestCase):
    def test_unrelated_changes_skip_paid_extra_work(self):
        self.assertEqual(relevant(["README.md", "server/mileage-viewer/app.py"]), (False, False))

    def test_builder_paths(self):
        for path in ("server/firmware-builder/build.sh", "firmware/04_trip_logger/a.h",
                     "tools/stamp_firmware_version.py", ".github/workflows/release.yml",
                     "tools/ci-install/run.sh", "tools/compare_firmware.py"):
            with self.subTest(path=path):
                self.assertEqual(relevant([path]), (True, False))

    def test_install_changes_include_windows(self):
        self.assertEqual(relevant(["install/tests/flash.Tests.ps1"]), (True, True))

    def test_dispatch_builds_without_spending_windows_minutes(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "outputs"
            subprocess.run([sys.executable, str(Path(__file__).with_name("ci_paths.py"))],
                           env=dict(os.environ, EVENT_NAME="workflow_dispatch", GITHUB_OUTPUT=str(output)),
                           check=True)
            self.assertEqual(output.read_text(), "builder=true\nwindows=false\n")

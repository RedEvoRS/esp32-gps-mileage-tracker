"""Run the real closing summary without installing packages or services."""

import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
INSTALL = ROOT / "server/install.sh"
HARNESS = r'''
source <(sed '$d' "$INSTALL")
SCRIPT_DIR=$WORK
cd "$WORK"
BIND_ADDR=192.0.2.1
MODE=$TEST_MODE
summary
'''


@unittest.skipIf(os.name == "nt" or shutil.which("bash") is None, "needs a POSIX shell")
class InstallerSummaryTests(unittest.TestCase):
    def run_summary(self, release=False, mode="install"):
        with tempfile.TemporaryDirectory(prefix="installer summary ") as root:
            work = Path(root)
            token = "example-private-upload-token-" + "a" * 32
            (work / ".env").write_text("INGEST_AUTH_TOKEN=" + token + "\n")
            if release:
                (work / "firmware-src").mkdir()
            result = subprocess.run(
                ["bash", "-c", HARNESS], capture_output=True, text=True, timeout=10,
                env={**os.environ, "INSTALL": str(INSTALL), "WORK": root, "TEST_MODE": mode})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            # Check both output streams, for both routes and --check.
            self.assertNotIn(token, result.stdout + result.stderr)
            return result.stdout, work

    def test_release_summary_build_flash_drive_and_private_token(self):
        output, work = self.run_summary(release=True)
        self.assertIn("setup.sh offers to build", output)
        command = next(line.strip() for line in output.splitlines() if "--install-dir" in line)
        self.assertEqual(shlex.split(command), ["bash", str(work / "firmware-builder/build.sh"),
                                               "--install-dir", str(work)])
        self.assertIn("README.md#step-2--flash-the-tracker", output)
        self.assertIn("README.md#step-3--first-drive-and-device-health", output)
        for text in ("PC connected", "factory flash", "ERASE", "Device health snapshot",
                     "one-time cleanup flash", "INGEST_AUTH_TOKEN", str(work / ".env")):
            self.assertIn(text, output)
        self.assertNotIn("secrets.h", output)
        self.assertNotIn("steps 3 to 5", output)

    def test_source_summary_keeps_manual_guidance_and_private_token(self):
        output, work = self.run_summary()
        for text in ("firmware/secrets.h.example", "firmware/04_trip_logger/secrets.h",
                     "SERVER_HOST", "SERVER_PORT", "Set AUTH_TOKEN to INGEST_AUTH_TOKEN",
                     str(work / ".env"), "DEPLOY_DEBIAN.md#advanced-local-firmware-settings"):
            self.assertIn(text, output)
        self.assertNotIn("setup.sh offers", output)
        self.assertNotIn("steps 3 to 5", output)

    def test_check_summary_has_no_firmware_instructions_or_token(self):
        for release in (False, True):
            with self.subTest(release=release):
                output, _ = self.run_summary(release=release, mode="check")
                self.assertIn("Everything checks out", output)
                self.assertNotIn("Next:", output)

    def test_summary_document_references_exist(self):
        for release in (False, True):
            output, _ = self.run_summary(release=release)
            references = re.findall(r"(README\.md|DEPLOY_DEBIAN\.md)#([a-z0-9-]+)", output)
            self.assertTrue(references)
            for filename, anchor in references:
                document = (ROOT / filename).read_text(encoding="utf-8")
                headings = re.findall(r"^#+ (.+)$", document, re.M)
                anchors = [re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
                           for heading in headings]
                self.assertIn(anchor, anchors, filename)


if __name__ == "__main__":
    unittest.main()

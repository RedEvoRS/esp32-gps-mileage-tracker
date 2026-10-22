"""Flash safety tests: CI's existing tools unittest discovery runs these.

Bash tests need bash and jq (apt install jq on Debian, brew install jq on macOS).
Windows: powershell -ExecutionPolicy Bypass -File install/tests/flash.Tests.ps1
No test opens a serial port. Fixtures contain synthetic bytes, never credentials.
"""

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def fixture(folder):
    manifest = {
        "schema": 1, "chip": "esp32s3", "kind": "enroll",
        "version": "v0.08+0123456789", "commit": "0" * 40,
        "fqbn": "esp32:esp32:esp32s3:CDCOnBoot=cdc,USBMode=hwcdc,FlashSize=8M,PSRAM=enabled",
        "flash_mode": "dio", "flash_freq": "80m", "flash_size": "8MB",
        "device_id": "test-device", "app_offset": "0x10000", "parts": [],
    }
    for name, offset in (("bootloader", "0x0"), ("partitions", "0x8000"),
                         ("boot_app0", "0xe000"), ("app", "0x10000")):
        content = (name + " synthetic test bytes").encode("ascii")
        (folder / (name + ".bin")).write_bytes(content)
        manifest["parts"].append({"name": name, "file": name + ".bin",
                                  "offset": offset,
                                  "sha256": hashlib.sha256(content).hexdigest()})
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="ascii")
    return manifest


@unittest.skipUnless(os.name != "nt" and shutil.which("bash") and shutil.which("jq"),
                     "bash/jq required; Windows runs install/tests/flash.Tests.ps1 separately")
class BashFlashTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="flash-tests-")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.manifest = fixture(self.folder)
        source = (ROOT / "install/flash.sh").read_text(encoding="utf-8")
        self.helpers = source.split("# BEGIN PURE HELPERS", 1)[1].split("\n", 1)[1].split("# END PURE HELPERS", 1)[0]

    def run_bash(self, code, *args, success=True):
        result = subprocess.run(["bash", "-c", "set -euo pipefail\n" + self.helpers + code,
                                 "test", str(self.folder), *args], text=True, capture_output=True)
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
        return result.stdout.strip()

    def save(self, manifest):
        (self.folder / "manifest.json").write_text(json.dumps(manifest), encoding="ascii")

    def test_valid_manifest_and_selection(self):
        self.run_bash('flash_validate "$1" false')
        self.assertEqual(self.run_bash('flash_parts "$1" false | cut -f1'), "app")
        self.assertEqual(self.run_bash('flash_parts "$1" true | cut -f1').splitlines(),
                         ["bootloader", "partitions", "boot_app0", "app"])
        self.manifest["kind"] = "normal"
        self.save(self.manifest)
        self.run_bash('flash_validate "$1" true')
        self.assertEqual(self.run_bash('flash_parts "$1" false | cut -f1'), "app")

    def test_invalid_metadata_and_parts(self):
        variants = [dict(schema=2), dict(schema="1"), dict(chip="esp32"), dict(kind="other"),
                    dict(flash_size="16MB"), dict(flash_mode="bad"), dict(flash_freq="bad"),
                    dict(app_offset="0x20000"), dict(commit="bad"), dict(fqbn="other")]
        for change in variants:
            with self.subTest(change=change):
                self.save({**self.manifest, **change})
                self.run_bash('flash_validate "$1" false', success=False)
        for field, value in (("file", "../app.bin"), ("sha256", "0" * 64),
                             ("offset", "0x9000"), ("offset", "0x190000"),
                             ("offset", "0x10001"), ("offset", "0x1;id"),
                             ("name", "bootloader")):
            with self.subTest(field=field, value=value):
                altered = copy.deepcopy(self.manifest)
                altered["parts"][3][field] = value
                if field == "offset":
                    altered["app_offset"] = value
                self.save(altered)
                self.run_bash('flash_validate "$1" false', success=False)

    def test_cleanup_rejects_enrollment(self):
        self.run_bash('flash_validate "$1" true', success=False)

    def test_hash_missing_empty_and_symlink(self):
        app = self.folder / "app.bin"
        app.write_bytes(b"tampered")
        self.run_bash('flash_validate "$1" false', success=False)
        app.unlink()
        self.run_bash('flash_validate "$1" false', success=False)
        app.symlink_to(self.folder / "bootloader.bin")
        self.run_bash('flash_validate "$1" false', success=False)
        app.unlink()
        app.write_bytes(b"")
        self.manifest["parts"][3]["sha256"] = hashlib.sha256(b"").hexdigest()
        self.save(self.manifest)
        self.run_bash('flash_validate "$1" false', success=False)

    def test_reserved_region_and_oversized_app(self):
        app = self.folder / "app.bin"
        app.write_bytes(b"x" * (0x180001))
        self.manifest["parts"][3]["sha256"] = hashlib.sha256(app.read_bytes()).hexdigest()
        self.save(self.manifest)
        self.run_bash('flash_validate "$1" false', success=False)
        self.manifest = fixture(self.folder)
        self.manifest["parts"][2]["offset"] = "0x9000"
        self.save(self.manifest)
        self.run_bash('flash_validate "$1" false', success=False)

    def test_port_selection(self):
        self.assertEqual(self.run_bash('flash_choose_port /dev/test ""'), "/dev/test")
        self.assertEqual(self.run_bash('flash_choose_port "" "" /dev/one'), "/dev/one")
        self.assertEqual(self.run_bash('flash_choose_port "" 2 /dev/one /dev/two'), "/dev/two")
        for code in ('flash_choose_port "" ""', 'flash_choose_port "" 0 /dev/one /dev/two',
                     'flash_choose_port "" 3 /dev/one /dev/two',
                     'flash_choose_port "" invalid /dev/one /dev/two'):
            self.run_bash(code, success=False)

    def test_dry_run_and_private_copy_cleanup(self):
        fake = self.folder / "esptool"
        fake.write_text('#!/bin/sh\n[ "$1" = version ] || exit 90\necho "esptool v5.6.0"\n', encoding="ascii")
        fake.chmod(0o700)
        for first, cleanup, count, major in ((False, False, 1, 5), (True, False, 4, 5),
                                            (False, True, 1, 5), (False, True, 1, 4),
                                            (True, False, 4, 4)):
            fake.write_text(f'#!/bin/sh\n[ "$1" = version ] || exit 90\necho "esptool v{major}.6.0"\n', encoding="ascii")
            with self.subTest(first=first, cleanup=cleanup, major=major):
                self.manifest["kind"] = "normal" if cleanup else "enroll"
                self.save(self.manifest)
                args = ["bash", str(ROOT / "install/flash.sh"), "--path", str(self.folder),
                        "--port", "/dev/test", "--dry-run", "--esptool", str(fake)]
                if first:
                    args += ["--first-install", "--backup-dir", str(self.folder / "backups")]
                if cleanup:
                    args += ["--cleanup"]
                result = subprocess.run(args, capture_output=True, text=True,
                                        env={**os.environ, "TMPDIR": str(self.folder)})
                self.assertEqual(result.returncode, 0, result.stderr)
                command = next(line for line in result.stdout.splitlines() if ("write-flash" if major == 5 else "write_flash") in line)
                self.assertEqual(command.count(".bin"), count)
                self.assertNotIn("--erase-all", command)
                self.assertEqual(result.stdout.count("read-flash" if major == 5 else "read_flash"), 2 if first else 0)
                self.assertEqual(list(self.folder.glob("tracker-firmware.*")), [])
        (self.folder / "app.bin").write_bytes(b"bad")
        result = subprocess.run(args, capture_output=True, text=True,
                                env={**os.environ, "TMPDIR": str(self.folder)})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(self.folder.glob("tracker-firmware.*")), [])


if __name__ == "__main__":
    unittest.main()

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

    def test_server_allowlist_and_release_label(self):
        for target in ("alias", "alias-1.example", "_alias", "user@host", "user.name-1@127.0.0.1"):
            self.run_bash('flash_server_valid "$2"', target)
        for target in ("", "-alias", "-user@host", "user@-host", "user@@host", "@host", "user@",
                       " alias", "alias ", "alias\n", "alias\r", "alias\t", "user host", "host:22",
                       "[::1]", "alias/path", "alias\\path", "$(id)", "`id`", "alias;id", "alias&",
                       "alias|id", "alias*", "alias?", 'alias"', "alias'", "alias%value", "alias=value"):
            with self.subTest(target=target):
                self.run_bash('flash_server_valid "$2"', target, success=False)
        self.assertEqual(self.run_bash('flash_label "$2"', "@" + "RELEASE_TAG" + "@"),
                         "development checkout")
        # Match release packaging's global replacement, including helper source.
        self.helpers = self.helpers.replace("@" + "RELEASE_TAG" + "@", "v0.00-test")
        self.assertEqual(self.run_bash('flash_label v0.00-test'), "release v0.00-test")

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
                commands = [line for line in result.stdout.splitlines() if line.startswith("DRY RUN:") and "--chip" in line]
                self.assertIn("Tracker flash (development checkout).", result.stdout)
                reset = "no-reset" if first else "default-reset"
                if major == 4:
                    reset = reset.replace("-", "_")
                self.assertIn("--before " + reset, commands[0])
                self.assertIn("--before " + ("no-reset" if major == 5 else "no_reset"), command)
                self.assertIn("--after " + ("hard-reset" if major == 5 else "hard_reset"), command)
                erase = "erase-flash" if major == 5 else "erase_flash"
                self.assertEqual(sum(erase in line for line in commands), 1 if first else 0)
                if first:
                    self.assertIn(erase, commands[-2])
                    self.assertIn("read-flash" if major == 5 else "read_flash", commands[-3])
                    self.assertIn("LittleFS partition is fully erased; provisioning trip storage.", result.stdout)
                    self.assertIn("Setup complete. Logging trips.", result.stdout)
                self.assertEqual(list(self.folder.glob("tracker-firmware.*")), [])
        release_script = self.folder / "release-flash.sh"
        release_script.write_bytes((ROOT / "install/flash.sh").read_bytes().replace(
            b"@" + b"RELEASE_TAG" + b"@", b"v0.00-test"))
        release_args = [str(release_script) if arg == str(ROOT / "install/flash.sh") else arg for arg in args]
        result = subprocess.run(release_args, capture_output=True, text=True,
                                env={**os.environ, "TMPDIR": str(self.folder)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Tracker flash (release v0.00-test).", result.stdout)
        (self.folder / "app.bin").write_bytes(b"bad")
        result = subprocess.run(args, capture_output=True, text=True,
                                env={**os.environ, "TMPDIR": str(self.folder)})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(self.folder.glob("tracker-firmware.*")), [])

    def test_unraid_root_absolute_pool_path_with_fake_scp(self):
        fake = self.folder / "esptool"
        fake.write_text('#!/bin/sh\n[ "$1" = version ] || exit 90\necho "esptool v5.6.0"\n')
        fake.chmod(0o700)
        scp = self.folder / "scp"
        scp.write_text('''#!/bin/bash
set -eu
[[ $1 == -rq ]]
[[ $2 == root@host.invalid:/mnt/pool/appdata/mileage-tracker/firmware-out/current/ ]]
printf '%s\\n' "$2" > "$FLASH_TEST_SCP_LOG"
mkdir -p "$3"
cp "$FLASH_TEST_FIXTURE"/manifest.json "$FLASH_TEST_FIXTURE"/*.bin "$3/"
''')
        scp.chmod(0o700)
        log = self.folder / "scp-calls"
        result = subprocess.run(["bash", str(ROOT / "install/flash.sh"),
                                 "--server", "root@host.invalid", "--install-dir",
                                 "/mnt/pool/appdata/mileage-tracker", "--port", "/dev/test",
                                 "--dry-run", "--esptool", str(fake)],
                                text=True, capture_output=True, timeout=20,
                                env=dict(os.environ, PATH=str(self.folder) + os.pathsep + os.environ["PATH"],
                                         FLASH_TEST_FIXTURE=str(self.folder), FLASH_TEST_SCP_LOG=str(log),
                                         TMPDIR=str(self.folder)))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(log.read_text().strip(),
                         "root@host.invalid:/mnt/pool/appdata/mileage-tracker/firmware-out/current/")
        self.assertEqual(list(self.folder.glob("tracker-firmware.*")), [])

    def interactive(self, options, replies, failure="", major=5):
        # A private pseudo-terminal exercises the real /dev/tty prompts. The
        # only executable given to the script is a fake tool; no device opens.
        import fcntl
        import pty
        import termios

        fake = self.folder / "esptool"
        log = self.folder / "commands.txt"
        log.write_text("", encoding="ascii")
        fake.write_text('''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FLASH_TEST_LOG"], "a") as out:
    out.write(json.dumps(args) + "\\n")
if args == ["version"]:
    print("esptool v" + os.environ["FLASH_TEST_MAJOR"] + ".6.0")
elif "chip-id" in args or "chip_id" in args:
    failure = os.environ["FLASH_TEST_FAILURE"]
    if failure == "stderr":
        print("Synthetic diagnostic with a successful exit", file=sys.stderr)
    if failure == "chip" or (failure == "auto" and ("default-reset" in args or "default_reset" in args)):
        print("Synthetic connection failure on stderr", file=sys.stderr)
        sys.exit(1)
elif ("write-flash" in args or "write_flash" in args) and os.environ["FLASH_TEST_FAILURE"] == "write":
    sys.exit(1)
elif "read-flash" in args or "read_flash" in args:
    if os.environ["FLASH_TEST_FAILURE"] == "read":
        sys.exit(1)
    with open(args[-1], "wb") as out:
        size = 17 if os.environ["FLASH_TEST_FAILURE"] == "short" else 8388608
        out.truncate(size)
        if os.environ["FLASH_TEST_FAILURE"] == "mismatch" and ".verify.bin" in args[-1]:
            out.write(b"x")
elif ("erase-flash" in args or "erase_flash" in args) and os.environ["FLASH_TEST_FAILURE"] == "erase":
    sys.exit(1)
''', encoding="ascii")
        fake.chmod(0o700)
        master, slave = pty.openpty()

        def controlling_terminal():
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        try:
            process = subprocess.Popen(
                ["bash", str(ROOT / "install/flash.sh"), "--path", str(self.folder),
                 "--port", "/dev/test", "--esptool", str(fake),
                 "--backup-dir", str(self.folder / "backups"), *options],
                stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, preexec_fn=controlling_terminal,
                env={**os.environ, "TMPDIR": str(self.folder), "FLASH_TEST_LOG": str(log),
                     "FLASH_TEST_FAILURE": failure, "FLASH_TEST_MAJOR": str(major)})
            try:
                os.write(master, replies.encode("ascii"))
                stdout, stderr = process.communicate(timeout=25)
            except BaseException:
                process.kill()
                process.communicate()
                raise
        finally:
            os.close(slave)
            os.close(master)
        self.assertEqual(list(self.folder.glob("tracker-firmware.*")), [])
        commands = [json.loads(line) for line in log.read_text(encoding="ascii").splitlines()]
        return process.returncode, stdout + stderr, commands

    def test_first_install_erase_gate_and_order(self):
        for major in (4, 5):
            for options, replies, expected_reads in (
                    (["--first-install"], "\nERASE\n", 2),
                    ([], "yes\n\nERASE\n", 2),
                    (["--first-install", "--skip-backup"], "\nERASE\n", 0)):
                with self.subTest(major=major, options=options):
                    code, output, commands = self.interactive(options, replies, major=major)
                    self.assertEqual(code, 0, output)
                    suffix = "-flash" if major == 5 else "_flash"
                    self.assertIn("erase" + suffix, commands[-2])
                    self.assertIn("write" + suffix, commands[-1])
                    self.assertEqual(sum("read" + suffix in c for c in commands), expected_reads)
                    self.assertEqual(sum(arg.endswith(".bin") for arg in commands[-1]), 4)
                    self.assertFalse(any("default-reset" in c or "default_reset" in c for c in commands))

    def test_rejected_confirmation_and_backup_failures_block_erase_and_write(self):
        for skip in (False, True):
            for reply in ("yes", "erase", "ERASE ", ""):
                with self.subTest(skip=skip, reply=reply):
                    options = ["--first-install"] + (["--skip-backup"] if skip else [])
                    code, output, commands = self.interactive(options, "\n" + reply + "\n")
                    self.assertNotEqual(code, 0, output)
                    self.assertIn("confirmation refused", output)
                    self.assertFalse(any("erase-flash" in c or "write-flash" in c for c in commands))
        for failure in ("read", "short", "mismatch"):
            with self.subTest(failure=failure):
                code, output, commands = self.interactive(["--first-install"], "\nERASE\n", failure)
                self.assertNotEqual(code, 0, output)
                self.assertFalse(any("erase-flash" in c or "write-flash" in c for c in commands))
        code, output, commands = self.interactive(["--first-install"], "\nERASE\n", "erase")
        self.assertNotEqual(code, 0, output)
        self.assertFalse(any("write-flash" in c for c in commands))

    def test_update_cleanup_never_erase_and_skip_requires_first(self):
        self.manifest["kind"] = "normal"
        self.save(self.manifest)
        for options, replies in (([], "no\n"), (["--cleanup"], "")):
            code, output, commands = self.interactive(options, replies)
            self.assertEqual(code, 0, output)
            self.assertFalse(any("erase-flash" in c or "read-flash" in c for c in commands))
            self.assertEqual(sum(arg.endswith(".bin") for arg in commands[-1]), 1)
            self.assertIn("default-reset", commands[1])
            self.assertNotIn("Download mode: hold BOOT", output)
            self.assertIn("no-reset", commands[-1])
            self.assertIn("hard-reset", commands[-1])
        for options in (["--skip-backup"], ["--cleanup", "--first-install"]):
            code, _, commands = self.interactive(options, "\n")
            self.assertNotEqual(code, 0)
            self.assertEqual(commands, [])

    def test_automatic_reset_fallback_and_failures(self):
        self.manifest["kind"] = "normal"
        self.save(self.manifest)
        for major in (4, 5):
            auto, no_reset, hard_reset, chip, write = (
                ("default-reset", "no-reset", "hard-reset", "chip-id", "write-flash") if major == 5 else
                ("default_reset", "no_reset", "hard_reset", "chip_id", "write_flash"))
            for cleanup in (False, True):
                options, replies = (["--cleanup"], "") if cleanup else ([], "no\n")
                code, output, commands = self.interactive(options, replies, major=major)
                self.assertEqual(code, 0, output)
                self.assertEqual(len(commands), 3)
                self.assertIn(auto, commands[1])
                self.assertNotIn("Download mode: hold BOOT", output)
                for failure in ("auto", "chip"):
                    with self.subTest(major=major, cleanup=cleanup, failure=failure):
                        code, output, commands = self.interactive(
                            options, replies + "\n/dev/changed\n", failure, major)
                        self.assertEqual(code == 0, failure == "auto", output)
                        self.assertIn(auto, commands[1])
                        self.assertIn(chip, commands[2])
                        self.assertIn(no_reset, commands[2])
                        self.assertIn("/dev/changed", commands[2])
                        self.assertIn("Download mode: hold BOOT", output)
                        self.assertEqual(sum(write in c for c in commands), 1 if failure == "auto" else 0)
                        if failure == "auto":
                            self.assertIn("/dev/changed", commands[-1])
                            self.assertIn(no_reset, commands[-1])
                            self.assertIn(hard_reset, commands[-1])
                            self.assertEqual(sum(arg.endswith(".bin") for arg in commands[-1]), 1)
                code, output, commands = self.interactive(options, replies, "write", major)
                self.assertNotEqual(code, 0, output)
                self.assertEqual(len(commands), 3)
                code, output, commands = self.interactive(options, replies, "stderr", major)
                self.assertEqual(code, 0, output)
                self.assertEqual(len(commands), 3)


if __name__ == "__main__":
    unittest.main()

"""Build stamps are tested against disposable repositories, never this checkout."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import stamp_firmware_version as stamp


class FirmwareStamp(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="firmware-stamp-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Example Builder")
        self.git("config", "user.email", "builder@example.org")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.autocrlf", "false")
        self.git("config", "core.hooksPath", str(self.root / "no-hooks"))
        (self.root / ".gitignore").write_text(
            f"{stamp.HEADER.as_posix()}\nsecrets.h\nbuild/\n", encoding="ascii")
        (self.root / "tracked.txt").write_text("original\n", encoding="ascii")
        (self.root / stamp.VERSION).parent.mkdir(parents=True)
        (self.root / stamp.VERSION).write_text("v0.05\n", encoding="ascii")
        self.git("add", ".")
        self.git("commit", "-qm", "Synthetic initial fixture")
        self.commit = self.git("rev-parse", "--short=10", "HEAD")
        self.version = "v0.05+" + self.commit

    def git(self, *args):
        return subprocess.check_output(
            ["git", *args], cwd=self.root, text=True,
            env={**os.environ, "TZ": "UTC"}, stderr=subprocess.DEVNULL).strip()

    def test_clean_stamp_and_generated_header(self):
        self.assertEqual(stamp.stamp(self.root), self.version)
        header = (self.root / stamp.HEADER).read_text(encoding="ascii")
        self.assertIn(f'#define FIRMWARE_VERSION_BASE "{self.version}"', header)
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_unstaged_tracked_change(self):
        (self.root / "tracked.txt").write_text("edited\n", encoding="ascii")
        self.assertEqual(stamp.stamp(self.root), self.version + "-dirty")

    def test_staged_change(self):
        (self.root / "tracked.txt").write_text("staged\n", encoding="ascii")
        self.git("add", "tracked.txt")
        self.assertEqual(stamp.git_version(self.root), self.version + "-dirty")

    def test_tracked_deletion_and_staged_rename(self):
        (self.root / "tracked.txt").unlink()
        self.assertEqual(stamp.git_version(self.root), self.version + "-dirty")
        self.git("restore", "tracked.txt")
        self.git("mv", "tracked.txt", "renamed.txt")
        self.assertEqual(stamp.git_version(self.root), self.version + "-dirty")

    def test_staged_new_file_is_dirty(self):
        (self.root / "new.txt").write_text("new\n", encoding="ascii")
        self.git("add", "new.txt")
        self.assertEqual(stamp.git_version(self.root), self.version + "-dirty")

    def test_untracked_and_ignored_files_do_not_mark_dirty(self):
        for name in ("untracked.txt", "secrets.h", "build/output.txt"):
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("synthetic\n", encoding="ascii")
        self.assertEqual(stamp.stamp(self.root), self.version)

    def test_unchanged_header_is_not_rewritten(self):
        stamp.stamp(self.root)
        target = self.root / stamp.HEADER
        os.utime(target, ns=(1_000_000_000, 1_000_000_000))
        before = target.stat().st_mtime_ns
        stamp.stamp(self.root)
        self.assertEqual(target.stat().st_mtime_ns, before)

    def test_change_then_restore_updates_stamp(self):
        stamp.stamp(self.root)
        (self.root / "tracked.txt").write_text("edited\n", encoding="ascii")
        self.assertEqual(stamp.stamp(self.root), self.version + "-dirty")
        self.git("restore", "tracked.txt")
        self.assertEqual(stamp.stamp(self.root), self.version)

    def test_shallow_detached_checkout(self):
        clone = self.root / "shallow"
        self.git("clone", "-q", "--depth=1", self.root.as_uri(), str(clone))
        subprocess.run(["git", "checkout", "--detach", "-q"], cwd=clone, check=True)
        self.assertEqual(stamp.stamp(clone), self.version)

    def test_version_tokens_and_suffix_budget(self):
        for base in ("unversioned", self.version, self.version + "-dirty",
                     "v99.99+0123456789-dirty", "A._+-9", "a" * 28):
            with self.subTest(base=base):
                stamp.validate_version(base)
                self.assertRegex(base + "+dbg", r"\A[A-Za-z0-9][A-Za-z0-9._+-]{0,31}\Z")
        for base in ("", "-bad", "a b", "a\n", "é", "a" * 29, "a" * 33):
            with self.subTest(base=base), self.assertRaises(ValueError):
                stamp.validate_version(base)

    def test_valid_release_numbers_and_surrounding_whitespace(self):
        for release in ("v0.00", "v0.05", "v0.99", "v1.00", "v9.99", "v10.00", "v99.99"):
            for whitespace in ("", "\n", "\r\n", " \t\r\n"):
                with self.subTest(release=release, whitespace=repr(whitespace)):
                    (self.root / stamp.VERSION).write_bytes(
                        (whitespace + release + whitespace).encode("ascii"))
                    self.assertEqual(stamp.stamp(self.root),
                                     release + "+" + self.commit + "-dirty")

    def test_modified_version_file_is_dirty(self):
        (self.root / stamp.VERSION).write_text("v0.06\n", encoding="ascii")
        expected = self.version.replace("v0.05", "v0.06") + "-dirty"
        self.assertEqual(stamp.stamp(self.root), expected)
        self.git("add", stamp.VERSION.as_posix())
        self.assertEqual(stamp.stamp(self.root), expected)

    def test_malformed_release_does_not_write_header(self):
        for value in ("0.05", "v0.5", "v.05", "v00.05", "v01.05", "v100.05",
                      "v-1.05", "v1.000", "V0.05", "v0.05+extra", "v0. 05",
                      "v0.05\nv0.06", "", " \n", "v０.05"):
            with self.subTest(value=value):
                (self.root / stamp.VERSION).write_text(value, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "firmware/VERSION"):
                    stamp.stamp(self.root)
                self.assertFalse((self.root / stamp.HEADER).exists())

    def test_missing_release_does_not_write_header(self):
        (self.root / stamp.VERSION).unlink()
        with self.assertRaisesRegex(ValueError, "cannot read firmware/VERSION"):
            stamp.stamp(self.root)
        self.assertFalse((self.root / stamp.HEADER).exists())

    def test_bad_release_preserves_existing_header(self):
        stamp.stamp(self.root)
        target = self.root / stamp.HEADER
        original = target.read_bytes()
        for value in ("invalid", None):
            with self.subTest(value=value):
                if value is None:
                    (self.root / stamp.VERSION).unlink()
                else:
                    (self.root / stamp.VERSION).write_text(value, encoding="ascii")
                with self.assertRaises(ValueError):
                    stamp.stamp(self.root)
                self.assertEqual(target.read_bytes(), original)

    def test_git_failure_does_not_write_fallback(self):
        with patch.object(stamp, "git_version", side_effect=subprocess.CalledProcessError(1, "git")):
            with self.assertRaises(subprocess.CalledProcessError):
                stamp.stamp(self.root)
        self.assertFalse((self.root / stamp.HEADER).exists())


if __name__ == "__main__":
    unittest.main()

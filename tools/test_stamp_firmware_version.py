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
        self.git("add", ".")
        self.git("commit", "-qm", "Synthetic initial fixture")
        self.commit = self.git("rev-parse", "--short=10", "HEAD")

    def git(self, *args):
        return subprocess.check_output(
            ["git", *args], cwd=self.root, text=True,
            env={**os.environ, "TZ": "UTC"}, stderr=subprocess.DEVNULL).strip()

    def test_clean_stamp_and_generated_header(self):
        self.assertEqual(stamp.stamp(self.root), self.commit)
        header = (self.root / stamp.HEADER).read_text(encoding="ascii")
        self.assertIn(f'#define FIRMWARE_VERSION_BASE "{self.commit}"', header)
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_unstaged_tracked_change(self):
        (self.root / "tracked.txt").write_text("edited\n", encoding="ascii")
        self.assertEqual(stamp.stamp(self.root), self.commit + "-dirty")

    def test_staged_change(self):
        (self.root / "tracked.txt").write_text("staged\n", encoding="ascii")
        self.git("add", "tracked.txt")
        self.assertEqual(stamp.git_version(self.root), self.commit + "-dirty")

    def test_tracked_deletion_and_staged_rename(self):
        (self.root / "tracked.txt").unlink()
        self.assertEqual(stamp.git_version(self.root), self.commit + "-dirty")
        self.git("restore", "tracked.txt")
        self.git("mv", "tracked.txt", "renamed.txt")
        self.assertEqual(stamp.git_version(self.root), self.commit + "-dirty")

    def test_staged_new_file_is_dirty(self):
        (self.root / "new.txt").write_text("new\n", encoding="ascii")
        self.git("add", "new.txt")
        self.assertEqual(stamp.git_version(self.root), self.commit + "-dirty")

    def test_untracked_and_ignored_files_do_not_mark_dirty(self):
        for name in ("untracked.txt", "secrets.h", "build/output.txt"):
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("synthetic\n", encoding="ascii")
        self.assertEqual(stamp.stamp(self.root), self.commit)

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
        self.assertEqual(stamp.stamp(self.root), self.commit + "-dirty")
        self.git("restore", "tracked.txt")
        self.assertEqual(stamp.stamp(self.root), self.commit)

    def test_shallow_detached_checkout(self):
        clone = self.root / "shallow"
        self.git("clone", "-q", "--depth=1", self.root.as_uri(), str(clone))
        subprocess.run(["git", "checkout", "--detach", "-q"], cwd=clone, check=True)
        self.assertEqual(stamp.stamp(clone), self.commit)

    def test_version_tokens_and_suffix_budget(self):
        for base in ("unversioned", self.commit, self.commit + "-dirty", "A._+-9", "a" * 28):
            with self.subTest(base=base):
                stamp.validate_version(base)
                self.assertRegex(base + "+dbg", r"\A[A-Za-z0-9][A-Za-z0-9._+-]{0,31}\Z")
        for base in ("", "-bad", "a b", "a\n", "é", "a" * 29, "a" * 33):
            with self.subTest(base=base), self.assertRaises(ValueError):
                stamp.validate_version(base)

    def test_git_failure_does_not_write_fallback(self):
        with patch.object(stamp, "git_version", side_effect=subprocess.CalledProcessError(1, "git")):
            with self.assertRaises(subprocess.CalledProcessError):
                stamp.stamp(self.root)
        self.assertFalse((self.root / stamp.HEADER).exists())


if __name__ == "__main__":
    unittest.main()

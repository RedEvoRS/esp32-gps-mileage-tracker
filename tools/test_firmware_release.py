"""Release validation uses disposable repositories and synthetic notes only."""

from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import firmware_release as release


class FirmwareRelease(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="firmware-release-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Example Builder")
        self.git("config", "user.email", "builder@example.org")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.autocrlf", "false")
        self.git("config", "core.hooksPath", str(self.root / "no-hooks"))
        self.write(release.VERSION_PATH, "v1.05\n")
        self.install = "**Install**\n- Synthetic installation instructions.\n"
        self.write(release.INSTALL_PATH, self.install)
        self.commit()
        self.base = self.git("rev-parse", "HEAD")
        self.write(release.VERSION_PATH, "v1.06\n")
        self.notes = "release-notes/fw-v1.06.md"
        self.write(self.notes, "Synthetic firmware improvements.\n")

    def git(self, *args):
        return subprocess.check_output(
            ["git", *args], cwd=self.root, stderr=subprocess.DEVNULL,
            env={**os.environ, "TZ": "UTC"}).decode().strip()

    def write(self, path, content):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode("utf-8"))

    def commit(self):
        self.git("add", ".")
        self.git("commit", "-qm", "Synthetic release fixture")

    def test_increase_with_notes(self):
        self.assertEqual(release.validate(self.root, self.base), ("v1.06", True))

    def test_unchanged_version_needs_no_notes(self):
        self.write(release.VERSION_PATH, "v1.05\n")
        (self.root / release.INSTALL_PATH).unlink()
        self.assertEqual(release.validate(self.root, self.base), ("v1.05", False))

    def test_missing_and_blank_notes_fail(self):
        (self.root / self.notes).unlink()
        with self.assertRaisesRegex(ValueError, self.notes):
            release.validate(self.root, self.base)
        for content in ("", " \t\r\n"):
            with self.subTest(content=content):
                self.write(self.notes, content)
                with self.assertRaisesRegex(ValueError, "non-empty"):
                    release.validate(self.root, self.base)

    def test_notes_contain_only_the_summary(self):
        for content in ("First.\n\nSecond.", "First.\r\n\r\nSecond.", "Summary.\n**Install**"):
            with self.subTest(content=content):
                self.write(self.notes, content)
                with self.assertRaisesRegex(ValueError, "only one"):
                    release.validate(self.root, self.base)

    def test_invalid_versions_use_stamp_rule(self):
        for value in ("", "v1.6", "v01.06", "v100.00", "v1.060", "v1.06+abc", "v１.06", "v1.06\nv1.07"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                release.version(value.encode("utf-8"))

    def test_valid_versions_and_surrounding_whitespace(self):
        for value in ("v0.00", "v1.06", "v9.99", "v10.00", "v99.99"):
            for whitespace in ("", "\n", "\r\n", " \t\r\n"):
                with self.subTest(value=value, whitespace=repr(whitespace)):
                    self.assertEqual(release.version((whitespace + value + whitespace).encode("ascii")), value)

    def test_equal_or_decreased_version_fails(self):
        for value in ("v1.05\r\n", " v1.05\n", "v1.04\n", "v0.99\n"):
            with self.subTest(value=value):
                self.write(release.VERSION_PATH, value)
                with self.assertRaisesRegex(ValueError, "must increase"):
                    release.validate(self.root, self.base)

    def test_major_rollover_is_numeric(self):
        for previous, proposed in (("v1.99", "v2.00"), ("v9.99", "v10.00")):
            with self.subTest(previous=previous, proposed=proposed):
                self.write(release.VERSION_PATH, previous + "\n")
                self.commit()
                base = self.git("rev-parse", "HEAD")
                self.write(release.VERSION_PATH, proposed + "\n")
                self.write(f"release-notes/fw-{proposed}.md", "Synthetic summary.\n")
                self.assertEqual(release.validate(self.root, base), (proposed, True))

    def test_missing_version_or_base_fails_closed(self):
        with self.assertRaises(ValueError):
            release.validate(self.root, "refs/heads/missing-base")
        (self.root / release.VERSION_PATH).unlink()
        with self.assertRaisesRegex(ValueError, "cannot read firmware/VERSION"):
            release.validate(self.root, self.base)

    def test_template_missing_or_invalid_fails(self):
        (self.root / release.INSTALL_PATH).unlink()
        with self.assertRaisesRegex(ValueError, "cannot read release-notes/install.md"):
            release.validate(self.root, self.base)
        for content in ("", "Wrong heading.\n"):
            self.write(release.INSTALL_PATH, content)
            with self.assertRaisesRegex(ValueError, "must start"):
                release.validate(self.root, self.base)

    def test_notes_must_be_utf8(self):
        (self.root / self.notes).write_bytes(b"\xff")
        with self.assertRaisesRegex(ValueError, "UTF-8"):
            release.validate(self.root, self.base)

    def test_body_preserves_install_section_exactly(self):
        self.assertEqual(release.body(self.root, "v1.06"),
                         "Synthetic firmware improvements.\n\n" + self.install)

    def test_windows_checkout_renders_the_same_install_section(self):
        self.write(release.INSTALL_PATH, self.install.replace("\n", "\r\n"))
        self.assertEqual(release.body(self.root, "v1.06"),
                         "Synthetic firmware improvements.\n\n" + self.install)

    def test_ref_reads_committed_version_notes_and_template(self):
        self.commit()
        head = self.git("rev-parse", "HEAD")
        self.write(release.VERSION_PATH, "v9.99\n")
        (self.root / self.notes).unlink()
        (self.root / release.INSTALL_PATH).unlink()
        self.assertEqual(release.validate(self.root, self.base, head), ("v1.06", True))
        self.assertEqual(release.body(self.root, "v1.06", head),
                         "Synthetic firmware improvements.\n\n" + self.install)

    def test_ref_does_not_accept_uncommitted_notes(self):
        (self.root / self.notes).unlink()
        self.commit()
        head = self.git("rev-parse", "HEAD")
        self.write(self.notes, "Only in the working tree.\n")
        with self.assertRaisesRegex(ValueError, "cannot read release-notes/fw-v1.06.md"):
            release.validate(self.root, self.base, head)

    def test_cli_check_and_render_outputs(self):
        self.commit()
        head = self.git("rev-parse", "HEAD")
        args = ["--root", str(self.root), "--base", self.base, "--ref", head]
        with redirect_stdout(io.StringIO()) as stdout:
            release.main(["check", *args])
        self.assertIn("v1.06 (increase with reviewed notes)", stdout.getvalue())
        output = self.root / "outputs"
        body = self.root / "body.md"
        release.main(["render", *args, "--body-file", str(body), "--github-output", str(output)])
        self.assertEqual(body.read_bytes(), ("Synthetic firmware improvements.\n\n" + self.install).encode())
        self.assertEqual(output.read_bytes(), b"version=v1.06\ntag=fw-v1.06\n")
        with redirect_stdout(io.StringIO()) as stdout:
            release.main(["render", *args])
        self.assertEqual(stdout.getvalue(), body.read_text())

    def test_cli_missing_notes_fails_without_outputs(self):
        (self.root / self.notes).unlink()
        output = self.root / "outputs"
        body = self.root / "body.md"
        with self.assertRaisesRegex(SystemExit, "cannot read release-notes/fw-v1.06.md"):
            release.main(["render", "--root", str(self.root), "--base", self.base,
                          "--body-file", str(body), "--github-output", str(output)])
        self.assertFalse(output.exists())
        self.assertFalse(body.exists())

    def test_cli_will_not_publish_an_unchanged_version(self):
        self.write(release.VERSION_PATH, "v1.05\n")
        with self.assertRaisesRegex(SystemExit, "no release to publish"):
            release.main(["render", "--root", str(self.root), "--base", self.base])


if __name__ == "__main__":
    unittest.main()

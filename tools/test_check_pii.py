"""Tests for check_pii.py.

Every leaky string here is assembled at runtime, so this file doesn't trip
the scanner it tests (CI runs the scanner over the whole repo).
"""

import importlib.util
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "check_pii", Path(__file__).with_name("check_pii.py"))
check_pii = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_pii)

USERS = "Us" + "ers"
HOME = "ho" + "me"


def hits(text, deny=()):
    return list(check_pii.scan_text("f", text, [check_pii.term_pattern(t) for t in deny]))


class HomePaths(unittest.TestCase):
    def test_real_account_names_are_flagged(self):
        for text in [
            'cd "C:\\' + USERS + '\\alice\\Documents"',
            '{"cwd": "C:\\\\' + USERS + '\\\\alice"}',
            "C:/" + USERS + "/alice/project",
            "/c/" + USERS + "/alice",
            "/mnt/c/" + USERS + "/alice",
            "#RSYNC_SSH=\"ssh -i /" + HOME + "/alice/.ssh/key\"",
            "scp x box:/" + HOME + "/alice/",
            "/" + USERS + "/alice/Library",
        ]:
            with self.subTest(text=text):
                self.assertTrue(hits(text))

    def test_placeholders_and_relative_paths_pass(self):
        for text in [
            'cd "~\\Documents\\Claude\\Projects\\GPS Milage Tracker"',
            "C:\\Users\\<user>\\Documents",
            "ExecStart=/bin/bash /home/%i/mileage-tracker/backup/backup.sh",
            'RSYNC_SSH="ssh -i /home/<user>/.ssh/mileage_backup"',
            'tinygps="${TINYGPS_DIR:-$HOME/Documents/Arduino/libraries}"',
            "/home/runner/work/repo",
            "https://example.org/users/42 and docs/users/guide.md",
        ]:
            with self.subTest(text=text):
                self.assertEqual(hits(text), [])


class Emails(unittest.TestCase):
    def test_personal_address_is_flagged(self):
        self.assertTrue(hits("contact: alice" + "@" + "gmail.com"))

    def test_noreply_example_and_lan_addresses_pass(self):
        for text in [
            "Co-Authored-By: Claude <noreply@anthropic.com>",
            "123+someone@users.noreply.github.com",
            "REMOTE=backup@nas.lan:/volume1/backups",
            "someone@example.com",
            "uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4",
            "mileage-backup@<user>.service and mileage-backup@.timer",
        ]:
            with self.subTest(text=text):
                self.assertEqual(hits(text), [])

    def test_commit_email_allowlist(self):
        self.assertTrue(check_pii.email_allowed("noreply@github.com"))
        self.assertTrue(check_pii.email_allowed("123+someone@users.noreply.github.com"))
        self.assertFalse(check_pii.email_allowed("alice" + "@" + "gmail.com"))


class Macs(unittest.TestCase):
    def test_real_bssid_is_flagged(self):
        self.assertTrue(hits("BSSID " + ":".join(["a4"] * 6)))
        array = "{ " + ", ".join(["0x1a"] * 6) + " }"
        self.assertTrue(hits("static const uint8_t WIFI_BSSID[6] = " + array + ";"))

    def test_zero_placeholder_and_unrelated_byte_arrays_pass(self):
        self.assertEqual(hits("static const uint8_t WIFI_BSSID[6] = "
                              "{ 0x00, 0x00, 0x00, 0x00, 0x00, 0x00 };"), [])
        self.assertEqual(hits("00:00:00:00:00:00"), [])
        self.assertEqual(hits("const uint8_t ubx[] = { 0xB5, 0x62, 0x06, 0x01, 0x08, 0x00 };"), [])


class Denylist(unittest.TestCase):
    def test_whole_word_case_insensitive(self):
        self.assertTrue(hits("shipped without it, per Alice.", deny=["alice"]))
        self.assertTrue(hits("Alice's call", deny=["alice"]))
        self.assertEqual(hits("malice aforethought", deny=["alice"]), [])

    def test_no_denylist_means_no_term_check(self):
        self.assertEqual(hits("per Alice."), [])


class Paths(unittest.TestCase):
    def problems(self, path):
        return list(check_pii.check_path(path))

    def test_files_that_belong_on_one_machine(self):
        for path in ["firmware/secrets.h", "server/.env", "server/viewer.env",
                     "server/backup/backup.conf", ".pii-denylist",
                     ".claude/settings.local.json", "annotations.db",
                     "server/data/2026-10/trip.json", "keys/id_ed25519"]:
            with self.subTest(path=path):
                self.assertTrue(self.problems(path))

    def test_only_known_synthetic_gpx(self):
        self.assertEqual(self.problems("server/sample.gpx"), [])
        self.assertTrue(self.problems("server/tests/parked.gpx"))

    def test_examples_pass(self):
        for path in ["server/.env.example", "server/viewer.env.example",
                     "firmware/secrets.h.example", ".claude/settings.json"]:
            with self.subTest(path=path):
                self.assertEqual(self.problems(path), [])


class Images(unittest.TestCase):
    def test_exif_is_flagged(self):
        jpeg = b"\xff\xd8\xff\xe1\x00\x16Exif\x00\x00II*\x00"
        self.assertTrue(list(check_pii.check_blob("docs/enclosure.jpg", jpeg, [])))

    def test_stripped_image_passes(self):
        png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        self.assertEqual(list(check_pii.check_blob("docs/enclosure.png", png, [])), [])


if __name__ == "__main__":
    unittest.main()

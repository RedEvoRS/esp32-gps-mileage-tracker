"""Tests for check_pii.py.

Every leaky string here is assembled at runtime, so this file doesn't trip
the scanner it tests (CI runs the scanner over the whole repo).
"""

import codecs
import importlib.util
import os
import subprocess
import tempfile
import unicodedata
import unittest
from pathlib import Path
from unittest.mock import patch

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
            "Signed-off-by: dependabot[bot] <support@github.com>",
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
        # Only GitHub's support address, not any github.com one.
        self.assertFalse(check_pii.email_allowed("alice" + "@" + "github.com"))


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


DECIMAL = "0.12345"  # Invented point near the equator in the Atlantic.
NMEA_LAT = "0007.4070"
NMEA_LON = "00007.4070"


def nmea(kind, lat=NMEA_LAT, lon=NMEA_LON, talker="GP", status="A"):
    fields = {
        "RMC": ["120000", status, lat, "N", lon, "E", "0", "0", "010100"],
        "GGA": ["120000", lat, "N", lon, "E", "1", "08", "0.9"],
        "GLL": [lat, "N", lon, "E", "120000", status],
    }
    return "$" + talker + kind + "," + ",".join(fields[kind]) + "*00"


class Coordinates(unittest.TestCase):
    def test_nmea_positions_from_any_talker_are_flagged(self):
        for kind in ("RMC", "GGA", "GLL"):
            for talker in ("GP", "GN", "GL", "ZZ"):
                with self.subTest(kind=kind, talker=talker):
                    self.assertEqual(hits("serial: " + nmea(kind, talker=talker)),
                                     ["f:1: NMEA position sentence"])

    def test_nmea_empty_or_missing_position_fields_pass(self):
        for kind in ("RMC", "GGA", "GLL"):
            for lat, lon in (("", ""), ("", "00007.4070"), ("0007.4070", "")):
                with self.subTest(kind=kind, empty_lat=not lat, empty_lon=not lon):
                    self.assertEqual(hits(nmea(kind, lat=lat, lon=lon, status="V")), [])
            self.assertEqual(hits("$" + "GP" + kind + ","), [])

    def test_nmea_nonempty_positions_are_checked_even_without_a_valid_fix(self):
        self.assertTrue(hits(nmea("RMC", status="V")))
        self.assertTrue(hits(nmea("GLL", status="V")))

    def test_nmea_multiple_sentences_and_other_sentence_types(self):
        self.assertTrue(hits(nmea("RMC", lat="", lon="") + " " + nmea("GGA")))
        self.assertEqual(hits("$" + "GPGSV,2,1,08,01,40,100,35*00"), [])
        self.assertEqual(hits("$" + "GPGSA,A,3,,,,,,,,,,,,,1.5,0.9,1.2*00"), [])

    def test_gpx_numeric_attribute_pairs_are_flagged(self):
        whole = "0"
        for attrs in (f'lat="{DECIMAL}" lon="{DECIMAL}"',
                      f"lon='{DECIMAL}' other='x' lat='{DECIMAL}'",
                      f'LAT = "{DECIMAL}" LON = "{DECIMAL}"',
                      f'lat="{whole}" lon="{whole}"'):
            with self.subTest(style=attrs.split("=", 1)[0]):
                self.assertEqual(hits("<trkpt " + attrs + "/>"),
                                 ["f:1: GPX latitude/longitude attributes"])

    def test_escaped_gpx_attributes_in_source_are_flagged(self):
        attrs = f'lat="{DECIMAL}" lon="{DECIMAL}"'.replace('"', '\\"')
        self.assertEqual(hits(attrs), ["f:1: GPX latitude/longitude attributes"])

    def test_gpx_single_empty_and_format_attributes_pass(self):
        for attrs in (f'lat="{DECIMAL}"', f'lon="{DECIMAL}"',
                      'lat="" lon=""', 'lat="%f" lon="%f"',
                      'lat="{lat}" lon="{lon}"'):
            self.assertEqual(hits("<trkpt " + attrs + "/>"), [])

    def test_decimal_pairs_and_labels_are_flagged(self):
        for text in (DECIMAL + ", -" + DECIMAL,
                     DECIMAL + " " + DECIMAL,
                     DECIMAL + "; " + DECIMAL,
                     "(+" + DECIMAL + ", " + DECIMAL + ")",
                     "lat " + DECIMAL + " lon -" + DECIMAL,
                     "latitude=" + DECIMAL + "; longitude=" + DECIMAL,
                     "LONGITUDE: " + DECIMAL + " LATITUDE: " + DECIMAL,
                     DECIMAL[1:] + ", " + DECIMAL[1:]):
            self.assertEqual(hits(text), ["f:1: decimal coordinate pair"])

    def test_decimal_ranges_include_edges_and_reject_outside(self):
        edge_lat, edge_lon = "90.0000", "180.0000"
        for sign in ("", "-"):
            self.assertTrue(hits(sign + edge_lat + ", " + sign + edge_lon))
        for lat, lon in (("90.0001", DECIMAL), (DECIMAL, "180.0001"),
                         ("-90.0001", DECIMAL), (DECIMAL, "-180.0001"),
                         ("90.00000000000000001", DECIMAL),
                         (DECIMAL, "180.00000000000000001")):
            self.assertEqual(hits(lat + ", " + lon), [])

    def test_unrelated_numbers_and_coarse_pairs_pass(self):
        for text in ("HDOP 0.9000", "3.3 V, 5.0 V", "0, -1", "0.123, 0.456",
                     DECIMAL + ", 0.123", "0.123, " + DECIMAL,
                     "16.777216 MiB", "16.777216 x 32.123456",
                     "version 1.2345.6789, 2.3456.7890",
                     "version v" + DECIMAL + ", " + DECIMAL):
            self.assertEqual(hits(text), [])

    def test_findings_contain_only_location_and_rule(self):
        self.assertEqual(hits("heading\n" + DECIMAL + ", " + DECIMAL),
                         ["f:2: decimal coordinate pair"])
        self.assertEqual(hits("heading\n" + nmea("GLL")),
                         ["f:2: NMEA position sentence"])

    def test_fixture_exemptions_apply_only_to_gpx_attributes(self):
        gpx = f'<trkpt lat="{DECIMAL}" lon="{DECIMAL}"/>'.encode()
        for path in check_pii.SYNTHETIC_GPX_CONTENT:
            with self.subTest(path=path):
                self.assertEqual(list(check_pii.check_blob(path, gpx, [])), [])
                for text, rule in ((nmea("GGA"), "NMEA position sentence"),
                                   (DECIMAL + ", " + DECIMAL, "decimal coordinate pair"),
                                   ("fictionalterm", "personal term from the denylist")):
                    findings = list(check_pii.check_blob(
                        path, text.encode(), [check_pii.term_pattern("fictionalterm")]))
                    self.assertEqual(findings, [f"{path}:1: {rule}"])

    def test_new_gpx_and_other_source_files_are_not_exempt(self):
        gpx = f'<trkpt lat="{DECIMAL}" lon="{DECIMAL}"/>'.encode()
        self.assertEqual(list(check_pii.check_blob("docs/log.txt", gpx, [])),
                         ["docs/log.txt:1: GPX latitude/longitude attributes"])
        findings = list(check_pii.check_blob("server/tests/new.gpx", gpx, []))
        self.assertEqual(len(findings), 2)
        self.assertIn("GPX that isn't a known synthetic fixture", findings[0])


class CommitMetadata(unittest.TestCase):
    def setUp(self):
        # Keep temporary repos inside the checkout, including on Windows.
        temp = tempfile.TemporaryDirectory(dir=check_pii.ROOT)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        root_patch = patch.object(check_pii, "ROOT", self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        self.git("init", "-q")
        (self.root / "readme.txt").write_text("Synthetic scanner fixture\n", encoding="utf-8")
        self.git("add", "readme.txt")
        self.tree = self.git("write-tree").decode().strip()

    def git(self, *args, data=None, env=None):
        return subprocess.run(["git", *args], cwd=self.root, input=data,
                              capture_output=True, check=True, env=env).stdout

    def commit(self, author_offset="+0000", committer_offset="+0000", message="Synthetic test"):
        env = dict(os.environ, GIT_AUTHOR_NAME="Scanner Test", GIT_COMMITTER_NAME="Scanner Test",
                   GIT_AUTHOR_EMAIL="scanner@users.noreply.github.com",
                   GIT_COMMITTER_EMAIL="scanner@users.noreply.github.com",
                   GIT_AUTHOR_DATE="946684800 " + author_offset,
                   GIT_COMMITTER_DATE="946684800 " + committer_offset)
        sha = self.git("commit-tree", self.tree, data=message.encode(), env=env).decode().strip()
        self.git("update-ref", "HEAD", sha)
        return sha

    def test_utc_author_and_committer_pass(self):
        sha = self.commit()
        self.assertEqual(list(check_pii.scan_commits(sha, [])), [])

    def test_non_utc_dates_report_each_role_without_the_offset(self):
        for role in ("author", "committer"):
            for offset in ("+0123", "-0042"):
                with self.subTest(role=role, sign=offset[0]):
                    sha = self.commit(**{role + "_offset": offset})
                    self.assertEqual(list(check_pii.scan_commits(sha, [])),
                                     [f"commit {sha[:12]}: {role} date isn't UTC (commit with TZ=UTC)"])

    def test_both_non_utc_dates_are_reported(self):
        sha = self.commit(author_offset="+0123", committer_offset="-0042")
        self.assertEqual(list(check_pii.scan_commits(sha, [])),
                         [f"commit {sha[:12]}: {role} date isn't UTC (commit with TZ=UTC)"
                          for role in ("author", "committer")])

    def test_tree_and_history_do_not_check_commit_dates(self):
        self.commit(author_offset="+0123", committer_offset="-0042")
        self.assertEqual(list(check_pii.scan_tree([])), [])
        self.assertEqual(list(check_pii.scan_history([])), [])

    def test_all_coordinate_rules_apply_to_commit_messages_without_fixture_exemptions(self):
        examples = ((nmea("RMC"), "NMEA position sentence"),
                    (f'<trkpt lat="{DECIMAL}" lon="{DECIMAL}"/>',
                     "GPX latitude/longitude attributes"),
                    (DECIMAL + ", " + DECIMAL, "decimal coordinate pair"))
        for text, rule in examples:
            with self.subTest(rule=rule):
                sha = self.commit(message="Synthetic test\n\nserver/sample.gpx: " + text)
                self.assertEqual(list(check_pii.scan_commits(sha, [])),
                                 [f"commit {sha[:12]} message:3: {rule}"])


TERM = "fictionalterm"  # Invented; the tests never use real denylist content.
ACCENTED = "zo\u00eb"  # Invented name, precomposed (NFC).


class RangeFiles(unittest.TestCase):
    """--commits scans the file versions a range adds, not just its final tree."""

    DENY = [check_pii.term_pattern(TERM)]

    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=check_pii.ROOT)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        root_patch = patch.object(check_pii, "ROOT", self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        self.env = dict(os.environ, GIT_AUTHOR_NAME="Scanner Test", GIT_COMMITTER_NAME="Scanner Test",
                        GIT_AUTHOR_EMAIL="scanner@users.noreply.github.com",
                        GIT_COMMITTER_EMAIL="scanner@users.noreply.github.com",
                        GIT_AUTHOR_DATE="946684800 +0000", GIT_COMMITTER_DATE="946684800 +0000")
        self.git("init", "-q")
        self.git("config", "core.autocrlf", "false")
        self.commit("base", {"readme.txt": "Synthetic scanner fixture\n"})
        self.mark_base()

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, capture_output=True,
                              check=True, env=self.env).stdout

    def mark_base(self):
        self.base = self.git("rev-parse", "HEAD").decode().strip()

    def commit(self, message, files=None, remove=(), move=()):
        for name, data in (files or {}).items():
            (self.root / name).parent.mkdir(exist_ok=True)
            (self.root / name).write_bytes(data if isinstance(data, bytes) else data.encode())
            self.git("add", name)
        for name in remove:
            self.git("rm", "-q", name)
        for old, new in move:
            self.git("mv", old, new)
        self.git("commit", "-q", "-m", message)

    def findings(self):
        return list(check_pii.scan_commits(self.base + "..HEAD", self.DENY))

    def test_added_then_deleted_file_is_found(self):
        self.commit("add", {"notes.txt": "met " + TERM + "\n"})
        self.commit("delete", remove=["notes.txt"])
        self.assertEqual(list(check_pii.scan_tree(self.DENY)), [])
        found = self.findings()
        self.assertEqual(len(found), 1)
        self.assertRegex(found[0], r"^\(range [0-9a-f]{8}\) notes\.txt:1: personal term from the denylist$")

    def test_added_then_redacted_file_is_found(self):
        self.commit("add", {"notes.txt": "met " + TERM + "\n"})
        self.commit("redact", {"notes.txt": "met <name>\n"})
        self.assertEqual(list(check_pii.scan_tree(self.DENY)), [])
        self.assertEqual(len(self.findings()), 1)

    def test_renamed_then_deleted_file_is_found(self):
        self.commit("add", {"notes.txt": "met " + TERM + "\n"})
        self.commit("move", move=[("notes.txt", "other.txt")])
        self.commit("delete", remove=["other.txt"])
        self.assertEqual(len(self.findings()), 1)

    def test_rename_to_a_bad_name_and_back_is_found_even_if_the_base_has_the_file(self):
        # The file version is already in the base, so only its names are new.
        for name, rule in ((TERM + "-notes.txt", "file name has a personal term from the denylist"),
                           (".env", "secrets file must not be committed")):
            with self.subTest(name=name):
                self.commit("add " + name, {"keep" + name[:1] + ".txt": "harmless\n"})
                self.mark_base()
                old = "keep" + name[:1] + ".txt"
                self.commit("rename", move=[(old, name)])
                self.commit("rename back", move=[(name, old)])
                self.assertEqual(list(check_pii.scan_tree(self.DENY)), [])
                self.assertEqual(self.findings(), [f"(range) {name}: {rule}"])

    def test_versions_already_in_the_base_are_not_rescanned(self):
        self.commit("old leak", {"old.txt": TERM + "\n"})
        self.mark_base()
        self.commit("later", {"new.txt": "clean\n"})
        self.assertEqual(self.findings(), [])

    def test_a_range_that_adds_no_files_is_fine(self):
        self.commit("add", {"a.txt": "clean\n"})
        self.mark_base()
        self.commit("delete", remove=["a.txt"])
        self.assertEqual(self.findings(), [])

    def test_fixture_allowlists_apply_to_removed_versions_too(self):
        gpx = f'<trkpt lat="{DECIMAL}" lon="{DECIMAL}"/>\n'
        self.commit("add", {"server/sample.gpx": gpx, "server/new.gpx": gpx})
        self.commit("delete", remove=["server/sample.gpx", "server/new.gpx"])
        found = self.findings()
        self.assertEqual(len(found), 2)
        self.assertTrue(all("server/new.gpx" in f for f in found), found)
        self.assertTrue(any("isn't a known synthetic fixture" in f for f in found))
        self.assertTrue(any("new.gpx:1: GPX latitude/longitude attributes" in f for f in found))

    def test_utf16_file_added_then_deleted_is_found(self):
        self.commit("add", {"dump.txt": codecs.BOM_UTF16_LE + (TERM + "\n").encode("utf-16-le")})
        self.commit("delete", remove=["dump.txt"])
        self.assertEqual(len(self.findings()), 1)

    def test_unicode_spellings_match_in_range_contents_and_messages(self):
        nfd = unicodedata.normalize("NFD", ACCENTED)
        self.commit("thanks " + nfd, {"notes.txt": "met " + nfd + "\n"})
        self.commit("delete", remove=["notes.txt"])
        found = list(check_pii.scan_commits(self.base + "..HEAD",
                                            [check_pii.term_pattern(ACCENTED)]))
        self.assertEqual(len(found), 2)
        self.assertTrue(any(" message:1: " in f for f in found))
        self.assertTrue(any(f.startswith("(range ") for f in found))


class DenylistUnicode(unittest.TestCase):
    def test_nfc_and_nfd_spellings_match_in_both_directions(self):
        nfc, nfd = ACCENTED, unicodedata.normalize("NFD", ACCENTED)
        self.assertNotEqual(nfc, nfd)
        for term, text in ((nfc, nfc), (nfc, nfd), (nfd, nfc), (nfd, nfd)):
            with self.subTest(term_is_nfc=term == nfc, text_is_nfc=text == nfc):
                self.assertEqual(hits("met " + text + " today", deny=[term]),
                                 ["f:1: personal term from the denylist"])

    def test_compatibility_forms_match_and_line_numbers_stay_put(self):
        fullwidth = "".join(chr(ord(c) + 0xFEE0) for c in TERM)
        self.assertEqual(hits("one\ntwo " + fullwidth, deny=[TERM]),
                         ["f:2: personal term from the denylist"])

    def test_word_boundaries_still_apply(self):
        self.assertEqual(hits("un" + ACCENTED + "x s" + TERM, deny=[TERM, "zoe"]), [])

    def test_file_names_are_normalised_too(self):
        for term, name in ((ACCENTED, unicodedata.normalize("NFD", ACCENTED)),
                           (unicodedata.normalize("NFD", ACCENTED), ACCENTED)):
            deny = [check_pii.term_pattern(term)]
            self.assertEqual(list(check_pii.check_path("notes-" + name + ".md", deny)),
                             [f"notes-{name}.md: file name has a personal term from the denylist"])

    def test_commit_text_is_normalised_too(self):
        deny = [check_pii.term_pattern(ACCENTED)]
        for name in (ACCENTED, unicodedata.normalize("NFD", ACCENTED)):
            self.assertEqual(
                list(check_pii.scan_text("commit abc message", "thanks " + name, deny)),
                ["commit abc message:1: personal term from the denylist"])


class Encodings(unittest.TestCase):
    TEXT = "line one\nmet " + TERM + " here\r\nline three\n"
    DENY = [check_pii.term_pattern(TERM)]
    FINDING = "notes.txt:2: personal term from the denylist"

    def scan(self, data, path="notes.txt"):
        return list(check_pii.check_blob(path, data, self.DENY))

    def test_utf8_control(self):
        self.assertEqual(self.scan(self.TEXT.encode()), [self.FINDING])

    def test_utf16_and_utf32_with_a_byte_order_mark(self):
        for codec, bom in (("utf-16-le", codecs.BOM_UTF16_LE), ("utf-16-be", codecs.BOM_UTF16_BE),
                           ("utf-32-le", codecs.BOM_UTF32_LE), ("utf-32-be", codecs.BOM_UTF32_BE),
                           ("utf-8", codecs.BOM_UTF8)):
            with self.subTest(codec=codec):
                self.assertEqual(self.scan(bom + self.TEXT.encode(codec)), [self.FINDING])

    def test_utf16_without_a_byte_order_mark(self):
        for codec in ("utf-16-le", "utf-16-be"):
            with self.subTest(codec=codec):
                self.assertEqual(self.scan(self.TEXT.encode(codec)), [self.FINDING])

    def test_other_rules_see_decoded_utf16_too(self):
        text = "see C:\\" + USERS + "\\alice\\x\n"
        self.assertEqual(self.scan(codecs.BOM_UTF16_LE + text.encode("utf-16-le")),
                         ["notes.txt:1: home directory path with an account name"])

    def test_nul_in_otherwise_utf8_text_is_refused_and_still_scanned(self):
        self.assertEqual(self.scan(b"\0" + self.TEXT.encode()),
                         ["notes.txt: text file has NUL bytes in an unrecognised encoding "
                          "(save it as UTF-8)",
                          "notes.txt [binary strings]:2: personal term from the denylist"])

    def test_binary_data_is_scanned_for_readable_runs_without_the_refusal(self):
        finding = "firmware/app.bin [binary strings]:1: personal term from the denylist"
        for name, data in (
                ("ascii", b"\x7fELF\0\0\x01\0" + TERM.encode() + b"\0\xff\xfe"),
                ("utf-16-le", b"\x01\x02\0\0\0" + TERM.encode("utf-16-le") + b"\0\0\0"),
                ("utf-16-be", b"\x01\x02\0\0\0" + TERM.encode("utf-16-be") + b"\0\0")):
            with self.subTest(runs=name):
                self.assertEqual(self.scan(data, "firmware/app.bin"), [finding])

    def test_ordinary_binary_data_images_and_tiny_files_still_pass(self):
        self.assertEqual(self.scan(bytes(range(256)) * 8, "firmware/app.bin"), [])
        self.assertEqual(self.scan(b"\x89PNG\r\n\x1a\n\0\0\0\rIHDR" + TERM.encode(),
                                   "docs/enclosure.png"), [])
        self.assertEqual(self.scan(b"", "empty.txt"), [])
        self.assertEqual(self.scan(b"\xff", "one.txt"), [])

    def test_vendored_files_are_still_skipped(self):
        path = sorted(check_pii.VENDORED)[0]
        self.assertEqual(self.scan(self.TEXT.encode(), path), [])


class MonitorEnv(unittest.TestCase):
    def test_opaque_token_in_monitor_env_is_refused_by_name(self):
        token = b"METRICS_TOKEN=" + b"q7Zk" * 10
        for path in ("server/monitor.env", "monitor.env"):
            self.assertEqual(list(check_pii.check_blob(path, token, [])),
                             [f"{path}: secrets file must not be committed"])
        self.assertEqual(list(check_pii.check_blob("server/monitor.env.example", token, [])), [])


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
                     "server/backup/backup.conf", ".pii-denylist", "server/monitor.env",
                     ".claude/settings.local.json", "annotations.db",
                     "server/data/2026-10/trip.json", "keys/id_ed25519"]:
            with self.subTest(path=path):
                self.assertTrue(self.problems(path))

    def test_only_known_synthetic_gpx(self):
        self.assertEqual(self.problems("server/sample.gpx"), [])
        self.assertTrue(self.problems("server/tests/parked.gpx"))

    def test_examples_pass(self):
        for path in ["server/.env.example", "server/viewer.env.example",
                     "server/monitor.env.example",
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

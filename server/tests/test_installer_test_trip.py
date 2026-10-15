"""install.sh's test upload must only ever delete its own test trip (#43).

Sources the real install.sh functions (everything but the final `main`)
into bash, with curl replaced by a fake ingest service that can write
files the way the real one would, then runs upload_test_trip and the
EXIT cleanup. Needs bash, jq and openssl, so it skips on Windows.
"""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap
import unittest


SERVER = Path(__file__).resolve().parents[1]
INSTALL = SERVER / "install.sh"
SAMPLE = SERVER / "sample.gpx"

HARNESS = textwrap.dedent("""\
    source <(sed '$d' "$INSTALL")
    MODE=check
    INGEST_URL=http://ingest.invalid
    env_raw() { printf 'the-token'; }
    if [[ -n ${FAKE_HEX:-} ]]; then
        openssl() { printf '%s\\n' "$FAKE_HEX"; }
    fi
    # The ingest service: optionally writes files, then answers.
    curl() {
        echo upload >> curl.log
        if [[ -n ${FAKE_WRITE:-} ]]; then
            mkdir -p "${FAKE_WRITE%/*}"
            printf '%s' "$FAKE_CONTENT" > "$FAKE_WRITE.gpx"
            printf '{}' > "$FAKE_WRITE.json"
        fi
        if [[ $FAKE_CODE == 000 ]]; then return 28; fi
        printf '%s\\n%s' "$FAKE_BODY" "$FAKE_CODE"
    }
    cd "$WORK"
    if upload_test_trip; then result=0; else result=1; fi
    printf 'RESULT=%s\\nTEST_ID=%s\\n' "$result" "$TEST_ID"
    cleanup
    """)


def tools_missing():
    if os.name == "nt":
        return "needs a POSIX shell"
    for tool in ("bash", "jq", "openssl"):
        if shutil.which(tool) is None:
            return f"{tool} not installed"
    return None


@unittest.skipIf(tools_missing(), tools_missing())
class InstallerTestTrip(unittest.TestCase):
    HEX = "0123456789ab"
    ID = "install-check-0123456789ab"

    def setUp(self):
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work)
        shutil.copy(SAMPLE, self.work / "sample.gpx")
        self.data = self.work / "data"
        self.data.mkdir()
        self.month = self.data / "2026-04"
        self.own = self.month / self.ID

    def add_trip(self, name, content):
        self.month.mkdir(exist_ok=True)
        for ext in (".gpx", ".json"):
            (self.month / (name + ext)).write_text(f"{content}{ext}")

    def assert_trip_intact(self, name, content):
        for ext in (".gpx", ".json"):
            self.assertEqual((self.month / (name + ext)).read_text(),
                             f"{content}{ext}")

    def run_check(self, code, body="", write=None, content="", hex_=HEX):
        env = dict(os.environ, INSTALL=str(INSTALL), WORK=str(self.work),
                   FAKE_HEX=hex_ or "", FAKE_CODE=code, FAKE_BODY=body,
                   FAKE_WRITE=str(write.relative_to(self.work)) if write else "",
                   FAKE_CONTENT=content)
        proc = subprocess.run(["bash", "-c", HARNESS], env=env, text=True,
                              capture_output=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        out = dict(line.split("=", 1) for line in proc.stdout.splitlines()
                   if line.startswith(("RESULT=", "TEST_ID=")))
        log = self.work / "curl.log"
        out["uploads"] = len(log.read_text().splitlines()) if log.exists() else 0
        out["output"] = proc.stdout
        return out

    def accepted(self):
        return ('{"status": "accepted", "trip_id": "%s", '
                '"stored_as": "2026-04/%s.gpx", "distance_miles": 0.3}'
                % (self.ID, self.ID))

    def test_existing_trip_at_the_test_path_is_not_touched(self):
        self.add_trip(self.ID, "someone else's trip")
        out = self.run_check("200", self.accepted())
        self.assertEqual(out["RESULT"], "1")
        self.assertEqual(out["uploads"], 0)
        self.assertIn("already exists", out["output"])
        self.assert_trip_intact(self.ID, "someone else's trip")

    def test_refused_upload_deletes_nothing(self):
        # The issue's case: the server answers 409 because what's at that
        # path isn't this upload. Here it appears between the check and
        # the upload; the old fixed-ID trip and a real one are next to it.
        self.add_trip("install-check", "old fixed-ID trip")
        self.add_trip("2026-04-14T14-30-00Z", "real trip")
        out = self.run_check(
            "409", '{"error": "trip ID already contains different GPX data"}',
            write=self.own, content="racing trip")
        self.assertEqual(out["RESULT"], "1")
        self.assertEqual((self.own.with_suffix(".gpx")).read_text(), "racing trip")
        self.assertTrue(self.own.with_suffix(".json").exists())
        self.assert_trip_intact("install-check", "old fixed-ID trip")
        self.assert_trip_intact("2026-04-14T14-30-00Z", "real trip")

    def test_timeout_cleans_up_what_the_upload_wrote(self):
        out = self.run_check("000", write=self.own, content="partial")
        self.assertEqual(out["RESULT"], "1")
        self.assertFalse(self.own.with_suffix(".gpx").exists())
        self.assertFalse(self.own.with_suffix(".json").exists())
        # The upload made the month folder, so cleanup takes it too.
        self.assertFalse(self.month.exists())

    def test_success_removes_only_its_own_trip(self):
        self.add_trip("2026-04-14T14-30-00Z", "real trip")
        out = self.run_check("200", self.accepted(), write=self.own,
                             content=SAMPLE.read_text())
        self.assertEqual(out["RESULT"], "0", out["output"])
        self.assertFalse(self.own.with_suffix(".gpx").exists())
        self.assertFalse(self.own.with_suffix(".json").exists())
        self.assert_trip_intact("2026-04-14T14-30-00Z", "real trip")

    def test_each_run_gets_its_own_id(self):
        ids = {self.run_check("401", "{}", hex_=None)["TEST_ID"]
               for _ in range(2)}
        self.assertEqual(len(ids), 2)
        for test_id in ids:
            self.assertRegex(test_id, r"^install-check-[0-9a-f]{12}$")


if __name__ == "__main__":
    unittest.main()

"""Input-hardening tests for both services (audit B1, B4, B5).

Stdlib only:  python -m unittest discover -s server/tests -v
(needs flask, openpyxl + tzdata installed, as in the two requirements.txt)
"""

import csv
import importlib.util
import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from openpyxl import load_workbook
from werkzeug.security import generate_password_hash

HERE = Path(__file__).resolve().parent
SERVER = HERE.parent


def _load(name, rel, env):
    # Both apps read their config at import, so set the env first.
    os.environ.update(env)
    spec = importlib.util.spec_from_file_location(name, SERVER / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


TOKEN = "test-token"
ING_DATA = Path(tempfile.mkdtemp())
ing = _load(
    "ingest_hardening",
    "mileage-ingest/app.py",
    {"INGEST_AUTH_TOKEN": TOKEN, "INGEST_DATA_ROOT": str(ING_DATA),
     "DISPLAY_TZ": "America/Los_Angeles"},
)

VIEW_DATA = Path(tempfile.mkdtemp())
VIEW_DB = Path(tempfile.mkdtemp()) / "annotations.db"
view = _load(
    "viewer_hardening",
    "mileage-viewer/app.py",
    {
        "INGEST_DATA_ROOT": str(VIEW_DATA),
        "VIEWER_DB_PATH": str(VIEW_DB),
        "VIEWER_USERNAME": "tester",
        # Cheap hash: these tests aren't about hash strength.
        "VIEWER_PASSWORD_HASH": generate_password_hash("pw", "pbkdf2:sha256:1000"),
        "VIEWER_SECRET_KEY": "x" * 32,
        "DISPLAY_TZ": "America/Los_Angeles",
    },
)

GOOD_GPX = (
    b'<?xml version="1.0"?><gpx version="1.0" xmlns="http://www.topografix.com/GPX/1/0">'
    b'<trk><trkseg>'
    b'<trkpt lat="37.0" lon="-122.0"><time>2026-04-14T14:30:00Z</time></trkpt>'
    b'<trkpt lat="37.01" lon="-122.0"><time>2026-04-14T14:31:00Z</time></trkpt>'
    b"</trkseg></trk></gpx>"
)


def _stored_files():
    return sorted(p.name for p in ING_DATA.rglob("*") if p.is_file())


class IngestAuth(unittest.TestCase):
    """B4: non-ASCII bearer tokens are a 401, not a 500."""

    def setUp(self):
        self.c = ing.app.test_client()

    def post(self, auth, body=GOOD_GPX):
        return self.c.post("/upload", data=body, headers={"Authorization": auth})

    def test_non_ascii_token_is_401(self):
        self.assertEqual(self.post("Bearer café").status_code, 401)

    def test_wrong_ascii_token_is_401(self):
        self.assertEqual(self.post("Bearer nope").status_code, 401)

    def test_right_token_is_accepted(self):
        self.assertEqual(self.post(f"Bearer {TOKEN}").status_code, 200)


class IngestEmptyGpx(unittest.TestCase):
    """B5: a GPX with no track points is rejected and nothing is written."""

    def setUp(self):
        self.c = ing.app.test_client()

    def assert_rejected(self, body):
        before = _stored_files()
        r = self.c.post("/upload", data=body, headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIn("no track points", r.get_json()["error"])
        self.assertEqual(_stored_files(), before)

    def test_bare_gpx(self):
        self.assert_rejected(b"<gpx></gpx>")

    def test_empty_track_and_segment(self):
        self.assert_rejected(b"<gpx><trk><trkseg></trkseg></trk></gpx>")

    def test_points_without_coordinates(self):
        # parse_gpx drops trkpts with no lat/lon, leaving nothing.
        self.assert_rejected(b"<gpx><trk><trkseg><trkpt/></trkseg></trk></gpx>")

    def test_metadata_time_but_no_points(self):
        self.assert_rejected(
            b"<gpx><metadata><time>2026-04-14T14:30:00Z</time></metadata></gpx>"
        )


class ViewerAuth(unittest.TestCase):
    """B4: non-ASCII login fields and CSRF tokens never 500."""

    def setUp(self):
        self.c = view.app.test_client()
        # Each test gets a fresh lockout counter.
        with closing(sqlite3.connect(view.DB_PATH)) as conn, conn:
            conn.execute("DELETE FROM login_attempts")

    def with_session(self, **values):
        with self.c.session_transaction() as s:
            s.update(values)

    def test_non_ascii_username_is_401(self):
        self.with_session(csrf="tok")
        r = self.c.post(
            "/login", data={"csrf_token": "tok", "username": "phíl", "password": "pw"}
        )
        self.assertEqual(r.status_code, 401)

    def test_non_ascii_login_csrf_is_400(self):
        self.with_session(csrf="tok")
        r = self.c.post(
            "/login", data={"csrf_token": "tök", "username": "tester", "password": "pw"}
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("Session expired", r.get_data(as_text=True))

    def test_non_ascii_csrf_on_authenticated_post_is_400(self):
        self.with_session(user="tester", pw_tag=view._password_tag(), csrf="tok")
        r = self.c.post(
            "/trip/2026-04/x/annotate", data={"csrf_token": "tök", "category": "business"}
        )
        self.assertEqual(r.status_code, 400)

    def test_correct_login_still_works(self):
        self.with_session(csrf="tok")
        r = self.c.post(
            "/login", data={"csrf_token": "tok", "username": "tester", "password": "pw"}
        )
        self.assertEqual(r.status_code, 302)


NOTES = {
    "eq": "=1+1",
    "minus": "-Client visit",
    "plus": "+1 555 0100",
    "at": "@SUM(A1)",
    "plain": "Client visit",
}


class ViewerExport(unittest.TestCase):
    """B1: notes that look like formulas export as text in both formats."""

    @classmethod
    def setUpClass(cls):
        ym = VIEW_DATA / "2026-04"
        ym.mkdir(exist_ok=True)
        for i, (trip_id, note) in enumerate(NOTES.items()):
            (ym / f"{trip_id}.json").write_text(
                json.dumps(
                    {
                        "trip_id": trip_id,
                        "start_time": f"2026-04-14T14:3{i}:00+00:00",
                        "distance_miles": 1.5,
                    }
                ),
                encoding="utf-8",
            )
            view.upsert_annotation(f"2026-04/{trip_id}", "business", note)

    def setUp(self):
        self.c = view.app.test_client()
        with self.c.session_transaction() as s:
            s.update(user="tester", pw_tag=view._password_tag(), csrf="tok")

    def column(self, rows, name):
        header, *body = rows
        idx = header.index(name)
        return {r[header.index("trip_id")]: r[idx] for r in body}

    def test_xlsx_notes_are_literal_text(self):
        r = self.c.get("/export.xlsx")
        self.assertEqual(r.status_code, 200)
        ws = load_workbook(io.BytesIO(r.data)).active
        header = [c.value for c in ws[1]]
        note_col = header.index("purpose_note")
        id_col = header.index("trip_id")
        for row in ws.iter_rows(min_row=2):
            note = row[note_col]
            self.assertEqual(note.data_type, "s", note.value)
            # Stored exactly as typed: no quote prefix in XLSX.
            self.assertEqual(note.value, NOTES[row[id_col].value])
        # Numbers stay numbers.
        dist = header.index("distance_miles")
        self.assertEqual(ws.cell(row=2, column=dist + 1).data_type, "n")

    def test_csv_notes_are_quote_prefixed(self):
        r = self.c.get("/export.csv")
        self.assertEqual(r.status_code, 200)
        rows = list(csv.reader(io.StringIO(r.get_data(as_text=True))))
        notes = self.column(rows, "purpose_note")
        for trip_id, note in NOTES.items():
            want = note if trip_id == "plain" else "'" + note
            self.assertEqual(notes[trip_id], want)

    def test_csv_safe_leaves_numbers_alone(self):
        self.assertEqual(view._csv_safe(-1.5), -1.5)
        self.assertEqual(view._csv_safe("\rx"), "'\rx")
        self.assertEqual(view._csv_safe("\tx"), "'\tx")


if __name__ == "__main__":
    unittest.main()

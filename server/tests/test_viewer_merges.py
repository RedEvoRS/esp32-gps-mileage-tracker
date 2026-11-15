"""Synthetic split drives exercise persisted merges through the viewer routes."""

import builtins
import csv
import html
import io
import json
import os
import re
import shutil
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

from openpyxl import load_workbook
from werkzeug.security import generate_password_hash

from test_round_trip import load_service, SERVER
from month_link_layouts import MONTH_LINK_LAYOUTS, make_layout


class ViewerMerges(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        self.month = self.data / "2026-04"
        self.month.mkdir(parents=True)
        self.db = self.root / "viewer" / "annotations.db"
        with patch.dict(os.environ, {
            "INGEST_DATA_ROOT": str(self.data), "VIEWER_DB_PATH": str(self.db),
            "VIEWER_USERNAME": "driver", "VIEWER_PASSWORD_HASH": generate_password_hash("test-password"),
            "VIEWER_SECRET_KEY": "merge-test-session-key-" * 3, "DISPLAY_TZ": "UTC",
            "VIEWER_GEOCODER_URL": "",
        }):
            self.viewer = load_service("mileage-viewer")
        self.client = self.viewer.app.test_client()
        self.authenticate(self.client)
        self.base = datetime(2026, 4, 14, 12, tzinfo=timezone.utc)

    def authenticate(self, client):
        with client.session_transaction() as session:
            session.clear()
            session.update(user="driver", pw_tag=self.viewer._password_tag(), csrf="test-csrf")

    def trip(self, name, seconds, offset=0, device="test-device", legacy=False):
        from common.trip_summary import parse_gpx, summarize
        # Equatorial ocean fixture: no real trip/location data.
        points = []
        for i, speed in ((0, 10), (30, 10), (60, 0)):
            points.append(f'<trkpt lat="0" lon="{offset + i * .00001}">'
                          f'<time>{(self.base + timedelta(seconds=seconds + i)).isoformat()}</time>'
                          f'<speed>{speed}</speed><hdop>1</hdop></trkpt>')
        raw = ('<gpx><trk><trkseg>' + ''.join(points) + '</trkseg></trk></gpx>').encode()
        summary = {k: v for k, v in summarize(parse_gpx(raw)).items() if not k.startswith("_")}
        summary.update(trip_id=name, device_id=device, gpx_file=f"2026-04/{name}.gpx")
        if legacy:
            summary.pop("recorded_until")
        (self.month / (name + ".gpx")).write_bytes(raw)
        (self.month / (name + ".json")).write_text(json.dumps(summary), encoding="utf-8")
        return f"2026-04/{name}"

    def merge(self, key, following, client=None):
        return (client or self.client).post(f"/trip/{key}/merge?year=2026", data={
            "csrf_token": "test-csrf", "next_trip": following,
        })

    def merged_key(self, response):
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True))
        return response.headers["Location"].split("/trip/")[1].split("?")[0]

    def annotate(self, key, category="", note="", destination=""):
        return self.client.post(f"/trip/{key}/annotate", data={
            "csrf_token": "test-csrf", "category": category,
            "purpose_note": note, "destination": destination,
        })

    def split(self, key):
        return self.client.post(f"/trip/{key}/split", data={"csrf_token": "test-csrf", "confirm": "yes"})

    def symlink(self, link, target, directory=False):
        try:
            link.symlink_to(target, target_is_directory=directory)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")

    def unused_merge(self):
        a, b = self.trip("a", 0), self.trip("b", 180, .01)
        self.annotate(a, "business", "original note")
        key = self.merged_key(self.merge(a, b))
        self.annotate(key, "business", "merged note")
        self.month.rename(self.data / "archive")
        make_layout(self.data, MONTH_LINK_LAYOUTS["merge-conflict"], self.symlink)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO annotations (trip_key,category) VALUES ('2026-03/a','personal')")
        return key

    def undo_unused(self, key, **data):
        return self.client.post(f"/admin/unused-merge/{key}/undo", data={
            "csrf_token": "test-csrf", "confirm": "yes", **data})

    def discard(self, key, **data):
        return self.client.post(f'/admin/damaged-merge/{key}/discard', data={
            'csrf_token': 'test-csrf', 'confirm': 'yes', **data})

    def damaged_merge(self):
        a, b = self.trip('a', 0), self.trip('b', 180, .01)
        self.annotate(a, 'personal', 'original note', 'original stop')
        key = self.merged_key(self.merge(a, b))
        self.annotate(key, 'business', 'merged note', 'example stop')
        with closing(sqlite3.connect(self.db)) as conn:
            summary = conn.execute('SELECT summary FROM trip_merges').fetchone()[0]
        shutil.rmtree(self.month)
        return key, [a, b], summary

    def test_damaged_merge_discard_is_admin_only_post_csrf_and_explicit_confirmation(self):
        key, _, _ = self.damaged_merge()
        from test_viewer_roles import ViewerRolesTests
        ViewerRolesTests.seed_editor(self)
        ViewerRolesTests.signed_in(self, 'editor')
        with self.client.session_transaction() as session:
            session['csrf'] = 'test-csrf'
        page = html.unescape(self.client.get('/').get_data(as_text=True))
        self.assertNotIn('>Undo merge (split back)</button>', page)
        self.assertNotIn('>Discard damaged merge</button>', page)
        self.assertIn('Please ask the admin to restore the files or discard this damaged merge.', page)
        self.assertEqual(self.split(key).status_code, 409)
        self.assertEqual(self.discard(key).status_code, 403)
        self.assertEqual(self.client.get('/admin/year/2026').status_code, 403)
        self.authenticate(self.client)
        page = html.unescape(self.client.get('/').get_data(as_text=True))
        self.assertNotIn('>Undo merge (split back)</button>', page)
        self.assertIn('>Discard damaged merge</button>', page)
        self.assertIn(self.viewer.DAMAGED_MERGE_DISCARD_EFFECT, page)
        self.assertEqual(self.client.get(f'/admin/damaged-merge/{key}/discard').status_code, 405)
        self.assertEqual(self.discard(key, csrf_token='bad').status_code, 400)
        self.assertEqual(self.discard(key, confirm='no').status_code, 400)
        anonymous = self.viewer.app.test_client()
        self.assertEqual(anonymous.post(f'/admin/damaged-merge/{key}/discard').status_code, 401)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM trip_merges').fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM trip_history WHERE action='discard_damaged_merge'").fetchone()[0], 0)

    def test_discard_preserves_full_saved_record_before_deletion_and_in_year_review(self):
        key, parts, summary = self.damaged_merge()
        original_record = self.viewer.trip_log.record
        def record(conn, record_key, action, details, **actor):
            if action == 'discard_damaged_merge':
                self.assertTrue(conn.in_transaction)
                self.assertEqual(conn.execute('SELECT summary FROM trip_merges WHERE trip_key=?', (key,)).fetchone()[0], summary)
            return original_record(conn, record_key, action, details, **actor)
        with patch.object(self.viewer.trip_log, 'record', side_effect=record):
            self.assertEqual(self.discard(key).status_code, 302)
        self.assertEqual(self.discard(key).status_code, 409)
        with closing(sqlite3.connect(self.db)) as conn:
            rows = conn.execute("SELECT trip_key, actor, actor_role, at, details FROM trip_history WHERE action='discard_damaged_merge'").fetchall()
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row[:3], (key, 'driver', 'admin'))
            self.assertIsNotNone(self.viewer.trip_log.parse_utc(row[3]))
            details = json.loads(row[4])
            self.assertEqual(details['parts'], parts)
            self.assertEqual(details['saved_summary_raw'], summary)
            self.assertEqual(details['saved_summary'], json.loads(summary))
            for field in ('start_time', 'end_time', 'distance_miles'):
                self.assertIsNotNone(details['saved_summary'][field])
            self.assertEqual(details['dropped'], {'category': 'business', 'purpose_note': 'merged note', 'destination': 'example stop'})
            self.assertEqual(details['original_annotations'][parts[0]],
                             {'category': 'personal', 'purpose_note': 'original note', 'destination': 'original stop'})
            for table in ('trip_merges', 'trip_merge_parts', 'trip_merge_aliases'):
                self.assertEqual(conn.execute('SELECT count(*) FROM ' + table).fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT purpose_note FROM annotations WHERE trip_key=?', (parts[0],)).fetchone()[0], 'original note')
            self.assertEqual(conn.execute("SELECT count(*) FROM trip_history WHERE action='split'").fetchone()[0], 0)
        review = html.unescape(self.client.get('/admin/year/2026').get_data(as_text=True))
        self.assertIn('Discarded or undone saved merges (1)', review)
        self.assertIn(f"{json.loads(summary)['distance_miles']:.3f} mi", review)
        self.assertIn('purpose: business; purpose note: merged note', review)
        self.assertIn('driver', review)
        self.assertIn(self.viewer.fmt_date(row[3]), review)
        self.assertIn('None of its trips are in the log; these saved miles are not in the totals.', review)
        self.assertNotIn('Discarded or undone saved merges', self.client.get('/admin/year/2025').get_data(as_text=True))
        self.assertIn(2026, [r['year'] for r in self.viewer._year_list()])
        self.assertEqual(self.close_year().status_code, 302)
        self.assertIn('Discarded or undone saved merges (1)', self.client.get('/admin/year/2026').get_data(as_text=True))
        history = self.client.get('/admin/history').get_data(as_text=True)
        self.assertIn('Damaged merge discarded', history)
        self.assertIn("The merged trip's saved miles and purpose were removed from the totals and kept in this history record.",
                      html.unescape(history))

    def discard_with_readable_parts(self, missing_summaries=()):
        a, b = self.trip('a', 0), self.trip('b', 180, .01)
        self.annotate(a, 'personal', 'original note')
        key = self.merged_key(self.merge(a, b))
        self.annotate(key, 'business', 'merged note')
        (self.month / 'b.gpx').unlink()
        for name in missing_summaries:
            (self.month / (name + '.json')).unlink()
        self.assertEqual(self.discard(key).status_code, 302)
        return key

    def assert_discard_current_statement(self, statement):
        with closing(sqlite3.connect(self.db)) as conn:
            rows = self.viewer._merge_history_rows(conn, 2026, self.viewer.load_trips()[0])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        expected = (f"Damaged merge discarded. Saved miles: 0.732 mi; purpose: business; purpose note: merged note; "
                    f"trip date: 2026-04-14 12:00 UTC. Recorded at: {self.viewer.fmt_date(row['at'])}; login: driver. "
                    + statement)
        self.assertEqual(self.viewer.merge_history_message(row), expected)
        review = html.unescape(self.client.get('/admin/year/2026').get_data(as_text=True))
        self.assertIn(expected, review)
        self.assertNotIn('Saved miles (excluded)', review)
        self.assert_history_exports(expected, 'damaged merge discarded')

    def assert_history_exports(self, expected, label):
        for suffix in ('?year=2026', ''):
            rows = list(csv.DictReader(io.StringIO(self.client.get('/export.csv' + suffix).get_data(as_text=True))))
            records = [r for r in rows if r['row_type'] == 'unreadable' and label.capitalize() in r['purpose_note']]
            # The label begins a sentence in the shared formatter.
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]['distance_miles'], '')
            self.assertEqual(records[0]['purpose_note'], expected)
            book = load_workbook(io.BytesIO(self.client.get('/export.xlsx' + suffix).data))
            try:
                header, *values = list(book['Unreadable trips'].values)
                records = [dict(zip(header, r, strict=True)) for r in values if expected == r[header.index('purpose_note')]]
                self.assertEqual(len(records), 1)
                self.assertTrue(records[0]['trip_id'].endswith(' (' + label + ')'))
                self.assertIsNone(records[0]['distance_miles'])
            finally:
                book.close()
        rows = list(csv.DictReader(io.StringIO(self.client.get('/export.csv?year=2025').get_data(as_text=True))))
        self.assertFalse(any(label.capitalize() in r['purpose_note'] for r in rows))

    def test_discard_with_all_summaries_readable_reports_both_counted_again(self):
        self.discard_with_readable_parts()
        self.assert_discard_current_statement(
            'Now counted again: 2026-04/a, 2026-04/b; do not add these saved miles by hand.')
        trips, _ = self.viewer.load_trips()
        self.assertEqual({t['trip_key']: t['category'] for t in trips}, {'2026-04/a': 'personal', '2026-04/b': ''})

    def test_discard_with_one_summary_readable_reports_mixed_membership(self):
        self.discard_with_readable_parts(('b',))
        self.assert_discard_current_statement(
            'Now counted again: 2026-04/a; do not add these saved miles by hand. Not currently counted: 2026-04/b.')

    def test_discard_with_no_summaries_readable_reports_none_counted(self):
        self.discard_with_readable_parts(('a', 'b'))
        self.assert_discard_current_statement('None of its trips are in the log; these saved miles are not in the totals.')

    def test_discard_then_restore_and_remerge_reports_parts_inside_current_merge(self):
        key, parts, _ = self.damaged_merge()
        self.assertEqual(self.discard(key).status_code, 302)
        self.month.mkdir()
        self.trip('a', 0)
        self.trip('b', 180, .01)
        merged = self.merged_key(self.merge(*parts))
        self.annotate(merged, 'business', 'merged note')
        self.assert_discard_current_statement(
            f'Now counted again: 2026-04/a (inside {merged}), 2026-04/b (inside {merged}); '
            'do not add these saved miles by hand.')

    def test_history_count_computation_is_absent_on_list_and_reviews_without_records(self):
        self.trip('a', 0)
        with patch.object(self.viewer, '_merge_history_counted_parts', side_effect=AssertionError('unexpected work')):
            self.assertEqual(self.client.get('/').status_code, 200)
            self.assertEqual(self.client.get('/admin/year/2026').status_code, 200)
        self.discard_with_readable_parts()
        with patch.object(self.viewer, '_merge_history_counted_parts', side_effect=AssertionError('unexpected work')):
            self.assertEqual(self.client.get('/').status_code, 200)
            self.assertEqual(self.client.get('/admin/year/2025').status_code, 200)

    def test_discard_and_history_roll_back_together_if_deletion_fails(self):
        key, parts, _ = self.damaged_merge()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("CREATE TRIGGER refuse_delete BEFORE DELETE ON trip_merges BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END")
        self.viewer.app.config['TESTING'] = True
        with self.assertRaises(sqlite3.IntegrityError):
            self.discard(key)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM trip_merges').fetchone()[0], 1)
            self.assertEqual(self.viewer.trip_merges.parts(conn, key), parts)
            self.assertEqual(conn.execute("SELECT count(*) FROM trip_history WHERE action='discard_damaged_merge'").fetchone()[0], 0)

    def test_damaged_split_refusal_addresses_each_role(self):
        key, _, _ = self.damaged_merge()
        from test_viewer_roles import ViewerRolesTests
        ViewerRolesTests.seed_editor(self)
        for role, advice in (
            ('admin', 'Restore its files from backups or review the saved merge on the trip list.'),
            ('editor', 'Ask the admin to restore its files from backups or review the saved merge on the trip list.'),
        ):
            if role == 'editor':
                ViewerRolesTests.signed_in(self, role)
                with self.client.session_transaction() as session:
                    session['csrf'] = 'test-csrf'
            response = self.split(key)
            self.assertEqual(response.status_code, 409)
            self.assertIn('This merge has missing or unreadable parts. ' + advice,
                          html.unescape(response.get_data(as_text=True)))

    def test_discard_refuses_healthy_unused_or_repaired_merges_and_preserves_files(self):
        a, b = self.trip('a', 0), self.trip('b', 180, .01)
        key = self.merged_key(self.merge(a, b))
        files = {p: p.read_bytes() for p in self.month.iterdir()}
        self.assertEqual(self.discard(key).status_code, 409)
        (self.month / 'b.gpx').unlink()
        self.assertIn('>Discard damaged merge</button>', self.client.get('/').get_data(as_text=True))
        (self.month / 'b.gpx').write_bytes(files[self.month / 'b.gpx'])
        self.assertEqual(self.discard(key).status_code, 409)
        self.assertEqual({p: p.read_bytes() for p in self.month.iterdir()}, files)
        self.viewer.load_trips()  # Commit the repaired source's refreshed stamp.
        self.month.rename(self.data / '2026-05')
        self.assertTrue(self.viewer.load_trips()[1][0]['unused_merge'])
        self.assertEqual(self.discard(key).status_code, 409)

    def test_discard_respects_closed_year_and_saved_snapshot_then_reopen_allows_it(self):
        a, b = self.trip('a', 0), self.trip('b', 180, .01)
        key = self.merged_key(self.merge(a, b))
        self.assertEqual(self.close_year().status_code, 302)
        shutil.rmtree(self.month)
        self.assertEqual(self.discard(key).status_code, 409)
        self.assertEqual(self.split(key).status_code, 409)
        self.assertEqual(self.client.post('/admin/year/2026/reopen', data={
            'csrf_token': 'test-csrf', 'confirm': 'yes'}).status_code, 302)
        # Snapshot membership still locks it even if the saved date is another year.
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('INSERT INTO tax_years VALUES (2025,\'driver\',\'\',?)', (json.dumps({'trip_keys': [key]}),))
        self.assertEqual(self.discard(key).status_code, 409)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('DELETE FROM tax_years')
        self.assertEqual(self.discard(key).status_code, 302)

    def test_healthy_merge_undo_still_restores_original_annotations_for_both_roles(self):
        from test_viewer_roles import ViewerRolesTests
        ViewerRolesTests.seed_editor(self)
        for index, role in enumerate(('admin', 'editor')):
            a = self.trip(f'a{index}', index * 600)
            b = self.trip(f'b{index}', index * 600 + 180, .01)
            self.annotate(a, 'personal', 'original note')
            key = self.merged_key(self.merge(a, b))
            self.annotate(key, 'business', 'merged note')
            if role == 'editor':
                ViewerRolesTests.signed_in(self, role)
                with self.client.session_transaction() as session:
                    session['csrf'] = 'test-csrf'
            page = self.client.get('/trip/' + key).get_data(as_text=True)
            self.assertIn('>Undo merge (split back)</button>', page)
            self.assertIn('Restore each original trip and annotation, removing the current merged annotation.', page)
            self.assertEqual(self.split(key).status_code, 302)
            trips = {t['trip_key']: t for t in self.viewer.load_trips()[0]}
            self.assertEqual(trips[a]['purpose_note'], 'original note')
            self.assertEqual(trips[a]['category'], 'personal')
            self.assertEqual(trips[b]['purpose_note'], '')
            self.authenticate(self.client)

    def test_discard_uses_exact_damaged_saved_key_without_following_month_alias(self):
        key = self.unused_merge()
        (self.data / 'archive/b.gpx').unlink()
        self.assertEqual(self.discard(key.replace('2026-04/', '2026-03/')).status_code, 409)
        self.assertEqual(self.split(key).status_code, 409)
        self.assertEqual(self.discard(key).status_code, 302)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT trip_key FROM trip_history WHERE action='discard_damaged_merge'").fetchone()[0], key)

    @unittest.skipIf(os.name == 'nt' or getattr(os, 'geteuid', lambda: 0)() == 0, 'requires non-root POSIX permissions')
    def test_unreadable_part_without_stamp_change_requires_admin_discard(self):
        a, b = self.trip('a', 0), self.trip('b', 180, .01)
        key = self.merged_key(self.merge(a, b))
        source = self.month / 'b.gpx'
        saved_stat = source.stat()
        self.addCleanup(source.chmod, 0o600)
        source.chmod(0o000)
        self.assertEqual(source.stat().st_mtime_ns, saved_stat.st_mtime_ns)
        self.assertEqual(source.stat().st_size, saved_stat.st_size)
        broken = self.viewer.load_trips()[1][0]
        self.assertTrue(broken['damaged_merge'])
        self.assertEqual(self.split(key).status_code, 409)
        self.assertEqual(self.discard(key).status_code, 302)

    def assert_unused_merge(self, key, conflict):
        trips, entries = self.viewer.load_trips()
        self.assertEqual({t["trip_key"] for t in trips}, {"2026-03/a", "2026-03/b"})
        unused = next(e for e in entries if e.get("unused_merge"))
        with self.viewer._trip_read(trip_key=key) as (_, targeted, problems):
            self.assertEqual(targeted, [])
            self.assertEqual(problems, [unused])
        for role in ('admin', 'editor'):
            if role == 'editor':
                from test_viewer_roles import ViewerRolesTests
                if self.viewer.users.get_user(self.db, 'helper') is None:
                    ViewerRolesTests.seed_editor(self)
                ViewerRolesTests.signed_in(self, 'editor')
            response = self.client.get('/trip/' + key)
            self.assertEqual(response.status_code, 404)
            self.assertIn(self.viewer.folder_message(unused, role), html.unescape(response.get_data(as_text=True)))
            self.authenticate(self.client)
        self.assertEqual(unused["trip_key"], key)
        self.assertFalse(self.viewer.missing_mileage(unused))
        self.assertEqual(any(e.get("folder_reason") == "saved_keys" for e in entries), conflict)
        first = next(t for t in trips if t["id"] == "a")
        candidate, reason = self.viewer.trip_merges.next_trip(first, trips, entries)
        self.assertEqual(candidate["trip_key"] if candidate else None, "2026-03/b", reason)
        figures = self.viewer.year_figures(*self.viewer.year_members(trips, entries, 2026))
        self.assertEqual(figures["unreadable"], 0)
        self.assertNotIn(key, figures["trip_keys"])
        for route in ("/", "/admin/year/2026", "/export.csv", "/export.xlsx"):
            response = self.client.get(route)
            self.assertEqual(response.status_code, 200)
            message = self.viewer.folder_message(unused, "admin")
            if route.endswith(".xlsx"):
                book = load_workbook(io.BytesIO(response.data))
                try:
                    self.assertIn(message, str(list(book["Unreadable trips"].values)))
                finally:
                    book.close()
            else:
                self.assertIn(message, html.unescape(response.get_data(as_text=True)))
                self.assertNotIn("A merge part is missing or unreadable", response.get_data(as_text=True))
            response.close()
        rows = list(csv.DictReader(io.StringIO(self.client.get("/export.csv").get_data(as_text=True))))
        notice = next(r for r in rows if r["trip_id"] == key)
        self.assertEqual(notice["row_type"], "folder_notice")
        self.assertEqual(notice["distance_miles"], "")
        self.assertEqual(notice["category"], "business")
        self.assertEqual(sum(r["row_type"] == "trip" for r in rows), 2)
        self.assert_unused_close_refused(key)
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn(f'action="/admin/unused-merge/{key}/undo"', page)
        part_page = self.client.get("/trip/2026-03/a").get_data(as_text=True)
        self.assertIn('value="2026-03/b"', part_page)

    def test_unused_merge_during_conflict_after_removal_and_after_original_name_restored(self):
        key = self.unused_merge()
        self.assert_unused_merge(key, conflict=True)
        (self.data / "2026-04").unlink()
        self.assert_unused_merge(key, conflict=False)
        (self.data / "2026-03").unlink()
        self.symlink(self.month, self.data / "archive", directory=True)
        trips, entries = self.viewer.load_trips()
        self.assertEqual([t["trip_key"] for t in trips], [key])
        self.assertFalse(any(e.get("unused_merge") for e in entries))
        self.assertEqual(trips[0]["purpose_note"], "merged note")
        self.assertEqual(self.close_year().status_code, 302)
        with closing(sqlite3.connect(self.db)) as conn:
            snapshot = json.loads(conn.execute("SELECT snapshot FROM tax_years").fetchone()[0])
        self.assertEqual(snapshot['trip_keys'], [key])
        self.assertEqual(snapshot['totals']['business'], round(trips[0]['distance_miles'], 3))

    def close_year(self, year=2026):
        return self.client.post(f'/admin/year/{year}/close', data={'csrf_token': 'test-csrf', 'confirm': 'yes'})

    def assert_unused_close_refused(self, key, year=2026):
        response = self.close_year(year)
        self.assertEqual(response.status_code, 409)
        self.assertIn(self.viewer.UNUSED_MERGE_CLOSE_REFUSAL, html.unescape(response.get_data(as_text=True)))
        review = html.unescape(self.client.get(f'/admin/year/{year}').get_data(as_text=True))
        section = review.split('<h2 id="unused-merges-heading">', 1)[1].split('</section>', 1)[0]
        self.assertIn(key, section)
        self.assertIn('These saved miles and this purpose are not in the totals shown.', section)
        self.assertIn('The trips it was made from are counted separately.', section)
        self.assertIn(f'<button class="btn" disabled>Close {year}</button>', review)

    def moved_merge(self):
        a, b = self.trip('a', 0), self.trip('b', 180, .01)
        self.annotate(a, 'personal', 'original note')
        key = self.merged_key(self.merge(a, b))
        self.annotate(key, 'business', 'merged note')
        summary = self.viewer.load_trips()[0][0]
        self.month.rename(self.data / '2026-05')
        return key, summary

    def test_moved_real_month_blocks_close_and_exports_saved_miles_without_counting_them(self):
        key, saved = self.moved_merge()
        self.assert_unused_close_refused(key)
        trips, entries = self.viewer.load_trips()
        figures = self.viewer.year_figures(*self.viewer.year_members(trips, entries, 2026))
        self.assertEqual(figures['unreadable'], 0)
        self.assertEqual(figures['totals']['business'], 0)
        self.assertLess(figures['totals']['all'], saved['distance_miles'])
        review = html.unescape(self.client.get('/admin/year/2026').get_data(as_text=True))
        self.assertIn(f"Saved miles: {saved['distance_miles']:.3f} mi; purpose: business; purpose note: merged note.", review)
        rows = list(csv.DictReader(io.StringIO(self.client.get('/export.csv?year=2026').get_data(as_text=True))))
        notice = next(r for r in rows if r['trip_id'] == key)
        self.assertEqual(notice['distance_miles'], '')
        self.assertEqual(notice['row_type'], 'folder_notice')
        self.assertAlmostEqual(sum(float(r['distance_miles'] or 0) for r in rows if r['row_type'] != 'total'),
                               sum(t['distance_miles'] for t in trips))
        self.assertEqual(float(next(r for r in rows if r['row_type'] == 'total' and r['category'] == 'all')['distance_miles']),
                         figures['totals']['all'])
        book = load_workbook(io.BytesIO(self.client.get('/export.xlsx?year=2026').data))
        try:
            header, row = list(book['Unreadable trips'].values)
            values = dict(zip(header, row, strict=True))
            self.assertIsNone(values['distance_miles'])
            self.assertIn('not in the totals shown', values['purpose_note'])
            self.assertEqual(book['Mileage'].max_row, 3)
            self.assertEqual(list(book['Totals'].values)[-1][-1], figures['totals']['all'])
        finally:
            book.close()
        self.assertEqual(self.undo_unused(key).status_code, 302)
        self.assertEqual(self.close_year().status_code, 302)

    def test_moved_real_month_restored_by_renaming_allows_close_with_saved_purpose(self):
        key, saved = self.moved_merge()
        self.assert_unused_close_refused(key)
        (self.data / '2026-05').rename(self.month)
        self.assertEqual(self.close_year().status_code, 302)
        with closing(sqlite3.connect(self.db)) as conn:
            snapshot = json.loads(conn.execute('SELECT snapshot FROM tax_years').fetchone()[0])
        self.assertEqual(snapshot['trip_keys'], [key])
        self.assertEqual(snapshot['totals']['business'], round(saved['distance_miles'], 3))

    def test_unused_undo_names_annotations_and_keeps_full_summary_before_deletion(self):
        key, _ = self.moved_merge()
        effect = ('The trips stay counted under 2026-05/ with only the annotations saved under that name. '
                  'Annotations saved under 2026-04/ are kept in the database but not applied. '
                  "The merged trip's saved miles and purpose leave the totals and are kept in history and the year review.")
        page = html.unescape(self.client.get('/').get_data(as_text=True))
        self.assertIn('Remove this unused merge and its merged annotation. ' + effect, page)
        refusal = self.undo_unused(key, confirm='no')
        self.assertEqual(refusal.status_code, 400)
        self.assertIn('Confirm removing this unused merge and its merged annotation. ' + effect,
                      html.unescape(refusal.get_data(as_text=True)))
        with closing(sqlite3.connect(self.db)) as conn:
            summary = conn.execute('SELECT summary FROM trip_merges').fetchone()[0]
        original_record = self.viewer.trip_log.record
        def record(conn, record_key, action, details, **actor):
            self.assertTrue(conn.in_transaction)
            self.assertEqual(conn.execute('SELECT summary FROM trip_merges WHERE trip_key=?', (key,)).fetchone()[0], summary)
            return original_record(conn, record_key, action, details, **actor)
        with patch.object(self.viewer.trip_log, 'record', side_effect=record):
            self.assertEqual(self.undo_unused(key).status_code, 302)
        trips, _ = self.viewer.load_trips()
        self.assertEqual({t['trip_key']: t['category'] for t in trips}, {'2026-05/a': '', '2026-05/b': ''})
        with closing(sqlite3.connect(self.db)) as conn:
            details = json.loads(conn.execute("SELECT details FROM trip_history WHERE action='split'").fetchone()[0])
            self.assertEqual(details['saved_summary_raw'], summary)
            self.assertEqual(details['saved_summary'], json.loads(summary))
            self.assertEqual(details['dropped']['purpose_note'], 'merged note')
            self.assertEqual(details['original_annotations']['2026-04/a']['category'], 'personal')
            self.assertEqual(details['undo_effect'], effect)
        review = html.unescape(self.client.get('/admin/year/2026').get_data(as_text=True))
        self.assertIn('Unused-name merge undone. Saved miles: 0.732 mi; purpose: business; purpose note: merged note;', review)
        self.assertIn('Now counted again: 2026-04/a as 2026-05/a, 2026-04/b as 2026-05/b; do not add these saved miles by hand.', review)
        with closing(sqlite3.connect(self.db)) as conn:
            row = self.viewer._merge_history_rows(conn, 2026, trips)[0]
        self.assert_history_exports(self.viewer.merge_history_message(row), 'unused-name merge undone')
        history = html.unescape(self.client.get('/admin/history').get_data(as_text=True))
        self.assertIn('Unused-name merge undone. ' + effect, history)
        self.assertEqual(self.close_year().status_code, 302)
        self.assertIn('Unused-name merge undone.', self.client.get('/admin/year/2026').get_data(as_text=True))

    def test_unused_undo_history_rolls_back_if_deletion_fails(self):
        key, _ = self.moved_merge()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("CREATE TRIGGER refuse_delete BEFORE DELETE ON trip_merges BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END")
        self.viewer.app.config['TESTING'] = True
        with self.assertRaises(sqlite3.IntegrityError):
            self.undo_unused(key)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM trip_merges').fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM trip_history WHERE action='split'").fetchone()[0], 0)

    def test_unused_merge_uses_saved_summary_year_and_unknown_year_blocks_every_close(self):
        key, _ = self.moved_merge()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            raw = conn.execute('SELECT summary FROM trip_merges').fetchone()[0]
            summary = json.loads(raw)
            summary['start_time'] = '2025-12-31T12:00:00Z'
            conn.execute('UPDATE trip_merges SET summary=?', (json.dumps(summary),))
        self.assert_unused_close_refused(key, 2025)
        self.assertIn(2025, [r['year'] for r in self.viewer._year_list()])
        self.assertEqual(self.close_year(2024).status_code, 302)
        self.assertNotIn('unused-merges-heading', self.client.get('/admin/year/2026').get_data(as_text=True))
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('UPDATE trip_merges SET summary=?', (json.dumps({'distance_miles': .732}),))
        for year in (2023, 2025, 2026):
            self.assert_unused_close_refused(key, year)
        # Unknown saved dates also cannot bypass an existing year lock.
        self.assertEqual(self.undo_unused(key).status_code, 409)
        self.assertEqual(self.client.post('/admin/year/2024/reopen', data={
            'csrf_token': 'test-csrf', 'confirm': 'yes'}).status_code, 302)

    def test_unused_merge_after_close_shows_saved_current_comparison_and_can_reopen(self):
        a, b = self.trip('a', 0), self.trip('b', 180, .01)
        key = self.merged_key(self.merge(a, b))
        self.annotate(key, 'business', 'merged note')
        self.assertEqual(self.close_year().status_code, 302)
        with closing(sqlite3.connect(self.db)) as conn:
            snapshot = conn.execute('SELECT snapshot FROM tax_years').fetchone()[0]
        self.month.rename(self.data / '2026-05')
        review = html.unescape(self.client.get('/admin/year/2026').get_data(as_text=True))
        self.assertIn('The totals now differ from the ones saved at the close.', review)
        self.assertIn('Saved merges under unused month names (1)', review)
        self.assertIn('No longer counted: ' + key, review)
        self.assertIn('Now counted:', review)
        self.assertIn('Reopen 2026', review)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT snapshot FROM tax_years').fetchone()[0], snapshot)
        self.assertEqual(self.client.post('/admin/year/2026/reopen', data={
            'csrf_token': 'test-csrf', 'confirm': 'yes'}).status_code, 302)
        self.assert_unused_close_refused(key)

    def test_admin_can_undo_unused_merge_with_or_without_original_link(self):
        key = self.unused_merge()
        files = {p: p.read_bytes() for p in (self.data / "archive").iterdir()}
        # A forged ordinary merge cannot be undone through this exceptional route.
        self.assertEqual(self.undo_unused("2026-03/merge-absent").status_code, 409)
        self.assertEqual(self.undo_unused(key, csrf_token="bad").status_code, 400)
        self.assertEqual(self.undo_unused(key, confirm="no").status_code, 400)
        # Test refusal with a closed year while the link exists, then successful
        # undo without that link after reopening. Neither path resolves aliases.
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO tax_years VALUES (2026,'driver','',?)", (json.dumps({"trip_keys": [key]}),))
        self.assertEqual(self.undo_unused(key).status_code, 409)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("DELETE FROM tax_years")
        (self.data / "2026-04").unlink()
        self.assertEqual(self.undo_unused(key).status_code, 302)
        self.assertEqual(self.undo_unused(key).status_code, 409)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM trip_merges").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM trip_merge_parts").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT purpose_note FROM annotations WHERE trip_key='2026-04/a'").fetchone()[0],
                             "original note")
            history = json.loads(conn.execute("SELECT details FROM trip_history WHERE action='split'").fetchone()[0])
            self.assertEqual(history["dropped"]["purpose_note"], "merged note")
        self.assertEqual({p: p.read_bytes() for p in files}, files)
        self.assertEqual(self.close_year().status_code, 302)

    def test_undo_unused_merge_rechecks_selection_before_mutating(self):
        key = self.unused_merge()
        (self.data / "2026-03").unlink()  # April becomes the name in use again.
        self.assertEqual(self.undo_unused(key).status_code, 409)
        self.assertEqual(self.viewer.load_trips()[0][0]["trip_key"], key)

    def test_admin_undo_uses_saved_key_while_it_is_an_alias(self):
        key = self.unused_merge()
        self.assertEqual(self.split(key).status_code, 409)  # Ordinary alias writes stay refused.
        self.assertEqual(self.undo_unused(key).status_code, 302)
        self.assertFalse(any(e.get("unused_merge") for e in self.viewer.load_trips()[1]))

    def test_unused_merge_snapshot_lock_survives_missing_link_and_different_folder_year(self):
        key = self.unused_merge()
        (self.data / "2026-04").unlink()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO tax_years VALUES (2025,'driver','',?)", (json.dumps({"trip_keys": [key]}),))
        self.assertEqual(self.undo_unused(key).status_code, 409)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM trip_merges").fetchone()[0], 1)

    def test_empty_active_folder_is_damage_and_mixed_active_parts_are_not_unused(self):
        key = self.unused_merge()
        (self.data / "2026-04").unlink()
        self.month.mkdir()  # An actual month in use, even though its files are gone.
        trips, entries = self.viewer.load_trips()
        broken = next(e for e in entries if e["trip_key"] == key)
        self.assertNotIn("unused_merge", broken)
        self.assertIn("A merge part is missing or unreadable", broken["problem"])
        self.assertEqual(self.undo_unused(key).status_code, 409)
        # One inactive part never hides damage under an active name.
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE trip_merge_parts SET part_key='2026-05/b' WHERE part_key='2026-04/b'")
        self.assertFalse(any(e.get("unused_merge") for e in self.viewer.load_trips()[1]))

    def test_genuinely_missing_part_under_active_name_still_blocks_suggestions(self):
        a, b = self.trip("a", 0), self.trip("b", 180, .01)
        key = self.merged_key(self.merge(a, b))
        c = self.trip("c", 360, .02)
        self.trip("d", 540, .03)
        (self.month / "a.json").unlink()
        trips, entries = self.viewer.load_trips()
        broken = next(t for t in entries if t["trip_key"] == key)
        self.assertNotIn("unused_merge", broken)
        self.assertEqual(broken["problem"], self.viewer.trip_merges.DAMAGED_MERGE_PROBLEM)
        first = next(t for t in trips if t["trip_key"] == c)
        candidate, reason = self.viewer.trip_merges.next_trip(first, trips, entries)
        self.assertIsNone(candidate)
        self.assertIn("unknown device", reason)
        self.assertEqual(self.viewer.year_figures(trips, entries)["unreadable"], 1)
        self.assertEqual(self.undo_unused(key).status_code, 409)
        self.assertEqual(self.split(key).status_code, 409)

    def assert_damaged_saved_merge(self, key, first_key, missing=1):
        trips, entries = self.viewer.load_trips()
        broken = next(t for t in entries if t['trip_key'] == key)
        self.assertNotIn('unused_merge', broken)
        self.assertEqual(broken['problem'], self.viewer.trip_merges.DAMAGED_MERGE_PROBLEM)
        page = self.client.get('/').get_data(as_text=True)
        self.assertIn(broken['problem'], page)
        self.assertNotIn('Undo saved merge', page)
        self.assertNotIn('>Undo merge (split back)</button>', page)
        self.assertIn('>Discard damaged merge</button>', page)
        from test_viewer_roles import ViewerRolesTests
        if self.viewer.users.get_user(self.db, 'helper') is None:
            ViewerRolesTests.seed_editor(self)
        ViewerRolesTests.signed_in(self, 'editor')
        editor_page = self.client.get('/').get_data(as_text=True)
        self.assertNotIn('>Undo merge (split back)</button>', editor_page)
        self.assertNotIn('>Discard damaged merge</button>', editor_page)
        self.authenticate(self.client)
        self.assertEqual(self.split(key).status_code, 409)
        first = next(t for t in trips if t['trip_key'] == first_key)
        candidate, reason = self.viewer.trip_merges.next_trip(first, trips, entries)
        self.assertIsNone(candidate)
        self.assertIn('unknown device', reason)
        self.assertEqual(self.viewer.year_figures(*self.viewer.year_members(trips, entries, 2026))['unreadable'], missing)
        self.assertIn(broken['problem'], self.client.get('/admin/year/2026').get_data(as_text=True))
        self.assertEqual(self.undo_unused(key).status_code, 409)
        self.assertEqual(self.client.post('/admin/year/2026/close', data={
            'csrf_token': 'test-csrf', 'confirm': 'yes'}).status_code, 302)
        with closing(sqlite3.connect(self.db)) as conn:
            snapshot = json.loads(conn.execute('SELECT snapshot FROM tax_years').fetchone()[0])
            self.assertEqual(snapshot['unreadable'], missing)
            self.assertEqual(conn.execute('SELECT count(*) FROM trip_merges').fetchone()[0], 1)

    def test_deleted_real_month_remains_damage_in_list_close_suggestions_and_undo(self):
        a, b = self.trip('a', 0), self.trip('b', 180, .01)
        key = self.merged_key(self.merge(a, b))
        self.trip('c', 360, .02)
        self.trip('d', 540, .03)
        other = self.data / '2026-05'
        other.mkdir()
        for name in ('c', 'd'):
            for suffix in ('.json', '.gpx'):
                (self.month / (name + suffix)).rename(other / (name + suffix))
        shutil.rmtree(self.month)
        self.assert_damaged_saved_merge(key, '2026-05/c')

    def test_unused_name_requires_every_part_and_saved_source_stamps(self):
        key = self.unused_merge()
        self.trip('c', 360, .02)
        self.trip('d', 540, .03)
        archive = self.data / 'archive'
        (self.data / '2026-04').unlink()
        # One of two parts is missing under the counted name: no exceptional undo.
        original = (archive / 'b.json').read_bytes()
        saved_stat = (archive / 'b.json').stat()
        (archive / 'b.json').unlink()
        self.assert_damaged_saved_merge(key, '2026-03/c', missing=2)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('DELETE FROM tax_years')
        (archive / 'b.json').write_bytes(original)
        os.utime(archive / 'b.json', ns=(saved_stat.st_atime_ns, saved_stat.st_mtime_ns))
        self.assertTrue(any(t.get('unused_merge') for t in self.viewer.load_trips()[1]))
        # Same IDs in readable files are insufficient when either source changed.
        with (archive / 'b.gpx').open('ab') as stream:
            stream.write(b' ')
        self.assert_damaged_saved_merge(key, '2026-03/c')

    def test_undo_unused_merge_rechecks_replacement_parts_under_write_lock(self):
        key = self.unused_merge()
        self.assertIn('Undo saved merge', self.client.get('/').get_data(as_text=True))
        (self.data / 'archive/b.gpx').unlink()
        original = self.viewer._load_trips
        def check_transaction(conn, **kwargs):
            self.assertTrue(conn.in_transaction)
            return original(conn, **kwargs)
        with patch.object(self.viewer, '_load_trips', side_effect=check_transaction):
            self.assertEqual(self.undo_unused(key).status_code, 409)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM trip_merges').fetchone()[0], 1)

    def test_merge_during_saved_name_conflict_stays_under_chosen_name(self):
        old = self.unused_merge()
        new = self.merged_key(self.merge('2026-03/a', '2026-03/b'))
        self.assertTrue(new.startswith('2026-03/'))
        self.annotate(new, 'business', 'new merged note')
        trips, entries = self.viewer.load_trips()
        self.assertEqual([t['trip_key'] for t in trips], [new])
        self.assertTrue(any(t['trip_key'] == old and t.get('unused_merge') for t in entries))
        distance = trips[0]['distance_miles']
        (self.data / '2026-03').unlink()  # Resolve in favour of the original April name.
        trips, entries = self.viewer.load_trips()
        self.assertEqual([t['trip_key'] for t in trips], [old])
        self.assertEqual(trips[0]['purpose_note'], 'merged note')
        self.assertEqual(trips[0]['distance_miles'], distance)
        self.assertTrue(any(t['trip_key'] == new and t.get('unused_merge') for t in entries))
        self.assertEqual(self.viewer.year_figures(trips, entries)['unreadable'], 0)
        self.symlink(self.data / '2026-03', self.data / 'archive', directory=True)
        trips, entries = self.viewer.load_trips()
        self.assertEqual([t['trip_key'] for t in trips], [new])
        self.assertEqual(trips[0]['purpose_note'], 'new merged note')
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM trip_merges').fetchone()[0], 2)

    def test_removed_month_requires_one_unambiguous_replacement_folder(self):
        key = self.unused_merge()
        (self.data / '2026-04').unlink()
        # Matching stamps in two different counted folders cannot establish
        # which one is the recording the stored merge refers to.
        shutil.copytree(self.data / 'archive', self.data / '2026-05')
        entries = self.viewer.load_trips()[1]
        broken = next(t for t in entries if t['trip_key'] == key)
        self.assertNotIn('unused_merge', broken)
        self.assertEqual(self.undo_unused(key).status_code, 409)
        # A malformed saved stamp is not evidence either, even with matching IDs.
        for value in ({'invalid': 0}, 42, None):
            with closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute('UPDATE trip_merges SET fingerprint=? WHERE trip_key=?', (json.dumps(value), key))
            broken = next(t for t in self.viewer.load_trips()[1] if t['trip_key'] == key)
            self.assertNotIn('unused_merge', broken)

    def test_matching_parts_split_between_folders_are_damage(self):
        key = self.unused_merge()
        (self.data / '2026-04').unlink()
        other = self.data / '2026-05'
        other.mkdir()
        for suffix in ('.json', '.gpx'):
            (self.data / ('archive/b' + suffix)).rename(other / ('b' + suffix))
        entries = self.viewer.load_trips()[1]
        broken = next(t for t in entries if t['trip_key'] == key)
        self.assertNotIn('unused_merge', broken)
        self.assertEqual(self.undo_unused(key).status_code, 409)

    @unittest.skipIf(os.name == 'nt' or getattr(os, 'geteuid', lambda: 0)() == 0, 'requires non-root POSIX permissions')
    def test_unreadable_replacement_track_is_damage_even_with_matching_stamps(self):
        key = self.unused_merge()
        (self.data / '2026-04').unlink()
        source = self.data / 'archive/b.gpx'
        self.addCleanup(source.chmod, 0o600)
        source.chmod(0o000)
        entries = self.viewer.load_trips()[1]
        self.assertNotIn('unused_merge', next(t for t in entries if t['trip_key'] == key))
        self.assertEqual(self.undo_unused(key).status_code, 409)

    def test_nonmonth_link_upload_notice_does_not_block_merge(self):
        a, b = self.trip("a", 0), self.trip("b", 180, .01)
        archive = self.data / "archive"
        self.month.rename(archive)
        try:
            self.month.symlink_to(archive, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        trips, notices = self.viewer.load_trips()
        self.assertEqual([t["folder_reason"] for t in notices], ["ingest_refused"])
        first = next(t for t in trips if t["trip_key"] == a)
        candidate, reason = self.viewer.trip_merges.next_trip(first, trips, notices)
        self.assertEqual(candidate["trip_key"] if candidate else None, b, reason)
        self.assertEqual(self.merge(a, b).status_code, 302)

    def test_plain_file_month_is_a_notice_and_does_not_block_healthy_merge(self):
        a, b = self.trip('a', 0), self.trip('b', 180, .01)
        (self.data / '2025-02').write_text('synthetic non-folder', encoding='utf-8')
        trips, entries = self.viewer.load_trips()
        self.assertEqual(entries[0]['folder_reason'], 'not_folder')
        self.assertFalse(self.viewer.missing_mileage(entries[0]))
        figures = self.viewer.year_figures(*self.viewer.year_members(trips, entries, 2025))
        self.assertEqual(figures['unreadable'], 0)
        first = next(t for t in trips if t['trip_key'] == a)
        candidate, reason = self.viewer.trip_merges.next_trip(first, trips, entries)
        self.assertEqual(candidate['trip_key'] if candidate else None, b, reason)
        self.assertEqual(self.merge(a, b).status_code, 302)
        self.assertEqual(self.client.post('/admin/year/2025/close', data={
            'csrf_token': 'test-csrf', 'confirm': 'yes'}).status_code, 302)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(json.loads(conn.execute('SELECT snapshot FROM tax_years').fetchone()[0])['unreadable'], 0)

    @unittest.skipIf(os.name == 'nt' or getattr(os, 'geteuid', lambda: 0)() == 0, 'requires non-root POSIX permissions')
    def test_unwritable_month_notice_does_not_block_merge(self):
        a, b = self.trip('a', 0), self.trip('b', 180, .01)
        self.addCleanup(self.month.chmod, 0o700)
        self.month.chmod(0o500)
        trips, entries = self.viewer.load_trips()
        self.assertEqual([entry['folder_reason'] for entry in entries], ['unwritable'])
        first = next(t for t in trips if t['trip_key'] == a)
        candidate, reason = self.viewer.trip_merges.next_trip(first, trips, entries)
        self.assertEqual(candidate['trip_key'] if candidate else None, b, reason)
        self.assertEqual(self.merge(a, b).status_code, 302)

    def test_duplicate_month_notice_does_not_block_merge(self):
        a, b = self.trip("a", 0), self.trip("b", 180, .01)
        try:
            (self.data / "2026-05").symlink_to(self.month, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        trips, unreadable = self.viewer.load_trips()
        first = next(t for t in trips if t["trip_key"] == a)
        candidate, reason = self.viewer.trip_merges.next_trip(first, trips, unreadable)
        self.assertEqual(candidate["trip_key"] if candidate else None, b, reason)
        self.assertEqual(unreadable[0]["folder_reason"], "duplicate")
        self.assertEqual(self.viewer.year_figures(trips, unreadable)["unreadable"], 0)
        # A notice remains harmless even if its display text changes.
        unreadable[0]["problem"] = "display text only"
        self.assertEqual(self.viewer.trip_merges.next_trip(first, trips, unreadable)[0]["trip_key"], b)
        response = self.client.post("/admin/year/2026/close", data={"csrf_token": "test-csrf", "confirm": "yes"})
        self.assertEqual(response.status_code, 302)
        with closing(sqlite3.connect(self.db)) as conn:
            saved = json.loads(conn.execute("SELECT snapshot FROM tax_years WHERE year=2026").fetchone()[0])
        self.assertEqual(saved["unreadable"], 0)

    def test_escaping_merge_source_symlinks_make_the_merge_unreadable(self):
        a, b = self.trip("a", 0), self.trip("b", 180, .01)
        key = self.merged_key(self.merge(a, b))
        for extension in ("json", "gpx"):
            with self.subTest(extension=extension):
                source = self.month / f"b.{extension}"
                raw = source.read_bytes()
                target = self.root / f"private.{extension}"
                target.write_bytes(raw)
                source.unlink()
                try:
                    source.symlink_to(target)
                except (OSError, NotImplementedError) as exc:
                    self.skipTest(f"symlink creation unavailable: {type(exc).__name__}")
                try:
                    with self.assertRaises(ValueError):
                        list(self.viewer.trip_merges.source_paths(self.data, [b]))
                    with self.assertRaises(ValueError):
                        self.viewer.trip_merges.fingerprint(self.data, [b])
                    with patch.object(self.viewer.trip_merges, "combined_summary",
                                      side_effect=AssertionError("unsafe merge rebuilt")):
                        trips, unreadable = self.viewer.load_trips()
                        self.assertEqual(trips, [])
                        self.assertEqual([t["trip_key"] for t in unreadable], [key])
                        self.assertIn("A merge part is missing or unreadable", unreadable[0]["problem"])
                        self.assertTrue(unreadable[0]["has_gpx"])
                        for route, status in (("/", 200), (f"/trip/{key}", 404),
                                              ("/export.csv", 200), ("/export.xlsx", 200)):
                            response = self.client.get(route)
                            self.assertEqual(response.status_code, status)
                            if route == "/":
                                self.assertNotIn("(no GPX either)", response.get_data(as_text=True))
                            response.close()
                finally:
                    source.unlink()
                    source.write_bytes(raw)

    def test_missing_merge_summary_does_not_claim_gpx_is_missing(self):
        a, b = self.trip("a", 0), self.trip("b", 180, .01)
        key = self.merged_key(self.merge(a, b))
        (self.month / "b.json").unlink()
        trips, unreadable = self.viewer.load_trips()
        self.assertEqual(trips, [])
        self.assertEqual([t["trip_key"] for t in unreadable], [key])
        self.assertTrue(unreadable[0]["has_gpx"])
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("A merge part is missing or unreadable", response.get_data(as_text=True))
        self.assertNotIn("(no GPX either)", response.get_data(as_text=True))
        response.close()

    def test_trip_uploaded_after_its_neighbours_were_merged_is_flagged_not_silent(self):
        first, last = self.trip("a", 0), self.trip("c", 200)
        merged = self.merged_key(self.merge(first, last))
        self.assertEqual(self.client.get("/?year=2026").get_data(as_text=True).count("Inside a merged trip"), 0)
        late = self.trip("b", 90)  # recorded between the two, uploaded after the merge
        other_device = self.trip("other", 90, offset=.01, device="another-device")
        after = self.trip("after", 5000)  # a separate drive later
        page = self.client.get("/?year=2026").get_data(as_text=True)
        self.assertEqual(page.count("Inside a merged trip"), 1)
        self.assertIn(f'href="/trip/{merged}?year=2026"', page)
        self.assertIn("counted twice: undo that merge", page)
        # Only the overlapping same-device recording is flagged.
        trips, _ = self.viewer.load_trips()
        flagged = {t["trip_key"]: t for t in trips}
        self.viewer._flag_trips_inside_merges(trips)
        self.assertEqual({t["trip_key"] for t in trips if t.get("inside_merge")}, {late})
        self.assertNotIn("inside_merge", flagged[other_device])
        self.assertNotIn("inside_merge", flagged[after])
        # The year review lists it too, with a pointer to the merged trip.
        review = self.client.get("/admin/year/2026").get_data(as_text=True)
        section = review[review.index('id="inside-merge-heading"'):]
        section = section[:section.index("</section>")]
        self.assertIn("Trips inside a merged trip (1)", section)
        self.assertIn(f'href="/trip/{merged}?year=2026"', section)
        # Undoing the merge clears the flag.
        self.assertEqual(self.split(merged).status_code, 302)
        self.assertNotIn("Inside a merged trip", self.client.get("/?year=2026").get_data(as_text=True))
        self.assertIn("Trips inside a merged trip (0)", self.client.get("/admin/year/2026").get_data(as_text=True))

    def damage_summary(self, key, **fields):
        path = self.data / (key + ".json")
        summary = json.loads(path.read_text(encoding="utf-8"))
        summary.update(distance_miles="damaged", **fields)
        path.write_text(json.dumps(summary), encoding="utf-8")

    def assert_export_sources(self, row_type, sheet, trip_id, files):
        rows = list(csv.DictReader(io.StringIO(self.client.get("/export.csv").data.decode())))
        row = next(r for r in rows if r["row_type"] == row_type)
        self.assertEqual(row["trip_id"], trip_id)
        self.assertEqual(row["gpx_file"], files)
        book = load_workbook(io.BytesIO(self.client.get("/export.xlsx").data))
        try:
            headers, values = list(book[sheet].values)[:2]
            row = dict(zip(headers, values, strict=True))
            self.assertEqual(row["trip_id"], trip_id)
            self.assertEqual(row["gpx_file"], files)
        finally:
            book.close()

    def test_merge_recomputes_summary_map_exports_totals_cache_and_undo(self):
        a = self.trip("a", 0)
        b = self.trip("b", 180, .01)
        self.annotate(b, "business", "Test purpose", "Test destination")
        original = {p.name: p.read_bytes() for p in self.month.iterdir()}
        parts, _ = self.viewer.load_trips()
        separate = sum(t["distance_miles"] for t in parts)
        self.assertIn("Merge with next trip", self.client.get(f"/trip/{a}").get_data(as_text=True))
        key = self.merged_key(self.merge(a, b))
        trips, unreadable = self.viewer.load_trips()
        self.assertEqual(len(trips), 1)
        self.assertFalse(unreadable)
        t = trips[0]
        from common.trip_summary import parse_gpx, summarize
        parsed = [parse_gpx((self.data / (k + ".gpx")).read_bytes()) for k in (a, b)]
        expected = summarize({"segments": [seg for p in parsed for seg in p["segments"]]})
        self.assertEqual(t["distance_miles"], expected["distance_miles"])
        self.assertGreater(t["distance_miles"], separate)
        self.assertEqual(t["duration_seconds"], 210)
        self.assertEqual(t["start_time"], self.base.isoformat())
        self.assertEqual(t["end_time"], (self.base + timedelta(seconds=210)).isoformat())
        self.assertEqual(t["purpose_note"], "Test purpose")
        self.assertEqual(t["trip_id"], "a")
        self.assertEqual(t["gpx_file"], f"{a}.gpx; {b}.gpx")
        for old in (a, b):
            r = self.client.get(f"/trip/{old}")
            self.assertEqual(r.status_code, 302)
            self.assertIn(key, r.headers["Location"])
            self.assertEqual(self.annotate(old, note="stale edit").status_code, 409)
        page = self.client.get(f"/trip/{key}").get_data(as_text=True)
        self.assertIn("Merged from 2 recordings", page)
        self.assertIn("<title>Trip a</title>", page)
        self.assertIn(f"{a}.gpx; {b}.gpx", page)
        self.assertIn(f"{a}.gpx; {b}.gpx", self.client.get("/").get_data(as_text=True))
        self.assertNotIn(key + ".gpx", page)
        self.assert_export_sources("trip", "Mileage", "a", f"{a}.gpx; {b}.gpx")
        self.assertIn("joinTrackSegments: false", page)
        raw = self.client.get(f"/gpx/{key}").data
        self.assertEqual(len(ET.fromstring(raw).findall("./trk/trkseg")), 2)
        self.assertEqual(len(ET.fromstring(self.client.get(f"/gpx/{b}").data).findall("./trk/trkseg")), 2)
        endpoints = self.viewer.track_endpoints(t)
        self.assertEqual(endpoints["start_longitude"], 0)
        self.assertAlmostEqual(endpoints["end_longitude"], .0106)
        rows = list(csv.DictReader(io.StringIO(self.client.get("/export.csv?year=2026").data.decode())))
        self.assertEqual(len([r for r in rows if r["row_type"] == "trip"]), 1)
        self.assertEqual(float(next(r for r in rows if r["row_type"] == "total" and r["category"] == "all")["distance_miles"]), t["distance_miles"])
        book = load_workbook(io.BytesIO(self.client.get("/export.xlsx?year=2026").data))
        self.addCleanup(book.close)
        self.assertEqual(book["Mileage"].max_row, 2)
        totals = {row[2]: row[3] for row in book["Totals"].iter_rows(min_row=2, values_only=True)}
        self.assertEqual(totals["business"], t["distance_miles"])
        with patch.object(self.viewer.trip_merges, "combined_summary", side_effect=AssertionError("uncached")):
            self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.annotate(key, "personal", "Merged edit").status_code, 302)
        self.assertEqual(self.split(key).status_code, 302)
        trips, _ = self.viewer.load_trips()
        by_key = {t["trip_key"]: t for t in trips}
        self.assertEqual(set(by_key), {a, b})
        self.assertEqual(by_key[a]["purpose_note"], "")
        self.assertEqual(by_key[b]["purpose_note"], "Test purpose")
        self.assertEqual(by_key[b]["destination"], "Test destination")
        self.assertEqual(original, {p.name: p.read_bytes() for p in self.month.iterdir()})

    def test_new_merge_stores_the_source_fingerprint_so_the_next_load_reuses_it(self):
        a, b = self.trip("a", 0), self.trip("b", 180, .01)
        key = self.merged_key(self.merge(a, b))
        with closing(sqlite3.connect(self.db)) as conn:
            fingerprint, updated_at = conn.execute(
                "SELECT fingerprint, updated_at FROM trip_merges WHERE trip_key=?", (key,)).fetchone()
        self.assertEqual(fingerprint, self.viewer.trip_merges.fingerprint(self.data, [a, b]))
        self.assertNotEqual(fingerprint, updated_at)
        with patch.object(self.viewer.trip_merges, "combined_summary", side_effect=AssertionError("rebuilt")):
            self.assertEqual(self.client.get("/").status_code, 200)
            self.assertEqual(len(self.viewer.load_trips()[0]), 1)

    def test_a_rules_version_bump_does_not_rebuild_merged_summaries_alone(self):
        a, b = self.trip("a", 0), self.trip("b", 180, .01)
        key = self.merged_key(self.merge(a, b))
        # Pretend this merged summary was built under earlier distance rules,
        # like the plain trips' stored summaries.
        with closing(sqlite3.connect(self.db)) as conn, conn:
            summary, fingerprint = conn.execute(
                "SELECT summary, fingerprint FROM trip_merges WHERE trip_key=?", (key,)).fetchone()
            conn.execute("UPDATE trip_merges SET summary=? WHERE trip_key=?",
                         (json.dumps({**json.loads(summary), "distance_miles": 123.0}), key))
        with patch.object(self.viewer.trip_merges, "SUMMARY_VERSION", 4, create=True), patch.object(
                self.viewer.trip_merges, "combined_summary", side_effect=AssertionError("rebuilt")):
            self.assertEqual(self.viewer.load_trips()[0][0]["distance_miles"], 123.0)
            # A row saved before the version left the fingerprint is not rebuilt either.
            with closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute("UPDATE trip_merges SET fingerprint=? WHERE trip_key=?",
                             (json.dumps([3, *json.loads(fingerprint)]), key))
            self.assertEqual(self.viewer.load_trips()[0][0]["distance_miles"], 123.0)
        # A changed source file still rebuilds it, under the current rules.
        path = self.month / "a.gpx"
        path.write_bytes(path.read_bytes() + b" ")
        rebuilt = self.viewer.load_trips()[0][0]["distance_miles"]
        self.assertNotEqual(rebuilt, 123.0)

    def stored_merge(self, key):
        with closing(sqlite3.connect(self.db)) as conn:
            return conn.execute("SELECT summary, fingerprint FROM trip_merges WHERE trip_key=?", (key,)).fetchone()

    def test_stamp_has_no_inode_so_a_restored_copy_keeps_its_merged_summary(self):
        a, b = self.trip("a", 0), self.trip("b", 180, .01)
        key = self.merged_key(self.merge(a, b))
        # A summary built under earlier rules must survive a move to a new disk.
        with closing(sqlite3.connect(self.db)) as conn, conn:
            summary, _ = self.stored_merge(key)
            conn.execute("UPDATE trip_merges SET summary=? WHERE trip_key=?",
                         (json.dumps({**json.loads(summary), "distance_miles": 123.0}), key))
        stored = self.stored_merge(key)
        self.assertTrue(all(len(entry) == 3 for entry in json.loads(stored[1])))
        copy = self.root / "restored"
        shutil.copytree(self.data, copy, copy_function=shutil.copy2)
        self.assertNotEqual((self.month / "a.gpx").stat().st_ino, (copy / "2026-04" / "a.gpx").stat().st_ino)
        with patch.object(self.viewer, "DATA_ROOT", copy), patch.object(
                self.viewer.trip_merges, "combined_summary", side_effect=AssertionError("rebuilt")):
            trips, unreadable = self.viewer.load_trips()
        self.assertFalse(unreadable)
        self.assertEqual(trips[0]["distance_miles"], 123.0)
        self.assertEqual(self.stored_merge(key), stored)

    def test_four_field_stamp_from_an_older_release_is_rewritten_once_without_a_rebuild(self):
        a, b = self.trip("a", 0), self.trip("b", 180, .01)
        key = self.merged_key(self.merge(a, b))
        summary, fresh = self.stored_merge(key)
        # Build the v1.06-v1.15 shape (name, mtime, size, inode) from the first
        # three fields, so the fixture does not depend on the code under test.
        old = json.dumps([[*entry[:3], 987654 + i] for i, entry in enumerate(json.loads(fresh))])
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE trip_merges SET fingerprint=? WHERE trip_key=?", (old, key))
            # Count later writes to the row.
            conn.execute("CREATE TABLE writes (n INTEGER)")
            conn.execute("CREATE TRIGGER count_writes AFTER UPDATE ON trip_merges BEGIN INSERT INTO writes VALUES (1); END")

        def writes():
            with closing(sqlite3.connect(self.db)) as conn:
                return conn.execute("SELECT COUNT(*) FROM writes").fetchone()[0]
        with patch.object(self.viewer.trip_merges, "combined_summary", side_effect=AssertionError("rebuilt")):
            self.viewer.load_trips()
            self.assertEqual(self.stored_merge(key), (summary, fresh))
            self.assertEqual(writes(), 1)
            self.viewer.load_trips()
            self.assertEqual(writes(), 1)

    def test_changed_size_or_time_still_rebuilds_a_merged_trip(self):
        a, b = self.trip("a", 0), self.trip("b", 180, .01)
        key = self.merged_key(self.merge(a, b))
        helper = self.viewer.trip_merges
        path = self.month / "a.json"
        with patch.object(helper, "combined_summary", wraps=helper.combined_summary) as compute:
            self.viewer.load_trips()
            self.assertEqual(compute.call_count, 0)
            # Same size, later modification time.
            st = path.stat()
            os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))
            self.viewer.load_trips()
            self.assertEqual(compute.call_count, 1)
            # Different size, same modification time as the last load saw.
            st = path.stat()
            path.write_bytes(path.read_bytes() + b" ")
            os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
            self.viewer.load_trips()
            self.assertEqual(compute.call_count, 2)
            self.viewer.load_trips()
            self.assertEqual(compute.call_count, 2)
        self.assertEqual(json.loads(self.stored_merge(key)[1]), json.loads(helper.fingerprint(self.data, [a, b])))

    def test_same_size_same_time_replacement_no_longer_rebuilds_a_merged_trip(self):
        a, b = self.trip("a", 0), self.trip("b", 180, .01)
        self.merged_key(self.merge(a, b))
        path = self.month / "a.gpx"
        st = path.stat()
        replacement = path.with_name("a.new")
        replacement.write_bytes(path.read_bytes())
        os.utime(replacement, ns=(st.st_atime_ns, st.st_mtime_ns))
        os.replace(replacement, path)
        with patch.object(self.viewer.trip_merges, "combined_summary", side_effect=AssertionError("rebuilt")):
            self.viewer.load_trips()

    def test_chain_and_old_merged_link_then_split_to_all_originals(self):
        a, b, c = self.trip("a", 0), self.trip("b", 120, .001), self.trip("c", 240, .002)
        self.annotate(a, "business", "Original A")
        ab = self.merged_key(self.merge(a, b))
        self.annotate(ab, "personal", "Merged AB")
        abc = self.merged_key(self.merge(ab, c))
        trips, _ = self.viewer.load_trips()
        self.assertEqual(trips[0]["parts"], [a, b, c])
        self.assertEqual(trips[0]["purpose_note"], "Merged AB")
        self.assertIn(abc, self.client.get(f"/trip/{ab}").headers["Location"])
        self.assertEqual(self.split(abc).status_code, 302)
        self.assertEqual(self.viewer.get_annotations()[a]["purpose_note"], "Original A")
        self.assertEqual(len(self.viewer.load_trips()[0]), 3)

    def test_merge_with_a_next_trip_that_is_already_merged(self):
        a, b, c = self.trip("a", 0), self.trip("b", 120, .001), self.trip("c", 240, .002)
        bc = self.merged_key(self.merge(b, c))
        abc = self.merged_key(self.merge(a, bc))
        self.assertEqual(self.viewer.load_trips()[0][0]["parts"], [a, b, c])
        self.assertIn(abc, self.client.get(f"/trip/{bc}").headers["Location"])
        self.assertEqual(self.split(abc).status_code, 302)

    def test_chained_export_sources_keep_order_across_months(self):
        self.base = datetime(2026, 4, 30, 23, 58, tzinfo=timezone.utc)
        a, b, c = self.trip("a", 0), self.trip("b", 120), self.trip("c", 240)
        next_month = self.data / "2026-05"
        next_month.mkdir()
        for key in (b, c):
            for suffix in (".gpx", ".json"):
                path = self.data / (key + suffix)
                path.rename(next_month / path.name)
        b, c = "2026-05/b", "2026-05/c"
        ab = self.merged_key(self.merge(a, b))
        abc = self.merged_key(self.merge(ab, c))
        files = f"{a}.gpx; {b}.gpx; {c}.gpx"
        self.assert_export_sources("trip", "Mileage", "a", files)
        self.assertIn(files, self.client.get(f"/trip/{abc}").get_data(as_text=True))
        self.assertIn(files, self.client.get("/").get_data(as_text=True))

    def test_annotations_conflict_requires_clearing_every_field(self):
        a, b = self.trip("a", 0), self.trip("b", 120, .001)
        self.annotate(a, note="A text")
        self.annotate(b, destination="B text")
        page = self.client.get(f"/trip/{a}").get_data(as_text=True)
        self.assertIn("Both trips have annotations", page)
        self.assertNotIn(">Merge with next trip</button>", page)
        self.assertEqual(self.merge(a, b).status_code, 409)
        self.assertEqual(self.viewer.get_annotations()[a]["purpose_note"], "A text")
        self.annotate(a)
        key = self.merged_key(self.merge(a, b))
        self.assertEqual(self.viewer.load_trips()[0][0]["destination"], "B text")
        self.assertEqual(self.split(key).status_code, 302)

    def test_limit_boundary_legacy_fallback_and_refusals(self):
        a = self.trip("a", 0, legacy=True)
        b = self.trip("b", 931, .001)  # end_time=30: 901 seconds is too late
        self.assertEqual(self.merge(a, b).status_code, 409)
        self.trip("b", 930, .001)
        self.assertEqual(self.merge(a, b).status_code, 302)

    def test_recorded_until_not_last_movement_is_boundary(self):
        a = self.trip("a", 0)
        b = self.trip("b", 960, .001)  # exactly 15 minutes after final recorded point
        self.assertEqual(self.merge(a, b).status_code, 302)

    def test_different_device_nonadjacent_overlap_and_unknown_identity_refused(self):
        a, b, c = self.trip("a", 0), self.trip("b", 120, .001), self.trip("c", 240, .002)
        other = self.trip("other", 100, device="other-device")
        for target in (other, c, a, "../bad"):
            self.assertEqual(self.merge(a, target).status_code, 409)
        self.trip("b", 30, .001)
        self.assertEqual(self.merge(a, b).status_code, 409)
        self.trip("a", 0, device="")
        self.assertEqual(self.merge(a, b).status_code, 409)
        self.trip("a", 0, device="unknown")
        self.trip("b", 120, .001, device="unknown")
        self.assertEqual(self.merge(a, b).status_code, 409)

    def remove(self, key, reason="Phantom recording"):
        return self.client.post(f"/trip/{key}/remove", data={"csrf_token": "test-csrf", "reason": reason})

    def restore(self, key):
        return self.client.post(f"/trip/{key}/restore", data={"csrf_token": "test-csrf"})

    def total_miles(self):
        return sum(t["distance_miles"] for t in self.viewer.load_trips()[0])

    def three_legs(self):
        # A and C are a few minutes apart with a removable B between them.
        return self.trip("a", 0), self.trip("b", 180, .003), self.trip("c", 420, .014)

    def test_removed_trip_between_two_recordings_blocks_merging_across_it(self):
        a, b, c = self.three_legs()
        all_three = self.total_miles()
        self.assertEqual(self.remove(b).status_code, 302)
        without_b = self.total_miles()
        self.assertLess(without_b, all_three)
        reason = "A trip removed from the log lies between these recordings."
        page = self.client.get(f"/trip/{a}").get_data(as_text=True)
        self.assertIn(reason, page)
        self.assertIn("Restore it to merge them, or leave them separate.", page)
        self.assertNotIn(">Merge with next trip</button>", page)
        self.assertNotIn("No eligible next trip", page)
        # A forged request for the pair that skips B is refused as well.
        forged = self.merge(a, c)
        self.assertEqual(forged.status_code, 409)
        self.assertIn(reason, forged.get_data(as_text=True))
        self.assertEqual(self.merge(a, b).status_code, 409)
        self.assertEqual(self.total_miles(), without_b)
        # Restoring B puts the three-trip total back, never more.
        self.assertEqual(self.restore(b).status_code, 302)
        self.assertAlmostEqual(self.total_miles(), all_three)
        self.assertIn(">Merge with next trip</button>", self.client.get(f"/trip/{a}").get_data(as_text=True))
        # With B back, the pair is offered in order again.
        self.merged_key(self.merge(a, b))

    def test_removed_trip_far_from_the_next_leg_gives_no_misleading_reason(self):
        a, b = self.trip("a", 0), self.trip("b", 3600)
        self.remove(b)
        page = self.client.get(f"/trip/{a}").get_data(as_text=True)
        self.assertNotIn("removed from the log lies between", page)
        self.assertIn("No eligible next trip", page)

    def test_restore_is_refused_while_an_older_merge_covers_the_removed_trip(self):
        a, b, c = self.three_legs()
        all_three = self.total_miles()
        self.remove(b)
        # Data from before merge suggestions saw removed trips: A+C merged
        # while B was removed. Insert it as merge_trip would have.
        key = "2026-04/merge-" + "0" * 24
        keys = [a, c]
        summary = self.viewer.trip_merges.combined_summary(self.data, keys)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO trip_merges (trip_key, summary, fingerprint, updated_at) VALUES (?, ?, ?, ?)",
                         (key, json.dumps(summary), self.viewer.trip_merges.fingerprint(self.data, keys), "2026-04-14T12:00:00+00:00"))
            conn.executemany("INSERT INTO trip_merge_parts (part_key, merge_key, position) VALUES (?, ?, ?)",
                             [(part, key, i) for i, part in enumerate(keys)])
        bridged = self.total_miles()
        refused = self.restore(b)
        self.assertEqual(refused.status_code, 409)
        self.assertIn("A merged trip now covers this recording&#39;s time. Undo that merge first, then restore this trip.",
                      refused.get_data(as_text=True))
        self.assertEqual(self.total_miles(), bridged)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM trip_removals").fetchone()[0], 1)
        # Undoing the merge unblocks the restore, and nothing is counted twice.
        self.assertEqual(self.split(key).status_code, 302)
        self.assertEqual(self.restore(b).status_code, 302)
        self.assertAlmostEqual(self.total_miles(), all_three)

    def test_restore_ignores_merges_that_do_not_overlap_or_belong_to_another_device(self):
        a, b, c = self.three_legs()
        other = self.trip("other", 0, .02, device="other-device")
        self.remove(b)
        self.assertEqual(self.remove(other).status_code, 302)
        # A merge of two other-device legs spans B's time but isn't B's device.
        late = self.trip("late", 7200, .03, device="other-device")
        self.merged_key(self.merge(late, self.trip("later", 7260, .031, device="other-device")))
        self.assertEqual(self.restore(b).status_code, 302)
        self.assertEqual(self.restore(other).status_code, 302)

    def test_removed_unreadable_trip_never_blocks_merging_or_restoring(self):
        a, b, c = self.three_legs()
        self.damage_summary(b)
        with self.assertLogs(self.viewer.app.logger, "WARNING"):
            self.assertEqual(self.merge(a, c).status_code, 409)
            self.assertEqual(self.remove(b).status_code, 302)
        page = self.client.get(f"/trip/{a}").get_data(as_text=True)
        self.assertIn(">Merge with next trip</button>", page)
        merged = self.merged_key(self.merge(a, c))
        # No counted miles and no reliable span: restoring it is still allowed.
        self.assertEqual(self.restore(b).status_code, 302)
        self.assertEqual(self.split(merged).status_code, 302)

    def test_unknown_start_or_unreadable_summary_cannot_prove_adjacency(self):
        a, b = self.trip("a", 0), self.trip("b", 120, .001)
        unknown = self.trip("unknown", 240)
        path = self.data / (unknown + ".json")
        summary = json.loads(path.read_text(encoding="utf-8"))
        summary.update(start_time=None, timing_status="partial")
        path.write_text(json.dumps(summary), encoding="utf-8")
        self.assertEqual(self.merge(a, b).status_code, 409)
        path.write_text("[]", encoding="utf-8")
        with self.assertLogs(self.viewer.app.logger, "WARNING"):
            self.assertEqual(self.merge(a, b).status_code, 409)
            page = self.client.get(f"/trip/{a}").get_data(as_text=True)
        self.assertIn("an unreadable trip with an unknown device may fall between", page)
        self.assertNotIn("No eligible next trip", page)

    def test_damaged_summary_from_another_device_does_not_block(self):
        a, b = self.trip("a", 0), self.trip("b", 120)
        damaged = self.trip("damaged", 90, device="other-device")
        self.damage_summary(damaged)
        with self.assertLogs(self.viewer.app.logger, "WARNING"):
            page = self.client.get(f"/trip/{a}").get_data(as_text=True)
            self.assertIn(">Merge with next trip</button>", page)
            self.assertEqual(self.merge(a, b).status_code, 302)

    def test_same_device_damaged_summary_outside_window_does_not_block(self):
        a, b = self.trip("a", 0), self.trip("b", 120)
        for name, seconds in (("before", -3600), ("after", 3600)):
            self.damage_summary(self.trip(name, seconds))
        with self.assertLogs(self.viewer.app.logger, "WARNING"):
            page = self.client.get(f"/trip/{a}").get_data(as_text=True)
            self.assertIn(">Merge with next trip</button>", page)
            self.assertEqual(self.merge(a, b).status_code, 302)

    def test_same_device_damaged_summary_in_window_explains_block(self):
        a, b = self.trip("a", 0), self.trip("b", 120)
        # Include overlap and equal starts: none proves the damaged trip is
        # outside the chronological interval even if it starts before A ends.
        for seconds in (0, 30, 90, 120):
            with self.subTest(seconds=seconds):
                self.damage_summary(self.trip("damaged", seconds))
                with self.assertLogs(self.viewer.app.logger, "WARNING"):
                    page = self.client.get(f"/trip/{a}").get_data(as_text=True)
                    response = self.merge(a, b)
                reason = "an unreadable trip from this device may fall between these recordings"
                self.assertIn(reason, page)
                self.assertNotIn("No eligible next trip", page)
                self.assertNotIn(">Merge with next trip</button>", page)
                self.assertEqual(response.status_code, 409)
                self.assertIn(reason, response.get_data(as_text=True))

    def test_damaged_summary_timestamp_id_bounds_adjacency_across_months(self):
        a, b = self.trip("a", 0), self.trip("b", 120)
        old = self.trip("2026-03-14T12-00-00Z", -31 * 86400)
        self.damage_summary(old, start_time=None)
        prior_month = self.data / "2026-03"
        prior_month.mkdir()
        for suffix in (".gpx", ".json"):
            path = self.data / (old + suffix)
            path.rename(prior_month / path.name)
        with self.assertLogs(self.viewer.app.logger, "WARNING"):
            page = self.client.get(f"/trip/{a}").get_data(as_text=True)
        self.assertIn(">Merge with next trip</button>", page)
        damaged = self.trip("2026-04-14T12-01-30Z", 90)
        self.damage_summary(damaged, start_time="invalid")
        with self.assertLogs(self.viewer.app.logger, "WARNING"):
            response = self.merge(a, b)
        self.assertEqual(response.status_code, 409)
        self.assertIn("may fall between these recordings", response.get_data(as_text=True))

    def test_same_device_damaged_summary_with_unusable_timing_blocks(self):
        a, b = self.trip("a", 0), self.trip("b", 120)
        for name in ("legacy-id", "2026-02-30T12-00-00Z"):
            key = self.trip(name, 3600)
            self.damage_summary(key, start_time=None)
            with self.subTest(name=name), self.assertLogs(self.viewer.app.logger, "WARNING"):
                response = self.merge(a, b)
            self.assertEqual(response.status_code, 409)
            self.assertIn("may fall between these recordings", response.get_data(as_text=True))
            (self.data / (key + ".json")).unlink()
            (self.data / (key + ".gpx")).unlink()

    def test_unidentified_damaged_device_outside_window_does_not_block(self):
        # A timestamp ID proves chronology whatever device recorded it.
        a = self.trip("a", 0)
        self.trip("b", 120)
        for device in (None, "", "unknown", {"invalid": "device"}):
            key = self.trip("2026-03-14T12-00-00Z", -31 * 86400)
            self.damage_summary(key, device_id=device, start_time=None)
            with self.subTest(device=device), self.assertLogs(self.viewer.app.logger, "WARNING"):
                page = self.client.get(f"/trip/{a}").get_data(as_text=True)
            self.assertIn(">Merge with next trip</button>", page)

    def test_unidentified_damaged_device_in_window_blocks(self):
        a, b = self.trip("a", 0), self.trip("b", 120)
        for device in (None, "", "unknown", {"invalid": "device"}):
            key = self.trip("2026-04-14T12-01-30Z", 90)
            self.damage_summary(key, device_id=device, start_time=None)
            with self.subTest(device=device), self.assertLogs(self.viewer.app.logger, "WARNING"):
                response = self.merge(a, b)
            self.assertEqual(response.status_code, 409)
            self.assertIn("unknown device may fall between", response.get_data(as_text=True))

    def test_malformed_tracks_are_refused_without_hiding_originals(self):
        a, b = self.trip("a", 0), self.trip("b", 120, .001)
        for raw in (b"<gpx>", b"<gpx/>", b'<!DOCTYPE gpx [<!ENTITY x "text">]><gpx/>'):
            with self.subTest(raw=raw):
                (self.data / (b + ".gpx")).write_bytes(raw)
                self.assertEqual(self.merge(a, b).status_code, 409)
                self.assertEqual(len(self.viewer.load_trips()[0]), 2)

    def test_auth_csrf_and_explicit_undo_confirmation(self):
        a, b = self.trip("a", 0), self.trip("b", 120, .001)
        anonymous = self.viewer.app.test_client()
        for suffix in ("merge", "split"):
            self.assertEqual(anonymous.post(f"/trip/{a}/{suffix}").status_code, 401)
            self.assertEqual(self.client.post(f"/trip/{a}/{suffix}").status_code, 400)
        key = self.merged_key(self.merge(a, b))
        self.assertEqual(self.client.post(f"/trip/{key}/split", data={"csrf_token": "test-csrf"}).status_code, 400)
        self.assertEqual(len(self.viewer.load_trips()[0]), 1)

    def test_cache_invalidates_and_missing_part_requires_deliberate_admin_discard(self):
        a, b = self.trip("a", 0), self.trip("b", 120, .001)
        key = self.merged_key(self.merge(a, b))
        helper = self.viewer.trip_merges
        with patch.object(helper, "combined_summary", wraps=helper.combined_summary) as compute:
            self.trip("b", 120, .002)
            self.viewer.load_trips()
            self.assertEqual(compute.call_count, 1)
            self.viewer.load_trips()
            self.assertEqual(compute.call_count, 1)
        (self.data / (b + ".gpx")).unlink()
        trips, unreadable = self.viewer.load_trips()
        self.assertEqual(trips, [])
        self.assertEqual([t["trip_key"] for t in unreadable], [key])
        page = self.client.get("/").get_data(as_text=True)
        self.assertNotIn('>Undo merge (split back)</button>', page)
        self.assertIn('>Discard damaged merge</button>', page)
        self.assertIn(f"{a}.gpx; {b}.gpx", page)
        self.assertNotIn(key + ".gpx", page)
        self.assert_export_sources("unreadable", "Unreadable trips", "a", f"{a}.gpx; {b}.gpx")
        self.assertEqual(self.split(key).status_code, 409)
        self.assertEqual(self.discard(key).status_code, 302)

    def test_old_schema_migrates_concurrently_preserving_rows(self):
        db = self.root / "old.db"
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("CREATE TABLE annotations (trip_key TEXT PRIMARY KEY, category TEXT, purpose_note TEXT, updated_at TEXT)")
            conn.execute("INSERT INTO annotations VALUES ('2026-04/a', 'business', 'Original', 'old')")
        with patch.object(self.viewer, "DB_PATH", db):
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda _: self.viewer.init_db(), range(8)))
            self.assertEqual(self.viewer.get_annotations()["2026-04/a"]["purpose_note"], "Original")
        with closing(sqlite3.connect(db)) as conn:
            self.assertEqual(conn.execute("SELECT destination FROM annotations").fetchone(), ("",))
            self.assertEqual(conn.execute("SELECT count(*) FROM trip_merges").fetchone(), (0,))

    def test_concurrent_merge_requests_only_create_one_merge(self):
        a, b = self.trip("a", 0), self.trip("b", 120, .001)
        def attempt(_):
            client = self.viewer.app.test_client()
            self.authenticate(client)
            return self.merge(a, b, client).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(attempt, range(2))), [302, 409])
        self.assertEqual(len(self.viewer.load_trips()[0]), 1)

    def test_read_only_data_mount_contract(self):
        a, b = self.trip("a", 0), self.trip("b", 120, .001)
        snapshot = {p: p.read_bytes() for p in self.month.iterdir()}
        original_open, original_path_open = builtins.open, Path.open
        def check(path, mode):
            if not isinstance(path, (str, os.PathLike)):
                return
            resolved = Path(path).resolve()
            if self.data.resolve() in resolved.parents and any(c in mode for c in "wax+"):
                raise AssertionError("Attempted write to read-only trip data")
        def guarded_open(path, mode="r", *args, **kwargs):
            check(path, mode)
            return original_open(path, mode, *args, **kwargs)
        def guarded_path_open(path, mode="r", *args, **kwargs):
            check(path, mode)
            return original_path_open(path, mode, *args, **kwargs)
        with patch("builtins.open", guarded_open), patch.object(Path, "open", guarded_path_open):
            key = self.merged_key(self.merge(a, b))
            for route in ("/", f"/trip/{key}", f"/gpx/{key}", "/export.csv", "/export.xlsx"):
                self.assertEqual(self.client.get(route).status_code, 200)
            self.annotate(key, note="Merged annotation")
            self.assertEqual(self.split(key).status_code, 302)
        self.assertEqual(snapshot, {p: p.read_bytes() for p in self.month.iterdir()})

    def test_shared_summary_ships_in_default_deny_build_context(self):
        dockerignore = (SERVER / ".dockerignore").read_text(encoding="utf-8")
        self.assertIn("\n**\n", dockerignore)
        self.assertIn("!common/trip_summary.py", dockerignore)
        self.assertIn("!mileage-viewer/trip_merges.py", dockerignore)
        for service in ("mileage-ingest", "mileage-viewer"):
            self.assertIn("COPY common/ /app/common/", (SERVER / service / "Dockerfile").read_text())

    def autofill_allowed(self, key):
        response = self.client.get(f"/trip/{key}")
        self.assertEqual(response.status_code, 200)
        field = re.search(r'<input[^>]*id="destination"[^>]*>', response.get_data(as_text=True)).group()
        return 'data-autofill="true"' in field

    def test_autofill_counts_annotations_saved_before_a_merge(self):
        a, b = self.trip("a", 0), self.trip("b", 180, .01)
        # A was annotated, then saved blank; its history sits under A's key.
        self.assertEqual(self.annotate(a, "business", "Example note", "Example place").status_code, 302)
        self.assertEqual(self.annotate(a).status_code, 302)
        self.assertFalse(self.autofill_allowed(a))
        merged = self.merged_key(self.merge(a, b))
        self.assertNotIn(merged, (a, b))
        self.assertFalse(self.autofill_allowed(merged))

        # A part annotated before the merge stays annotated after a split.
        self.split(merged)
        self.assertFalse(self.autofill_allowed(a))
        self.assertTrue(self.autofill_allowed(b))

    def test_autofill_stops_after_a_merged_trip_is_annotated_under_its_own_key(self):
        a, b = self.trip("a", 0), self.trip("b", 180, .01)
        merged = self.merged_key(self.merge(a, b))
        # A fresh merge of never-annotated trips may still autofill.
        self.assertTrue(self.autofill_allowed(merged))
        # A blank save changes nothing, so it records no history and keeps it allowed.
        self.assertEqual(self.annotate(merged).status_code, 302)
        self.assertTrue(self.autofill_allowed(merged))
        for note in ("Example note", ""):
            self.assertEqual(self.annotate(merged, note=note).status_code, 302)
            self.assertFalse(self.autofill_allowed(merged))


if __name__ == "__main__":
    unittest.main()

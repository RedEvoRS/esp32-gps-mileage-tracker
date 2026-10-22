"""Role policy, upgrade and session regressions for the viewer."""

import importlib.util
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from werkzeug.security import generate_password_hash

SERVER = Path(__file__).resolve().parent.parent
ADMIN_HASH = generate_password_hash('test-password')
EDITOR_HASH = generate_password_hash('everyday-password')


class ViewerRolesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.db = root / 'annotations.db'
        # Model an old annotations database, with annotation and merge data.
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('CREATE TABLE annotations (trip_key TEXT PRIMARY KEY, category TEXT, purpose_note TEXT, updated_at TEXT)')
            conn.execute("INSERT INTO annotations VALUES ('2026-01/drive', 'business', 'example', '')")
        spec = importlib.util.spec_from_file_location('viewer_roles_test', SERVER / 'mileage-viewer/app.py')
        self.viewer = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {
            'INGEST_DATA_ROOT': str(root / 'data'), 'VIEWER_DB_PATH': str(self.db),
            'VIEWER_USERNAME': 'driver', 'VIEWER_PASSWORD_HASH': ADMIN_HASH,
            'VIEWER_SECRET_KEY': 'test-session-key-' * 4, 'DISPLAY_TZ': 'America/Los_Angeles',
        }):
            spec.loader.exec_module(self.viewer)
        self.viewer.app.config['TESTING'] = True
        self.viewer.app.static_folder = str(SERVER / 'mileage-viewer/static')
        month = root / 'data/2026-01'
        month.mkdir(parents=True)
        (month / 'drive.json').write_text(json.dumps({
            'trip_id': 'drive', 'device_id': 'example', 'start_time': '2026-01-02T12:00:00Z',
            'end_time': '2026-01-02T12:05:00Z', 'distance_miles': 1, 'duration_seconds': 300,
        }), encoding='utf-8')
        (month / 'drive.gpx').write_text('<gpx><trk><trkseg/></trk></gpx>', encoding='utf-8')
        self.seed_editor()
        self.client = self.viewer.app.test_client()

    def seed_editor(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO users VALUES (?, ?, 0, 0, ?, ?)",
                         ('helper', EDITOR_HASH, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'))

    def signed_in(self, role, client=None):
        client = client or self.client
        with client.session_transaction() as sess:
            sess.clear()
            sess['csrf'] = 'known-token'
            if role == 'admin':
                sess['user'] = 'driver'
                sess['pw_tag'] = self.viewer._password_tag()
            elif role == 'editor':
                sess['user'] = 'helper'
                sess['kind'] = 'editor'
                sess['pw_tag'] = self.viewer.users.session_tag(self.viewer.VIEWER_SECRET_KEY,
                                    self.viewer.users.get_user(self.db, 'helper'))
        return client

    def login(self, username, password='everyday-password', client=None):
        client = client or self.client
        client.get('/login')
        with client.session_transaction() as sess:
            token = sess['csrf']
        return client.post('/login', data={'username': username, 'password': password, 'csrf_token': token})

    def test_role_matrix_and_policy_coverage(self):
        # Expected statuses after successful authorization; invalid business
        # input exercises mutating routes without depending on geocoder/network.
        routes = {
            'health': ('/health', 'GET', 200), 'login': ('/login', 'GET', 302),
            'static': ('/static/gpx.js', 'GET', 200), 'index': ('/', 'GET', 200),
            'trip_view': ('/trip/2026-01/drive', 'GET', 200),
            'trip_gpx': ('/gpx/2026-01/drive', 'GET', 200),
            'annotate': ('/trip/2026-01/drive/annotate', 'POST', 302),
            'lookup_place': ('/trip/2026-01/drive/lookup-place', 'POST', 400),
            'merge_trip': ('/trip/2026-01/drive/merge', 'POST', 409),
            'split_trip': ('/trip/2026-01/drive/split', 'POST', 400),
            'export_csv': ('/export.csv', 'GET', 200), 'export_xlsx': ('/export.xlsx', 'GET', 200),
            'device_health': ('/device-health', 'GET', 200), 'logout': ('/logout', 'POST', 302),
            'admin': ('/admin', 'GET', 200),
            'admin_removed': ('/admin/removed', 'GET', 200),
            'admin_history': ('/admin/history', 'GET', 200),
            'close_year': ('/admin/year/2026/close', 'POST', 400),
            'reopen_year': ('/admin/year/2026/reopen', 'POST', 400),
            'remove_trip': ('/trip/2026-01/drive/remove', 'POST', 400),
            'restore_trip': ('/trip/2026-01/drive/restore', 'POST', 409),
            'account_password': ('/account/password', 'GET', 200),
        }
        endpoints = {rule.endpoint for rule in self.viewer.app.url_map.iter_rules()}
        self.assertEqual(endpoints, set(routes))
        self.assertFalse(endpoints - {'static', 'health', 'login'} - self.viewer.ENDPOINT_ROLES.keys())
        cases = [(endpoint, *case) for endpoint, case in routes.items()]
        cases.extend([
            ('login', '/login', 'POST', 302),
            ('admin', '/admin', 'POST', 302),
            ('account_password', '/account/password', 'POST', 302),
        ])
        declared_methods = {(rule.endpoint, method) for rule in self.viewer.app.url_map.iter_rules()
                            for method in rule.methods & {'GET', 'POST'}}
        self.assertEqual(declared_methods, {(endpoint, method) for endpoint, _, method, _ in cases})
        for role in (None, 'editor', 'admin'):
            for endpoint, path, method, status in cases:
                with self.subTest(role=role, endpoint=endpoint, method=method):
                    self.signed_in(role)
                    expected = status
                    if role is None:
                        expected = (200 if endpoint == 'health' or (endpoint == 'login' and method == 'GET')
                                    else (302 if method == 'GET' else 401))
                    elif endpoint in {'admin', 'admin_removed', 'admin_history', 'close_year', 'reopen_year',
                                      'remove_trip', 'restore_trip'} and role == 'editor':
                        expected = 403
                    response = self.client.open(path, method=method, data={'csrf_token': 'known-token'})
                    self.assertEqual(response.status_code, expected)
                    response.close()

    def test_disabled_login_has_generic_error_and_counts_failure(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('UPDATE users SET disabled=1')
        with patch.object(self.viewer, 'check_password_hash', wraps=self.viewer.check_password_hash) as check:
            response = self.login('helper')
            self.assertEqual(check.call_count, 1)
        self.assertEqual(response.status_code, 401)
        self.assertIn(b'Incorrect username or password.', response.data)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT fails FROM login_attempts').fetchone()[0], 1)

    def test_missing_policy_denied_and_role_precedes_csrf(self):
        self.viewer.app.add_url_rule('/unlisted', 'unlisted', lambda: 'private', methods=['GET', 'POST'])
        for role in ('admin', 'editor'):
            self.signed_in(role)
            for method in ('GET', 'POST'):
                self.assertEqual(self.client.open('/unlisted', method=method).status_code, 403)
        self.signed_in('editor')
        with patch.dict(self.viewer.ENDPOINT_ROLES, {'logout': {'admin'}}):
            self.assertEqual(self.client.post('/logout').status_code, 403)

    def test_exactly_one_hash_check(self):
        for username, password, expected in [('driver', 'test-password', 302), ('helper', 'everyday-password', 302),
                                              ('unknown', 'wrong-password', 401)]:
            with self.subTest(username=username):
                self.client = self.viewer.app.test_client()
                with patch.object(self.viewer, 'check_password_hash', wraps=self.viewer.check_password_hash) as check:
                    self.assertEqual(self.login(username, password).status_code, expected)
                    self.assertEqual(check.call_count, 1)

    def test_disabled_clashing_and_case_insensitive_names(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO users SELECT 'HELPER', password_hash, disabled, session_version, created_at, updated_at FROM users")
            conn.execute('UPDATE users SET disabled=1')
        self.assertEqual(self.login('HELPER').status_code, 401)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE users SET username='DRIVER', disabled=0")
        self.assertEqual(self.login('DRIVER').status_code, 401)
        with self.assertRaises(self.viewer.users.UserError):
            self.viewer.users.validate_username('DRIVER', 'driver')

    def test_session_revoked_from_current_database_state(self):
        admin = self.signed_in('admin', self.viewer.app.test_client())
        for change in ('UPDATE users SET session_version=session_version+1', 'UPDATE users SET disabled=1',
                       'UPDATE users SET disabled=0, session_version=session_version+1', 'DELETE FROM users'):
            self.signed_in('editor')
            with closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute(change)
            self.assertEqual(self.client.get('/').status_code, 302)
            self.assertEqual(admin.get('/').status_code, 200)
            with closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute('DELETE FROM users')
            self.seed_editor()

    def test_idempotent_upgrade_keeps_legacy_admin_and_data(self):
        self.signed_in('admin')
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO trip_merges (trip_key, summary, fingerprint, updated_at) VALUES ('2026-01/merge-example', '{}', 'example', '')")
            conn.execute('DROP TABLE users')
        for _ in range(2):
            self.viewer.init_db()
        self.assertEqual(self.client.get('/admin').status_code, 200)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT purpose_note FROM annotations').fetchone()[0], 'example')
            self.assertEqual(conn.execute('SELECT count(*) FROM trip_merges').fetchone()[0], 1)

    def test_headers_show_server_role(self):
        for role, label, link in [('admin', 'Admin', '/admin'), ('editor', 'Everyday', '/account/password')]:
            self.signed_in(role)
            for path in ('/', '/trip/2026-01/drive', '/device-health', '/account/password', '/no-such-page'):
                response = self.client.get(path)
                self.assertIn(('(' + label + ')').encode(), response.data)
                self.assertIn(('href="' + link + '"').encode(), response.data)


if __name__ == '__main__':
    unittest.main()

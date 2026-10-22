"""Everyday login management via web forms and the standalone CLI."""

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from werkzeug.security import check_password_hash
import test_viewer_roles as roles

CLI = roles.SERVER / 'mileage-viewer/manage_users.py'


def run_cli(db, *args, password=b'', admin='driver'):
    env = os.environ.copy()
    for name in ('DISPLAY_TZ', 'VIEWER_SECRET_KEY', 'VIEWER_PASSWORD_HASH', 'VIEWER_USERNAME'):
        env.pop(name, None)
    env['VIEWER_DB_PATH'] = str(db)
    if admin is not None:
        env['VIEWER_USERNAME'] = admin
    return subprocess.run([sys.executable, str(CLI), *args], input=password,
                          capture_output=True, env=env, timeout=30)


class ViewerAccountsTests(unittest.TestCase):
    setUp = roles.ViewerRolesTests.setUp
    seed_editor = roles.ViewerRolesTests.seed_editor
    signed_in = roles.ViewerRolesTests.signed_in
    login = roles.ViewerRolesTests.login

    def admin_post(self, action, username='helper', **fields):
        return self.client.post('/admin', data={'csrf_token': 'known-token', 'action': action,
                                              'username': username, **fields})

    def test_all_management_posts_require_admin_and_csrf(self):
        for action in ('add', 'reset-password', 'disable', 'enable', 'remove'):
            for role, expected in ((None, 401), ('editor', 403), ('admin', 302)):
                with self.subTest(action=action, role=role):
                    with closing(sqlite3.connect(self.db)) as conn, conn:
                        conn.execute('UPDATE users SET disabled=0')
                    self.signed_in(role)
                    self.assertEqual(self.admin_post(action).status_code, expected)
            self.signed_in('admin')
            self.assertEqual(self.client.post('/admin', data={'action': action}).status_code, 400)
        for role, expected in ((None, 401), ('editor', 400), ('admin', 400)):
            self.signed_in(role)
            self.assertEqual(self.client.post('/account/password').status_code, expected)

    def test_web_validation_and_one_shot_messages(self):
        self.signed_in('admin')
        for username, password, confirm, message in (
            ('DRIVER', 'new-password', 'new-password', 'clashes with the admin'),
            ('bad name', 'new-password', 'new-password', '1-64 letters'),
            ('x' * 65, 'new-password', 'new-password', '1-64 letters'),
            ('replace-me-helper', 'new-password', 'new-password', 'must not start'),
            ('example', 'short', 'short', '8-1024'),
            ('example', 'x' * 1025, 'x' * 1025, '8-1024'),
            ('HELPER', 'new-password', 'new-password', 'already exists'),
            ('example', 'new-password', 'different-password', "Passwords don&#39;t match"),
        ):
            with self.subTest(username=username[:20], message=message):
                self.assertEqual(self.admin_post('add', username, password=password,
                                               confirm_password=confirm).status_code, 302)
                self.assertIn(message.encode(), self.client.get('/admin').data)
                self.assertNotIn(message.encode(), self.client.get('/admin').data)
        self.admin_post('remove')
        self.assertIn(b'Confirm removing', self.client.get('/admin').data)
        self.assertIsNotNone(self.viewer.users.get_user(self.db, 'helper'))
        self.admin_post('unknown')
        self.assertIn(b'Unknown command', self.client.get('/admin').data)

    def test_web_add_lists_date_and_hashes_password(self):
        self.signed_in('admin')
        self.assertEqual(self.admin_post('add', 'example', password='new-password',
                                       confirm_password='new-password').status_code, 302)
        user = self.viewer.users.get_user(self.db, 'EXAMPLE')
        self.assertTrue(check_password_hash(user['password_hash'], 'new-password'))
        self.assertTrue(user['password_hash'].startswith('scrypt:'))
        response = self.client.get('/admin')
        self.assertIn(b'Admin (set in viewer.env)', response.data)
        self.assertIn(self.viewer.fmt_date(user['created_at']).encode(), response.data)
        self.assertNotIn(user['password_hash'].encode(), response.data)
        self.assertNotIn(b'value="new-password"', response.data)

    def test_web_and_cli_mutations_revoke_editor_but_not_admin(self):
        for source in ('web', 'cli'):
            for action in ('reset-password', 'disable', 'enable', 'remove'):
                with self.subTest(source=source, action=action):
                    with closing(sqlite3.connect(self.db)) as conn, conn:
                        conn.execute('DELETE FROM users')
                    self.seed_editor()
                    editor = self.signed_in('editor', self.viewer.app.test_client())
                    self.signed_in('admin')
                    if source == 'web':
                        self.assertEqual(self.admin_post(action, password='new-password',
                            confirm_password='new-password', confirm='yes').status_code, 302)
                    else:
                        result = run_cli(self.db, action, 'helper', password=b'new-password\n')
                        self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(editor.get('/').status_code, 302)
                    self.assertEqual(self.client.get('/admin').status_code, 200)
                    user = self.viewer.users.get_user(self.db, 'helper')
                    if action == 'remove':
                        self.assertIsNone(user)
                    else:
                        self.assertEqual(user['session_version'], 1)
                        self.assertEqual(user['disabled'], int(action == 'disable'))

    def password_post(self, current='everyday-password', password='new-password', confirm='new-password'):
        return self.client.post('/account/password', data={'csrf_token': 'known-token',
                    'current_password': current, 'password': password, 'confirm_password': confirm})

    def test_own_password_retags_only_current_session(self):
        self.signed_in('editor')
        other = self.signed_in('editor', self.viewer.app.test_client())
        admin = self.signed_in('admin', self.viewer.app.test_client())
        self.assertEqual(self.password_post().status_code, 302)
        self.assertEqual(self.client.get('/').status_code, 200)
        self.assertEqual(other.get('/').status_code, 302)
        self.assertEqual(admin.get('/admin').status_code, 200)
        self.assertIn(b'Other sessions', self.client.get('/account/password').data)
        fresh = self.viewer.app.test_client()
        self.assertEqual(self.login('HELPER', 'new-password', fresh).status_code, 302)

    def test_own_password_validation_lockout_and_admin_explanation(self):
        self.signed_in('editor')
        for password, confirm, message in [('short', 'short', b'8-1024'),
                                         ('new-password', 'different-password', b'Passwords don&#39;t match')]:
            self.password_post(password=password, confirm=confirm)
            self.assertIn(message, self.client.get('/account/password').data)
        self.assertTrue(check_password_hash(self.viewer.users.get_user(self.db, 'helper')['password_hash'], 'everyday-password'))
        for _ in range(5):
            self.assertEqual(self.password_post(current='wrong-password').status_code, 302)
        self.assertEqual(self.password_post().status_code, 429)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertIsNotNone(conn.execute('SELECT locked_until FROM login_attempts').fetchone()[0])
        self.signed_in('admin')
        response = self.client.get('/account/password')
        self.assertIn(b'bash install.sh', response.data)
        self.assertNotIn(b'name="current_password"', response.data)
        self.assertEqual(self.password_post().status_code, 302)

    def test_concurrent_reset_cannot_be_overwritten_by_own_password_change(self):
        self.signed_in('editor')
        original = self.viewer.users.change_user
        def concurrent_reset(*args, **kwargs):
            original(self.db, 'reset-password', 'helper', 'admin-new-password', 'driver')
            return original(*args, **kwargs)
        with patch.object(self.viewer.users, 'change_user', side_effect=concurrent_reset):
            self.assertEqual(self.password_post().status_code, 302)
        user = self.viewer.users.get_user(self.db, 'helper')
        self.assertTrue(check_password_hash(user['password_hash'], 'admin-new-password'))
        self.assertEqual(user['session_version'], 1)
        self.assertEqual(self.client.get('/').status_code, 302)

    def test_schema_initialization_serializes_with_cli(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: run_cli(self.db, 'list'), range(4)))
        self.viewer.init_db()
        self.assertTrue(all(result.returncode == 0 for result in results))
        self.assertTrue(all(result.stdout == b'helper\tenabled\n' for result in results))


class ManageUsersCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'nested/annotations.db'

    def run_command(self, *args, password=b'', code=0, admin='driver'):
        result = run_cli(self.db, *args, password=password, admin=admin)
        self.assertEqual(result.returncode, code, result.stderr)
        if password:
            self.assertNotIn(password.rstrip(b'\r\n'), result.stdout + result.stderr)
        self.assertNotIn(b'scrypt:', result.stdout + result.stderr)
        self.assertNotIn(b'Traceback', result.stderr)
        self.assertLessEqual(len(result.stderr.splitlines()), 1)
        return result

    def test_all_commands_and_exact_list_format(self):
        self.assertEqual(self.run_command('list').stdout, b'')
        self.run_command('add', 'helper', password=b'first-password\n')
        self.run_command('add', 'Example', password=b'other-password\n')
        self.assertEqual(self.run_command('list').stdout, b'Example\tenabled\nhelper\tenabled\n')
        self.run_command('disable', 'HELPER')
        self.assertEqual(self.run_command('list').stdout, b'Example\tenabled\nhelper\tdisabled\n')
        self.run_command('enable', 'helper')
        self.run_command('reset-password', 'helper', password=b'reset-password\r\n')
        with closing(sqlite3.connect(self.db)) as conn:
            row = conn.execute("SELECT password_hash, session_version, disabled, created_at, updated_at FROM users WHERE username='helper'").fetchone()
        self.assertTrue(check_password_hash(row[0], 'reset-password'))
        self.assertEqual(row[1:3], (3, 0))
        self.assertTrue(row[3].endswith('+00:00'))
        self.assertGreater(row[4], row[3])
        self.run_command('remove', 'helper')
        self.assertEqual(self.run_command('list').stdout, b'Example\tenabled\n')
        self.run_command('remove', 'Example')
        self.assertEqual(self.run_command('list').stdout, b'')

    def test_usage_errors_are_two(self):
        for args in ((), ('unknown',), ('list', 'extra'), ('add',), ('add', 'helper', 'secret'),
                     ('reset-password',), ('disable',), ('enable', 'helper', 'extra'), ('remove',)):
            with self.subTest(command=args[0] if args else ''):
                self.run_command(*args, code=2)

    def test_invalid_input_missing_duplicate_and_unexpected_error(self):
        for username in ('', 'bad name', 'x' * 65, 'replace-me', 'Replace-Me-helper', 'DRIVER', 'example\u00e9'):
            self.run_command('add', username, password=b'valid-password', code=3)
        for password in (b'', b'short', b'x' * 1025, b'\xff' * 8):
            self.run_command('add', 'helper', password=password, code=3)
        for command in ('reset-password', 'disable', 'enable', 'remove'):
            self.run_command(command, 'missing', password=b'valid-password', code=4)
        self.run_command('add', 'helper', password=b'valid-password')
        self.run_command('add', 'HELPER', password=b'valid-password', code=5)
        self.run_command('add', 'driver', password=b'valid-password', admin=None)
        self.db = Path(self.temp.name) / 'not-a-directory'
        self.db.write_text('example', encoding='utf-8')
        self.db = self.db / 'annotations.db'
        self.run_command('list', code=1)

    def test_password_stdin_removes_exactly_one_line_ending(self):
        for index, raw in enumerate((b'password1', b'password2\n', b'password3\r\n', b'password4\n\n',
                                     b'password5\r', b'password6\r\nembedded\n', b'x' * 1024 + b'\n')):
            username = 'helper' + str(index)
            self.run_command('add', username, password=raw)
            expected = raw
            if expected.endswith(b'\n'):
                expected = expected[:-1]
                if expected.endswith(b'\r'):
                    expected = expected[:-1]
            with closing(sqlite3.connect(self.db)) as conn:
                stored = conn.execute('SELECT password_hash FROM users WHERE username=?', (username,)).fetchone()[0]
            self.assertTrue(check_password_hash(stored, expected.decode('utf-8')))


if __name__ == '__main__':
    unittest.main()

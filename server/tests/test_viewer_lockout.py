"""The login lockout counts per IP; a success only clears its own username's failures."""

import importlib.util
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import test_viewer_roles as roles


class ViewerLockoutTests(unittest.TestCase):
    setUp = roles.ViewerRolesTests.setUp
    seed_editor = roles.ViewerRolesTests.seed_editor
    signed_in = roles.ViewerRolesTests.signed_in
    login = roles.ViewerRolesTests.login

    def password_post(self, current='everyday-password', password='new-password'):
        return self.client.post('/account/password', data={
            'csrf_token': 'known-token', 'current_password': current,
            'password': password, 'confirm_password': password})

    def attempt(self, username, password):
        # A fresh browser each time, as an attacker would use.
        return self.login(username, password, self.viewer.app.test_client()).status_code

    def row(self, ip='127.0.0.1'):
        with closing(sqlite3.connect(self.db)) as conn:
            return conn.execute('SELECT fails, locked_until FROM login_attempts WHERE ip=?', (ip,)).fetchone()

    def test_everyday_login_cannot_reset_failed_admin_guesses(self):
        codes = []
        for cycle in range(5):
            codes += [self.attempt('driver', f'guess-{cycle}-{i}') for i in range(4)]
            codes.append(self.attempt('helper', 'everyday-password'))
        # Cycle 1: four admin failures, then the helper's success. Its success
        # clears none of them, so the fifth failure in total locks the IP.
        self.assertEqual(codes[:6], [401, 401, 401, 401, 302, 401])
        self.assertEqual(codes[6:10], [429, 429, 429, 429])
        self.assertEqual(codes.count(401), 5)
        self.assertEqual(self.attempt('driver', 'test-password'), 429)
        self.assertEqual(self.attempt('helper', 'everyday-password'), 429)

    def test_a_users_own_success_clears_only_their_own_failures(self):
        for _ in range(2):
            self.assertEqual(self.attempt('helper', 'wrong'), 401)
        self.assertEqual(self.row()[0], 2)
        self.assertEqual(self.attempt('helper', 'everyday-password'), 302)
        self.assertIsNone(self.row())
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM login_attempt_names').fetchone()[0], 0)
        # The admin's own success clears the admin's failures the same way.
        for _ in range(4):
            self.attempt('driver', 'wrong')
        self.assertEqual(self.attempt('driver', 'test-password'), 302)
        self.assertIsNone(self.row())

    def test_mixed_names_keep_counting_and_names_are_case_insensitive(self):
        self.attempt('Helper', 'wrong')
        self.attempt('HELPER', 'wrong')
        self.attempt('driver', 'wrong')
        self.attempt('no-such-user', 'wrong')
        self.assertEqual(self.row()[0], 4)
        # 'helper' succeeds: the two failures typed as Helper/HELPER go, the
        # admin and unknown-name failures stay.
        self.assertEqual(self.attempt('helper', 'everyday-password'), 302)
        self.assertEqual(self.row()[0], 2)
        self.assertEqual(self.attempt('driver', 'wrong'), 401)
        self.assertEqual(self.attempt('no-such-user', 'wrong'), 401)
        self.assertEqual(self.attempt('another-name', 'wrong'), 401)
        self.assertEqual(self.attempt('driver', 'test-password'), 429)
        self.assertIsNotNone(self.row()[1])

    def test_failures_of_one_ip_do_not_touch_another(self):
        self.viewer._record_failure('192.0.2.1', 'driver')
        self.viewer._record_failure('192.0.2.2', 'helper')
        self.viewer._clear_failures('192.0.2.2', 'helper')
        self.assertEqual(self.row('192.0.2.1')[0], 1)
        self.assertIsNone(self.row('192.0.2.2'))

    def test_own_password_form_counts_against_the_users_name(self):
        self.signed_in('editor')
        for _ in range(3):
            self.assertEqual(self.password_post(current='wrong-password').status_code, 302)
        self.assertEqual(self.row()[0], 3)
        # A correct change clears the helper's three, not other names' failures.
        self.assertEqual(self.attempt('driver', 'wrong'), 401)
        self.assertEqual(self.row()[0], 4)
        self.assertEqual(self.password_post(current='everyday-password').status_code, 302)
        self.assertEqual(self.row()[0], 1)

    def test_own_password_form_success_cannot_reset_admin_guesses(self):
        for i in range(4):
            self.assertEqual(self.attempt('driver', f'guess-{i}'), 401)
        self.signed_in('editor')
        self.assertEqual(self.password_post().status_code, 302)
        self.assertEqual(self.row()[0], 4)
        self.assertEqual(self.attempt('driver', 'guess-4'), 401)
        self.assertEqual(self.attempt('driver', 'test-password'), 429)

    def test_documented_manual_unlock_fully_unlocks(self):
        # DEPLOY_DEBIAN.md: DELETE FROM login_attempts. It must clear the lock
        # and leave no per-name leftovers that a later success could subtract.
        for i in range(2):
            self.attempt('helper', 'wrong')
        for i in range(3):
            self.attempt('driver', 'wrong')
        self.assertEqual(self.attempt('driver', 'test-password'), 429)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('DELETE FROM login_attempts')
        self.assertEqual(self.attempt('helper', 'everyday-password'), 302)
        # The helper's pre-unlock failures are gone for good: four fresh admin
        # failures plus the helper's success must still leave four counted.
        for i in range(4):
            self.attempt('driver', f'again-{i}')
        self.assertEqual(self.row()[0], 4)
        self.assertEqual(self.attempt('helper', 'everyday-password'), 302)
        self.assertEqual(self.row()[0], 4)
        self.assertEqual(self.attempt('driver', 'again-4'), 401)
        self.assertEqual(self.attempt('driver', 'test-password'), 429)

    def test_stale_name_counts_after_manual_unlock_are_not_subtracted(self):
        for _ in range(3):
            self.attempt('helper', 'wrong')
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('DELETE FROM login_attempts')
        for i in range(4):
            self.attempt('driver', f'guess-{i}')
        self.assertEqual(self.attempt('helper', 'everyday-password'), 302)
        self.assertEqual(self.row()[0], 4)

    def test_lock_resets_the_per_name_counts(self):
        for i in range(5):
            self.attempt('helper', 'wrong')
        self.assertIsNotNone(self.row()[1])
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM login_attempt_names').fetchone()[0], 0)
        # Once the lock lapses, a success has nothing left to subtract.
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE login_attempts SET locked_until='2000-01-01T00:00:00+00:00'")
        self.attempt('driver', 'wrong')
        self.assertEqual(self.attempt('helper', 'everyday-password'), 302)
        self.assertEqual(self.row()[0], 1)

    def test_concurrent_failures_for_several_names_are_all_counted(self):
        names = ['driver', 'helper', 'someone'] * 8
        with patch.object(self.viewer, 'MAX_FAILS', 1000):
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda name: self.viewer._record_failure('192.0.2.9', name), names))
        self.assertEqual(self.row('192.0.2.9')[0], 24)
        with closing(sqlite3.connect(self.db)) as conn:
            counts = dict(conn.execute("SELECT name, fails FROM login_attempt_names WHERE ip='192.0.2.9'"))
        self.assertEqual(counts, {'driver': 8, 'helper': 8, 'someone': 8})

    def test_upgrade_keeps_old_login_attempts_and_still_locks(self):
        # An annotations.db from before per-name counts: login_attempts only.
        root = Path(self.temp.name) / 'upgrade'
        root.mkdir()
        old_db = root / 'annotations.db'
        with closing(sqlite3.connect(old_db)) as conn, conn:
            conn.execute('CREATE TABLE login_attempts (ip TEXT PRIMARY KEY, fails INTEGER NOT NULL DEFAULT 0, locked_until TEXT)')
            conn.execute("INSERT INTO login_attempts VALUES ('127.0.0.1', 3, NULL)")
            conn.execute("INSERT INTO login_attempts VALUES ('192.0.2.5', 0, '2999-01-01T00:00:00+00:00')")
        spec = importlib.util.spec_from_file_location('viewer_lockout_upgrade', roles.SERVER / 'mileage-viewer/app.py')
        viewer = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {
            'INGEST_DATA_ROOT': str(root / 'data'), 'VIEWER_DB_PATH': str(old_db),
            'VIEWER_USERNAME': 'driver', 'VIEWER_PASSWORD_HASH': roles.ADMIN_HASH,
            'VIEWER_SECRET_KEY': 'test-session-key-' * 4, 'DISPLAY_TZ': 'America/Los_Angeles',
        }):
            spec.loader.exec_module(viewer)
        with closing(sqlite3.connect(old_db)) as conn:
            self.assertEqual(conn.execute('SELECT fails FROM login_attempts WHERE ip="127.0.0.1"').fetchone()[0], 3)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM login_attempt_names').fetchone()[0], 0)
        self.assertGreater(viewer._lock_seconds_remaining('192.0.2.5'), 0)
        # The old, unattributed failures keep counting: two more lock the IP,
        # and a success by any user does not erase them.
        viewer._clear_failures('127.0.0.1', 'driver')
        viewer._record_failure('127.0.0.1', 'driver')
        viewer._record_failure('127.0.0.1', 'driver')
        self.assertGreater(viewer._lock_seconds_remaining('127.0.0.1'), 0)


if __name__ == '__main__':
    unittest.main()

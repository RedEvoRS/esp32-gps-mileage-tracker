"""The login lockout counts per IP; a success only clears its own username's failures."""

import importlib.util
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
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

    def test_stale_failures_and_their_names_expire_at_the_quiet_window(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with patch.object(self.viewer, 'datetime', wraps=datetime) as clock:
            clock.now.return_value = start
            for _ in range(4):
                self.viewer._record_failure('127.0.0.1', 'typo')
            # Just short of a quiet window, the existing count still applies.
            clock.now.return_value = start + timedelta(minutes=15, microseconds=-1)
            self.assertEqual(self.row()[0], 4)
            clock.now.return_value = start + timedelta(minutes=15)
            self.viewer._record_failure('127.0.0.1', 'driver')
        self.assertEqual(self.row(), (1, None))
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT name, fails FROM login_attempt_names').fetchall(), [('driver', 1)])
        # A later success against an expired name cannot erase this new failure.
        self.viewer._clear_failures('127.0.0.1', 'TYPO')
        self.assertEqual(self.row()[0], 1)

    def test_each_failure_restarts_the_quiet_window_and_five_still_lock(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with patch.object(self.viewer, 'datetime', wraps=datetime) as clock:
            for index in range(5):
                # More than 15 min from the first guess, but never a 15 min pause.
                clock.now.return_value = start + timedelta(minutes=14 * index)
                self.viewer._record_failure('127.0.0.1', 'typo')
                if index < 4:
                    self.assertEqual(self.row(), (index + 1, None))
            self.assertEqual(self.viewer._lock_seconds_remaining('127.0.0.1'), 15 * 60)

    def test_active_lock_lasts_fifteen_minutes_and_is_not_extended(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with patch.object(self.viewer, 'datetime', wraps=datetime) as clock:
            clock.now.return_value = start
            for _ in range(5):
                self.viewer._record_failure('127.0.0.1', 'driver')
            until = self.row()[1]
            clock.now.return_value = start + timedelta(minutes=14, seconds=59)
            self.assertEqual(self.viewer._lock_seconds_remaining('127.0.0.1'), 1)
            self.viewer._record_failure('127.0.0.1', 'helper')
            self.viewer._clear_failures('127.0.0.1', 'driver')
            self.assertEqual(self.row()[1], until)
            clock.now.return_value = start + timedelta(minutes=15)
            self.assertEqual(self.viewer._lock_seconds_remaining('127.0.0.1'), 0)
            self.viewer._record_failure('127.0.0.1', 'helper')
            self.assertEqual(self.row(), (1, None))

    def test_success_for_another_name_does_not_postpone_expiry(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with patch.object(self.viewer, 'datetime', wraps=datetime) as clock:
            clock.now.return_value = start
            self.viewer._record_failure('127.0.0.1', 'typo')
            clock.now.return_value = start + timedelta(minutes=14)
            self.assertEqual(self.attempt('helper', 'everyday-password'), 302)
            clock.now.return_value = start + timedelta(minutes=15)
            self.viewer._record_failure('127.0.0.1', 'driver')
            self.assertEqual(self.row(), (1, None))

    def test_concurrent_failures_after_quiet_window_start_one_fresh_count(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with patch.object(self.viewer, 'datetime', wraps=datetime) as clock:
            clock.now.return_value = start
            for _ in range(4):
                self.viewer._record_failure('127.0.0.1', 'typo')
            clock.now.return_value = start + timedelta(minutes=15)
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda _: self.viewer._record_failure('127.0.0.1', 'driver'), range(4)))
        self.assertEqual(self.row(), (4, None))

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

    def test_correct_password_in_flight_when_the_lock_starts_gets_no_session(self):
        # The password hash is slow; guesses already past the lock check can
        # finish after the fifth failure locked the address. Inject the lock at
        # exactly that point: after the lock check, before the session.
        real_check = self.viewer.check_password_hash

        def check_then_lock(stored, password):
            result = real_check(stored, password)
            for _ in range(5):
                self.viewer._record_failure('127.0.0.1', 'guesser')
            return result

        for username, password in (('driver', 'test-password'), ('helper', 'everyday-password')):
            with self.subTest(username=username):
                client = self.viewer.app.test_client()
                with patch.object(self.viewer, 'check_password_hash', side_effect=check_then_lock):
                    response = self.login(username, password, client)
                self.assertEqual(response.status_code, 429)
                self.assertIn(b'Too many failed attempts', response.data)
                with client.session_transaction() as sess:
                    self.assertNotIn('user', sess)
                self.assertEqual(client.get('/').status_code, 302)  # still sent to log in
                with closing(sqlite3.connect(self.db)) as conn, conn:
                    conn.execute('DELETE FROM login_attempts')
                    conn.execute('DELETE FROM login_attempt_names')

    def test_clear_failures_reports_whether_the_address_is_locked(self):
        self.assertTrue(self.viewer._clear_failures('127.0.0.1', 'driver'))
        self.viewer._record_failure('127.0.0.1', 'driver')
        self.assertTrue(self.viewer._clear_failures('127.0.0.1', 'driver'))
        for _ in range(5):
            self.viewer._record_failure('127.0.0.1', 'driver')
        self.assertFalse(self.viewer._clear_failures('127.0.0.1', 'driver'))
        self.assertGreater(self.viewer._lock_seconds_remaining('127.0.0.1'), 0)

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

    def test_upgrade_keeps_active_locks_but_untimed_failures_are_stale(self):
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
        # The old unattributed failures have no time; the next failure starts
        # a fresh count, while an already active lock is preserved above.
        viewer._clear_failures('127.0.0.1', 'driver')
        viewer._record_failure('127.0.0.1', 'driver')
        viewer._record_failure('127.0.0.1', 'driver')
        self.assertEqual(viewer._lock_seconds_remaining('127.0.0.1'), 0)
        with closing(sqlite3.connect(old_db)) as conn:
            self.assertEqual(conn.execute('SELECT fails FROM login_attempts WHERE ip="127.0.0.1"').fetchone()[0], 2)

    def test_v005_migration_is_additive_and_serialized_across_startup(self):
        old_db = Path(self.temp.name) / 'v005.db'
        with closing(sqlite3.connect(old_db)) as conn, conn:
            conn.execute('CREATE TABLE annotations (trip_key TEXT PRIMARY KEY, category TEXT, purpose_note TEXT, updated_at TEXT, destination TEXT NOT NULL DEFAULT "")')
            conn.execute("INSERT INTO annotations VALUES ('2026-01/example', 'business', 'example', '', 'example')")
            conn.execute('CREATE TABLE login_attempts (ip TEXT PRIMARY KEY, fails INTEGER NOT NULL DEFAULT 0, locked_until TEXT)')
            conn.execute('CREATE TABLE login_attempt_names (ip TEXT NOT NULL, name TEXT NOT NULL, fails INTEGER NOT NULL, PRIMARY KEY (ip, name))')
            conn.execute("INSERT INTO login_attempts VALUES ('192.0.2.1', 4, NULL)")
            conn.execute("INSERT INTO login_attempt_names VALUES ('192.0.2.1', 'typo', 4)")
            conn.execute("INSERT INTO login_attempts VALUES ('192.0.2.2', 0, '2999-01-01T00:00:00+00:00')")
        with patch.object(self.viewer, 'DB_PATH', old_db):
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda _: self.viewer.init_db(), range(8)))
            with closing(sqlite3.connect(old_db)) as conn:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM annotations').fetchone()[0], 1)
                self.assertEqual(conn.execute('SELECT last_failed_at FROM login_attempts').fetchall(), [(None,), (None,)])
                self.assertEqual(conn.execute('SELECT fails FROM login_attempt_names').fetchone()[0], 4)
            self.viewer._record_failure('192.0.2.2', 'driver')
            self.assertGreater(self.viewer._lock_seconds_remaining('192.0.2.2'), 0)
            self.viewer._record_failure('192.0.2.1', 'driver')
            self.viewer._clear_failures('192.0.2.1', 'typo')
            with closing(sqlite3.connect(old_db)) as conn:
                self.assertEqual(conn.execute('SELECT fails FROM login_attempts WHERE ip="192.0.2.1"').fetchone()[0], 1)
                self.assertEqual(conn.execute('SELECT name, fails FROM login_attempt_names').fetchall(), [('driver', 1)])
                self.assertIsNotNone(conn.execute('SELECT last_failed_at FROM login_attempts WHERE ip="192.0.2.1"').fetchone()[0])


if __name__ == '__main__':
    unittest.main()

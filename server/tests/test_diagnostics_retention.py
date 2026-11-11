"""Admin settings, atomic hand-off and ingest event retention regressions."""

import errno
import json
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import test_viewer_roles as roles
import test_ingest_diagnostics as ingest_tests
from test_ingest_diagnostics import batch


class RetentionAdminTests(unittest.TestCase):
    setUp = roles.ViewerRolesTests.setUp
    seed_editor = roles.ViewerRolesTests.seed_editor
    signed_in = roles.ViewerRolesTests.signed_in

    def settings(self):
        settings = self.viewer.DIAGNOSTICS_SETTINGS
        settings.mirror_path = Path(self.temp.name) / 'settings/diagnostics.json'
        settings.mirror_path.parent.mkdir(exist_ok=True)
        return settings

    def save(self, value):
        return self.client.post('/admin', data={'csrf_token': 'known-token',
            'action': 'diagnostics-retention', 'events_retention_days': value})

    def test_default_save_audit_and_health_text(self):
        settings = self.settings()
        self.assertTrue(settings.mirror())
        self.signed_in('admin')
        response = self.client.get('/admin')
        self.assertIn(b'name="events_retention_days"', response.data)
        self.assertIn(b'value="365"', response.data)
        self.assertIn(b'next time the tracker uploads diagnostics', response.data)
        self.assertNotIn(b'ingest worker', response.data)
        for days in (30, 3650, 400):
            self.assertEqual(self.save(str(days)).status_code, 302)
            self.assertEqual(settings.current()['days'], days)
            self.assertEqual(json.loads(settings.mirror_path.read_text()), {'events_retention_days': days})
            self.assertIn(f'value="{days}"'.encode(), self.client.get('/admin').data)
        current = settings.current()
        self.assertEqual(current['updated_by'], 'driver')
        self.assertEqual(datetime.fromisoformat(current['updated_at']).utcoffset(), timedelta(0))
        self.assertIn(b'Events older than 400 days', self.client.get('/device-health').data)

    def test_validation_changes_neither_db_nor_mirror(self):
        settings = self.settings()
        self.signed_in('admin')
        self.save('500')
        original = settings.current(), settings.mirror_path.read_bytes()
        for value in ('29', '3651', 'abc', '', '365.5', '-30', '+30', ' 365 ', '9' * 100):
            with self.subTest(value=value):
                self.assertEqual(self.save(value).status_code, 302)
                self.assertIn(b'whole number of days from 30 to 3650', self.client.get('/admin').data)
                self.assertEqual((settings.current(), settings.mirror_path.read_bytes()), original)

    def test_admin_role_and_csrf_required(self):
        settings = self.settings()
        for role, status in ((None, 401), ('editor', 403)):
            self.signed_in(role)
            self.assertEqual(self.save('500').status_code, status)
        self.signed_in('admin')
        self.assertEqual(self.client.post('/admin', data={
            'action': 'diagnostics-retention', 'events_retention_days': '500'}).status_code, 400)
        self.assertEqual(settings.current()['days'], 365)
        self.assertFalse(settings.mirror_path.exists())
        self.assertEqual(self.viewer.ENDPOINT_ROLES['admin'], {'admin'})

    def test_health_remains_available_when_optional_policy_cannot_be_read(self):
        self.signed_in('admin')
        with patch.object(self.viewer.DIAGNOSTICS_SETTINGS, 'current', side_effect=sqlite3.OperationalError('synthetic')):
            response = self.client.get('/device-health')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'No diagnostics yet', response.data)
        self.assertNotIn(b'Events older than', response.data)

    def test_atomic_replace_failure_keeps_complete_old_file_and_saved_db(self):
        settings = self.settings()
        settings.mirror()
        self.signed_in('admin')
        with patch('diagnostics_settings.os.replace', side_effect=OSError('synthetic')):
            self.assertEqual(self.save('500').status_code, 302)
        self.assertEqual(settings.current()['days'], 500)
        self.assertEqual(json.loads(settings.mirror_path.read_text()), {'events_retention_days': 365})
        self.assertEqual(list(settings.mirror_path.parent.iterdir()), [settings.mirror_path])
        self.assertIn(b'could not be handed to ingest', self.client.get('/admin').data)
        self.assertTrue(settings.mirror())
        self.assertNotIn(b'could not be handed to ingest', self.client.get('/admin').data)

    def test_atomic_write_fsyncs_before_rename_and_no_partial_file(self):
        settings = self.settings()
        settings.mirror()
        import diagnostics_settings
        original_replace = diagnostics_settings.os.replace
        calls = []
        def replace(source, target):
            self.assertEqual(Path(source).parent, target.parent)
            self.assertEqual(json.loads(Path(source).read_text()), {'events_retention_days': 500})
            self.assertEqual(json.loads(target.read_text()), {'events_retention_days': 365})
            self.assertIn('fsync', calls)
            calls.append('replace')
            original_replace(source, target)
        with patch('diagnostics_settings.os.fsync', side_effect=lambda _: calls.append('fsync')), \
                patch('diagnostics_settings.os.replace', side_effect=replace):
            self.assertTrue(settings.save('500', 'driver'))
        self.assertEqual(calls[:2], ['fsync', 'replace'])
        self.assertEqual(list(settings.mirror_path.parent.iterdir()), [settings.mirror_path])

    def test_concurrent_instances_and_startups_mirror_latest_committed_value(self):
        settings = self.settings()
        def save(days):
            other = self.viewer.DiagnosticsSettings(self.db, settings.mirror_path)
            return other.save(str(days), 'driver')
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertTrue(all(pool.map(save, range(400, 420))))
        self.assertEqual(json.loads(settings.mirror_path.read_text())['events_retention_days'], settings.current()['days'])
        self.assertEqual(list(settings.mirror_path.parent.iterdir()), [settings.mirror_path])

    def test_failure_after_replace_still_warns_until_another_worker_republishes(self):
        settings = self.settings()
        import diagnostics_settings
        original = diagnostics_settings.os.replace
        def fail_after_replace(source, target):
            original(source, target)
            raise OSError('synthetic durability failure')
        with patch('diagnostics_settings.os.replace', side_effect=fail_after_replace):
            self.assertFalse(settings.mirror())
        self.assertEqual(json.loads(settings.mirror_path.read_text()), {'events_retention_days': 365})
        self.assertIsNotNone(settings.warning(365))
        other = self.viewer.DiagnosticsSettings(self.db, settings.mirror_path)
        self.assertTrue(other.mirror())
        self.assertIsNone(settings.warning(365))

    def test_startup_with_missing_settings_directory_warns_but_serves(self):
        # Execute the complete import/startup again with an explicitly absent dir.
        settings_class = self.viewer.DiagnosticsSettings
        missing = Path(self.temp.name) / 'absent/diagnostics.json'
        with patch.object(settings_class.__init__, '__defaults__', (missing,)), \
                self.assertLogs('diagnostics_settings', level='WARNING'):
            roles.ViewerRolesTests.setUp(self)
        self.assertEqual(self.client.get('/health').status_code, 200)
        self.signed_in('admin')
        self.assertIn(b'could not be handed to ingest', self.client.get('/admin').data)
        self.assertEqual(self.save('500').status_code, 302)
        self.assertEqual(self.viewer.DIAGNOSTICS_SETTINGS.current()['days'], 500)

    def test_startup_replaces_mirror_from_restored_annotations(self):
        settings = self.settings()
        settings.save('500', 'driver')
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('DELETE FROM viewer_settings')  # Older restored DB: absent row means default.
        with patch.object(self.viewer.DiagnosticsSettings.__init__, '__defaults__', (settings.mirror_path,)):
            # A new process's startup helper reads the restored source, not the old mirror.
            fresh = self.viewer.DiagnosticsSettings(self.db)
            self.assertTrue(fresh.mirror())
        self.assertEqual(json.loads(settings.mirror_path.read_text()), {'events_retention_days': 365})


class RetentionIngestTests(unittest.TestCase):
    setUp = ingest_tests.DiagnosticsIngest.setUp
    post = ingest_tests.DiagnosticsIngest.post
    rows = ingest_tests.DiagnosticsIngest.rows

    def seed_old_events(self, count):
        body = batch()
        body['events'] = [dict(body['events'][0], sequence=n) for n in range(1, count + 1)]
        store = self.ing.DIAGNOSTICS_STORE
        store.accept('tracker-1', body)
        old = (datetime.now(timezone.utc) - timedelta(days=366)).isoformat()
        with closing(sqlite3.connect(store.path)) as db, db:
            db.execute('UPDATE events SET received_at=?', (old,))

    def test_budget_preserves_ack_and_next_upload_finishes_before_hourly_throttle(self):
        store = self.ing.DIAGNOSTICS_STORE
        self.seed_old_events(350)
        store.settings_path = self.root / 'diagnostics.json'
        store.settings_path.write_text('{"events_retention_days":30}')
        with patch('diag_store.time.monotonic', return_value=0) as clock, \
                patch('diag_store.time.sleep') as pause, patch.object(store, 'prune', wraps=store.prune) as prune:
            def yield_between_batches(seconds):
                self.assertEqual(seconds, 0.01)
                clock.return_value += 2  # Exhaust the budget without a real sleep.
            pause.side_effect = yield_between_batches
            with self.assertLogs('diag_store', level='INFO') as logs:
                response = self.post(batch(2, 351))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json(), {'accepted': [{'boot_id': ingest_tests.BOOT, 'sequence': 351}]})
            self.assertEqual(self.rows('SELECT MIN(rowid),MAX(rowid),COUNT(*) FROM events'), [(251, 351, 101)])
            self.assertIn('250 event row(s); more remain: True', logs.output[-1])

            with self.assertLogs('diag_store', level='INFO') as logs:
                response = self.post(batch(3, 352))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json(), {'accepted': [{'boot_id': ingest_tests.BOOT, 'sequence': 352}]})
            self.assertEqual(self.rows('SELECT rowid FROM events ORDER BY rowid'), [(351,), (352,)])
            self.assertIn('100 event row(s); more remain: False', logs.output[-1])
            self.assertEqual(prune.call_count, 2)
            pause.assert_called_once_with(0.01)

            with closing(sqlite3.connect(store.path)) as db, db:
                db.execute("UPDATE events SET received_at='2000-01-01T00:00:00+00:00' WHERE rowid=351")
            for stamp in (2, 3601):
                clock.return_value = stamp
                self.assertEqual(self.post(batch(3, 352)).status_code, 200)
                self.assertEqual(prune.call_count, 2)
                self.assertEqual(self.rows('SELECT rowid FROM events ORDER BY rowid'), [(351,), (352,)])
            clock.return_value = 3602
            self.assertEqual(self.post(batch(4, 353)).status_code, 200)
            self.assertEqual(prune.call_count, 3)
            self.assertEqual(self.rows('SELECT rowid FROM events ORDER BY rowid'), [(352,), (353,)])

    def test_budget_expiry_after_exact_batch_keeps_newest_and_restores_throttle(self):
        store = self.ing.DIAGNOSTICS_STORE
        self.seed_old_events(251)
        with patch('diag_store.time.monotonic', return_value=0) as clock, \
                patch('diag_store.time.sleep') as pause, patch.object(store, 'prune', wraps=store.prune) as prune:
            def yield_between_batches(_):
                clock.return_value = 2
            pause.side_effect = yield_between_batches
            with self.assertLogs('diag_store', level='INFO') as logs:
                self.assertEqual(self.post().status_code, 200)
            self.assertEqual(self.rows('SELECT rowid FROM events'), [(251,)])
            self.assertIn('250 event row(s); more remain: False', logs.output[-1])
            self.assertEqual(self.post(batch(2, 252)).status_code, 200)
            self.assertEqual(prune.call_count, 1)
            self.assertEqual(self.rows('SELECT rowid FROM events ORDER BY rowid'), [(251,), (252,)])

    def test_failed_continuation_preserves_ack_and_is_throttled(self):
        store = self.ing.DIAGNOSTICS_STORE
        self.seed_old_events(350)
        with patch('diag_store.time.monotonic', return_value=0) as clock, patch('diag_store.time.sleep') as pause:
            def yield_between_batches(_):
                clock.return_value = 2
            pause.side_effect = yield_between_batches
            self.assertEqual(self.post(batch(2, 351)).status_code, 200)
            self.assertEqual(self.rows('SELECT COUNT(*) FROM events'), [(101,)])
            with patch.object(store, 'prune', side_effect=sqlite3.OperationalError('synthetic')) as prune:
                with self.assertLogs(self.ing.app.logger, level='WARNING'):
                    response = self.post(batch(3, 352))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.get_json(), {'accepted': [{'boot_id': ingest_tests.BOOT, 'sequence': 352}]})
                self.assertEqual(prune.call_count, 1)
                for stamp in (2, 3601):
                    clock.return_value = stamp
                    self.assertEqual(self.post(batch(3, 352)).status_code, 200)
                    self.assertEqual(prune.call_count, 1)
            clock.return_value = 3602
            self.assertEqual(self.post(batch(4, 353)).status_code, 200)
            self.assertEqual(self.rows('SELECT rowid FROM events ORDER BY rowid'), [(351,), (352,), (353,)])

    def test_missing_valid_invalid_and_unreadable_settings(self):
        store = self.ing.DIAGNOSTICS_STORE
        store.settings_path = self.root / 'diagnostics.json'
        self.assertEqual(store.retention_days(), 365)
        for value in (30, 365, 3650):
            store.settings_path.write_text(json.dumps({'events_retention_days': value}))
            self.assertEqual(store.retention_days(), value)
        for raw, problem in (('broken', 'malformed JSON'), ('{"events_retention_days":29}', 'invalid days'),
                             ('[]', 'invalid days'), ('x' * 4097, 'oversized file')):
            store._settings_problems.clear()
            store.settings_path.write_text(raw)
            with self.assertLogs('diag_store', level='WARNING') as logs:
                self.assertEqual(store.retention_days(), 365)
                self.assertEqual(store.retention_days(), 365)
            self.assertEqual(len(logs.output), 1)
            self.assertIn(problem, logs.output[0])
        for value in (None, True, '365', 365.5, 3651):
            store.settings_path.write_text(json.dumps({'events_retention_days': value}))
            self.assertEqual(store.retention_days(), 365)
        store._settings_problems.clear()
        with patch.object(Path, 'open', side_effect=PermissionError(errno.EACCES, 'synthetic')), \
                self.assertLogs('diag_store', level='WARNING') as logs:
            self.assertEqual(store.retention_days(), 365)
            self.assertEqual(store.retention_days(), 365)
        self.assertEqual(len(logs.output), 1)

    def test_prune_batches_by_receipt_age_keeps_newest_and_schema_and_health(self):
        store = self.ing.DIAGNOSTICS_STORE
        body = batch()
        event = body['events'][0]
        body['events'] = [dict(event, sequence=n) for n in range(1, 604)]
        store.accept('tracker-1', body)
        now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        cutoff = now - timedelta(days=365)
        with closing(sqlite3.connect(store.path)) as db, db:
            db.execute('UPDATE events SET received_at=?', ((cutoff - timedelta(seconds=1)).isoformat(),))
            db.execute('UPDATE events SET received_at=? WHERE rowid=601', (cutoff.isoformat(),))
            db.execute('UPDATE events SET received_at=? WHERE rowid=602', ((cutoff + timedelta(microseconds=1)).isoformat(),))
            schema = db.execute('SELECT * FROM sqlite_master').fetchall()
            health = {table: db.execute('SELECT * FROM ' + table).fetchall()
                      for table in ('devices', 'boots', 'device_conditions', 'schema_version')}
        counts = []
        original = store._connect
        def connect():
            conn = original()
            conn.set_trace_callback(lambda sql: counts.append(sql) if sql.startswith('DELETE FROM events') else None)
            return conn
        with patch('diag_store.datetime') as clock, patch.object(store, '_connect', side_effect=connect), \
                self.assertLogs('diag_store', level='INFO') as logs:
            clock.now.return_value = now
            self.assertEqual(store.prune(365), 600)
        self.assertEqual(len(counts), 3)  # 250 + 250 + 100, each separately committed.
        self.assertIn('600 event row(s)', logs.output[-1])
        self.assertEqual(self.rows('SELECT rowid FROM events ORDER BY rowid'), [(601,), (602,), (603,)])
        self.assertEqual(self.rows('SELECT * FROM sqlite_master'), schema)
        for table, expected in health.items():
            self.assertEqual(self.rows('SELECT * FROM ' + table), expected)
        self.assertEqual(self.rows('SELECT * FROM schema_version'), [(1, 1)])
        store.accept('tracker-1', batch(2, 604))
        self.assertEqual(self.rows('SELECT MAX(rowid) FROM events'), [(604,)])

    def test_fractional_cutoff_and_single_old_row_survive(self):
        store = self.ing.DIAGNOSTICS_STORE
        body = batch()
        body['events'] = [dict(body['events'][0], sequence=n) for n in range(1, 5)]
        store.accept('tracker-1', body)
        now = datetime(2026, 1, 1, 12, 0, 0, 500000, tzinfo=timezone.utc)
        cutoff = now - timedelta(days=30)
        with closing(sqlite3.connect(store.path)) as db, db:
            for rowid, stamp in enumerate((cutoff.replace(microsecond=0), cutoff, cutoff + timedelta(microseconds=1),
                                           cutoff - timedelta(days=1)), 1):
                db.execute('UPDATE events SET received_at=? WHERE rowid=?', (stamp.isoformat(), rowid))
        with patch('diag_store.datetime') as clock:
            clock.now.return_value = now
            self.assertEqual(store.prune(30), 1)
        with closing(sqlite3.connect(store.path)) as db, db:
            db.execute('DELETE FROM events WHERE rowid<4')
        self.assertEqual(store.prune(30), 0)
        self.assertEqual(self.rows('SELECT rowid FROM events'), [(4,)])

    def test_upload_commits_before_pruning_and_errors_preserve_ack(self):
        store = self.ing.DIAGNOSTICS_STORE
        def fail():
            self.assertEqual(self.rows('SELECT sequence FROM events'), [(1,)])
            raise sqlite3.OperationalError('synthetic')
        with patch.object(store, 'prune_if_due', side_effect=fail), self.assertLogs(self.ing.app.logger, level='WARNING'):
            response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {'accepted': [{'boot_id': batch()['events'][0]['boot_id'], 'sequence': 1}]})
        with patch.object(store, 'prune_if_due') as prune:
            self.assertEqual(self.post(headers={}).status_code, 401)
            prune.assert_not_called()

    def test_hourly_throttle_includes_failed_attempts_and_rereads_setting(self):
        store = self.ing.DIAGNOSTICS_STORE
        store.settings_path = self.root / 'diagnostics.json'
        with patch('diag_store.time.monotonic', side_effect=[0, 1, 3599, 3600, 3601, 7200]), \
                patch.object(store, 'prune', side_effect=[None, OSError('synthetic'), None]) as prune:
            store.prune_if_due()
            store.prune_if_due()
            store.prune_if_due()
            store.settings_path.write_text('{"events_retention_days":30}')
            with self.assertRaises(OSError):
                store.prune_if_due()
            store.prune_if_due()
            store.prune_if_due()
        self.assertEqual([call.args for call in prune.call_args_list], [(365,), (30,), (30,)])


if __name__ == '__main__':
    unittest.main()

import copy
from contextlib import contextmanager
from datetime import datetime, timezone
import os
import sys
import unittest
from unittest import mock

import app
import automation
import model
import state_store
import storage_migration as migration


STAMP = datetime(2026, 10, 8, 11, 10, 0, 123456, tzinfo=timezone.utc)
SOURCE_URL = 'postgresql://source:source-secret@private-render/boat'
TARGET_URL = 'postgresql://boat.test:target-secret@observed.pooler.supabase.com:5432/postgres?sslmode=require'


def saved_data():
    return {
        'performance_ledger': [
            ('auto:20261008:10:01', {'id': 'auto:20261008:10:01', 'prediction_origin': 'automatic',
              'learned': True, 'settled': True, 'performance_version': 2,
              'bets': [{'bet': '1-2-3', 'category': 'gachi'}]}, STAMP),
            ('auto:20261008:10:02', {'id': 'auto:20261008:10:02', 'prediction_origin': 'automatic',
              'learned': False, 'settled': False, 'performance_version': 2,
              'bets': [{'bet': '6-1-5', 'category': 'roman'}]}, STAMP),
            ('old-manual', {'id': 'old-manual', 'learned': True, 'investment': 100}, STAMP),
        ],
        'ai_model_state': [(1, {'weights': {'nation': .28, 'exhibition_st': .03},
                                'temperature': 12.7, 'samples': 848, 'hits': 85,
                                'updated_at': '2026-10-08T20:10:00'}, STAMP)],
        'boat_automation_state': [(1, {'phase': 'watching', 'pending': 1}, STAMP)],
    }


class Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return copy.deepcopy(self.rows)

    def fetchone(self):
        return copy.deepcopy(self.rows[0]) if self.rows else None


class Database:
    """Transactional test store; no network or production credentials are used."""
    def __init__(self, tables=None):
        self.tables = copy.deepcopy(tables or {table: [] for table in migration.TABLES})
        self.receipt = None
        self.inserts = 0
        self.lease = True
        self.corrupt_insert = False
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    @contextmanager
    def transaction(self):
        original = copy.deepcopy((self.tables, self.receipt))
        try:
            yield
        except Exception:
            self.tables, self.receipt = original
            raise

    def execute(self, query, params=()):
        self.queries.append(query)
        if query.startswith('SELECT id,'):
            table = query.split('FROM public.')[1].split()[0]
            return Result(sorted(self.tables[table], key=lambda row: row[0]))
        if query.startswith('SELECT receipt'):
            return Result([(self.receipt,)] if self.receipt else [])
        if query.startswith('INSERT INTO public.boat_storage_migrations'):
            self.receipt = copy.deepcopy(params[1].obj)
        elif query.startswith('INSERT INTO public.'):
            table = query.split('public.')[1].split()[0]
            row_id, value, stamp = params
            value = copy.deepcopy(value.obj)
            if self.corrupt_insert and table == 'performance_ledger':
                value['learned'] = not value.get('learned')
            self.tables[table].append((row_id, value, stamp))
            self.inserts += 1
        elif query.startswith('SELECT pg_try_advisory_xact_lock'):
            return Result([(self.lease,)])
        return Result([])


class StorageMigrationTests(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {
            'BOAT_STORAGE_PHASE': 'render', 'BOAT_SUPABASE_URL': TARGET_URL,
            'DATABASE_URL': SOURCE_URL, 'POSTGRES_URL': '', 'BOAT_AUTO_RUNNER': '0',
        })
        env.start()
        self.addCleanup(env.stop)
        self.original_report = migration.storage_report()
        self.addCleanup(lambda: setattr(migration, '_report', self.original_report))
        self.source, self.destination = Database(saved_data()), Database()

    def copy_via_startup(self):
        os.environ['BOAT_STORAGE_PHASE'] = 'copy'
        with mock.patch('psycopg.connect', side_effect=[self.destination, self.source]):
            migration.initialize_storage()

    def test_copy_preserves_all_history_weights_pending_flags_and_timestamps(self):
        self.copy_via_startup()
        self.assertEqual(self.destination.tables, self.source.tables)
        self.assertEqual(self.source.tables, saved_data())
        self.assertEqual(self.destination.receipt['summary'], {
            'records': 3, 'model_samples': 848, 'automatic_predictions': 2,
            'automatic_learned': 1, 'automatic_pending': 1,
        })
        self.assertTrue(migration.storage_report()['verified'])
        self.assertFalse(any(query.startswith(('INSERT', 'UPDATE', 'DELETE'))
                             for query in self.source.queries))

    def test_copy_uses_all_records_instead_of_public_api_last_100(self):
        self.source.tables['performance_ledger'] = [
            (f'old-{i:03}', {'id': f'old-{i:03}', 'learned': True}, STAMP) for i in range(125)]
        self.copy_via_startup()
        self.assertEqual(len(self.destination.tables['performance_ledger']), 125)
        self.assertEqual(self.destination.tables['performance_ledger'][0][0], 'old-000')

    def test_verification_failure_rolls_back_every_table_and_receipt(self):
        self.destination.corrupt_insert = True
        with self.assertRaisesRegex(migration.MigrationError, 'Verification failed'):
            self.copy_via_startup()
        self.assertFalse(any(self.destination.tables.values()))
        self.assertIsNone(self.destination.receipt)

    def test_existing_destination_is_never_overwritten(self):
        self.destination.tables['performance_ledger'] = [('keep', {'id': 'keep'}, STAMP)]
        original = copy.deepcopy(self.destination.tables)
        with self.assertRaisesRegex(migration.MigrationError, 'contains data'):
            self.copy_via_startup()
        self.assertEqual(self.destination.tables, original)
        self.assertEqual(self.destination.inserts, 0)

    def test_repeated_copy_verifies_without_adding_rows_or_learning_again(self):
        self.copy_via_startup()
        inserts = self.destination.inserts
        self.copy_via_startup()
        self.assertEqual(self.destination.inserts, inserts)
        self.assertEqual(self.destination.tables['ai_model_state'][0][1]['samples'], 848)

    def test_source_change_after_copy_blocks_silent_recopy(self):
        self.copy_via_startup()
        self.source.tables['ai_model_state'][0][1]['samples'] += 1
        with self.assertRaisesRegex(migration.MigrationError, 'source changed'):
            self.copy_via_startup()
        self.assertEqual(self.destination.tables['ai_model_state'][0][1]['samples'], 848)

    def test_an_old_runner_must_finish_before_copy(self):
        self.source.lease = False
        with self.assertRaisesRegex(migration.MigrationError, 'old automatic cycle'):
            self.copy_via_startup()
        self.assertEqual(self.destination.inserts, 0)

    def test_missing_model_blocks_migration_instead_of_using_defaults(self):
        self.source.tables['ai_model_state'] = []
        with self.assertRaisesRegex(migration.MigrationError, 'saved model'):
            self.copy_via_startup()
        self.assertEqual(self.destination.inserts, 0)

    def test_activation_requires_receipt_and_an_exact_copy(self):
        with self.assertRaisesRegex(migration.MigrationError, 'receipt'):
            migration.activate_destination(self.destination)
        self.copy_via_startup()
        self.destination.tables['ai_model_state'][0][1]['samples'] += 1
        with self.assertRaisesRegex(migration.MigrationError, 'changed before activation'):
            migration.activate_destination(self.destination)
        self.assertIsNone(self.destination.receipt['activated_at'])

    def test_activation_runs_once_and_never_overwrites_subsequent_learning(self):
        self.copy_via_startup()
        receipt = migration.activate_destination(self.destination)
        activated = receipt['activated_at']
        self.destination.tables['ai_model_state'][0][1]['samples'] += 1
        self.assertEqual(migration.activate_destination(self.destination)['activated_at'], activated)
        self.assertEqual(self.destination.tables['ai_model_state'][0][1]['samples'], 849)
        with self.assertRaisesRegex(migration.MigrationError, 'already active'):
            self.copy_via_startup()

    def test_all_storage_modules_switch_to_same_destination_and_keep_source_env(self):
        os.environ['BOAT_STORAGE_PHASE'] = 'supabase'
        self.assertEqual(state_store.database_url(), TARGET_URL)
        self.assertEqual(app._database_url(), TARGET_URL)
        self.assertEqual(model._database_url(), TARGET_URL)
        self.assertEqual(os.environ['DATABASE_URL'], SOURCE_URL)
        del os.environ['BOAT_SUPABASE_URL']
        with self.assertRaisesRegex(RuntimeError, 'not configured'):
            state_store.database_url()

    def test_prepare_and_copy_block_manual_and_background_updates(self):
        for phase in ('prepare', 'copy'):
            with self.subTest(phase=phase):
                os.environ['BOAT_STORAGE_PHASE'] = phase
                self.assertFalse(automation.configured())
                self.assertEqual(automation.read_status()['phase'], 'maintenance')
                for write in (lambda: model.save(copy.deepcopy(model.DEFAULT)),
                              lambda: app.save_ledger([]), lambda: automation.write_status({})):
                    with self.assertRaises(state_store.StorageMaintenance):
                        write()
                client = app.app.test_client()
                for method, endpoint in [('post', '/api/learn'), ('post', '/api/performance'),
                                         ('post', '/api/settle_prediction'), ('delete', '/api/performance')]:
                    response = getattr(client, method)(endpoint, json={})
                    self.assertEqual(response.status_code, 503)
                    self.assertEqual(response.json['code'], 'storage_maintenance')

    def test_migrated_database_error_cannot_return_initial_model_or_empty_ledger(self):
        os.environ['BOAT_STORAGE_PHASE'] = 'supabase'
        with mock.patch.object(model, '_ensure_table', side_effect=RuntimeError('unreachable')):
            with self.assertRaisesRegex(RuntimeError, 'unreachable'):
                model.load()
        with mock.patch.object(app, '_ensure_ledger_table', side_effect=RuntimeError('unreachable')):
            with self.assertRaisesRegex(RuntimeError, 'unreachable'):
                app.load_ledger()

    def test_transaction_pooler_unencrypted_or_non_supabase_target_is_rejected(self):
        for invalid in [TARGET_URL.replace(':5432', ':6543'), TARGET_URL.split('?')[0],
                        TARGET_URL.replace('observed.pooler.supabase.com', 'other.example'),
                        'postgresql://password@invalid:bad']:
            with self.subTest(url=invalid):
                os.environ['BOAT_SUPABASE_URL'] = invalid
                with self.assertRaises(migration.MigrationError) as caught:
                    migration.destination_url()
                self.assertNotIn('target-secret', str(caught.exception))

    def test_connection_error_never_exposes_credentials_or_uses_local_storage(self):
        os.environ['BOAT_STORAGE_PHASE'] = 'copy'
        with mock.patch('psycopg.connect', side_effect=RuntimeError(TARGET_URL)):
            with self.assertRaises(migration.MigrationError) as caught:
                migration.initialize_storage()
        self.assertNotIn('target-secret', str(caught.exception))
        self.assertNotIn('pooler.supabase.com', str(caught.exception))
        self.assertFalse(migration.storage_report().get('verified'))

    def test_default_startup_does_not_contact_or_change_any_database(self):
        with mock.patch('psycopg.connect') as connect:
            migration.initialize_storage()
            connect.assert_not_called()
        self.assertEqual(migration.storage_report(), {'phase': 'render'})


if __name__ == '__main__':
    unittest.main()

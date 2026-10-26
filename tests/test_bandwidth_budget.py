"""Checks for bandwidth savings without changing saved predictions or data."""
from contextlib import contextmanager
import gzip
import json
from pathlib import Path
import unittest
from unittest import mock

import app as core
import automation


class BandwidthBudgetTests(unittest.TestCase):
    def setUp(self):
        self.client = core.app.test_client()

    def test_homepage_gzip_round_trip_identity_fallback_and_conditional_etag(self):
        original = (Path(__file__).resolve().parents[1] / 'static' / 'index.html').read_bytes()
        zipped = self.client.get('/', headers={'Accept-Encoding': 'gzip, deflate'})
        self.assertEqual(zipped.status_code, 200)
        self.assertEqual(zipped.headers['Content-Encoding'], 'gzip')
        self.assertIn('Accept-Encoding', zipped.headers['Vary'])
        self.assertEqual(gzip.decompress(zipped.data), original)
        self.assertLess(len(zipped.data), len(original) * .6)
        etag = zipped.headers['ETag']
        not_modified = self.client.get('/', headers={
            'Accept-Encoding': 'gzip',
            'If-None-Match': etag,
        })
        self.assertEqual(not_modified.status_code, 304)
        self.assertFalse(not_modified.data)
        plain = self.client.get('/', headers={'Accept-Encoding': 'identity'})
        self.assertEqual(plain.status_code, 200)
        self.assertNotIn('Content-Encoding', plain.headers)
        self.assertEqual(plain.data, original)
        self.assertIn('Accept-Encoding', plain.headers['Vary'])

    def test_json_gzip_is_lossless_and_preserves_cache_and_cors(self):
        status = {
            'ok': True, 'enabled': True, 'phase': 'watching',
            'message': '監視中' * 1100,
            'predicted': 12, 'settled': 8,
        }
        with mock.patch.object(core, 'read_status', return_value=status):
            compressed = self.client.get('/api/automation', headers={'Accept-Encoding': 'gzip'})
            plain = self.client.get('/api/automation', headers={'Accept-Encoding': 'identity'})
        self.assertEqual(compressed.status_code, 200)
        self.assertEqual(compressed.headers['Content-Encoding'], 'gzip')
        self.assertIn('Accept-Encoding', compressed.headers['Vary'])
        self.assertEqual(compressed.headers['Cache-Control'], 'no-store')
        self.assertEqual(compressed.headers['Access-Control-Allow-Origin'], '*')
        self.assertEqual(json.loads(gzip.decompress(compressed.data)), plain.json)
        self.assertEqual(plain.json, status)
        self.assertLess(len(compressed.data), len(plain.data) * .3)
        self.assertNotIn('Content-Encoding', plain.headers)

    def test_small_json_stays_uncompressed(self):
        with mock.patch.object(core, 'read_status', return_value={
            'ok': True, 'enabled': True, 'phase': 'waiting'
        }):
            response = self.client.get('/api/automation', headers={'Accept-Encoding': 'gzip'})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.headers.get('Content-Encoding'))
        self.assertTrue(response.json['enabled'])

    def test_monitoring_projection_keeps_database_events_but_omits_them_on_wire(self):
        details = {f'11:{i}': {'message': 'test' * 500} for i in range(1, 13)}
        full_status = {
            'date': '20261026', 'phase': 'watching',
            'last_checked_at': '2026-10-26T10:00:00+09:00',
            'predicted': 7, 'settled': 4, 'pending': 3,
            'race_events': details,
        }
        queries = []

        class Cursor:
            def fetchone(self):
                # Emulate the database-side subtraction, not a Python trim.
                return ({key: value for key, value in full_status.items()
                         if key != 'race_events'},)

        class Connection:
            def execute(self, sql):
                queries.append(sql)
                return Cursor()

        @contextmanager
        def connection():
            yield Connection()

        with mock.patch.object(automation, 'storage_paused', return_value=False), \
             mock.patch.object(automation, 'configured', return_value=True), \
             mock.patch.object(automation, 'ensure_status_table'), \
             mock.patch.object(automation, 'state_connection', connection):
            compact = automation.read_status()

        self.assertEqual(len(full_status['race_events']), 12)
        self.assertTrue(compact['enabled'])
        self.assertEqual(compact['predicted'], 7)
        self.assertEqual(compact['settled'], 4)
        self.assertEqual(compact['pending'], 3)
        self.assertNotIn('race_events', compact)
        self.assertLess(len(json.dumps(compact)), len(json.dumps(full_status)) / 10)
        self.assertEqual(len(queries), 1)
        self.assertIn("state - 'race_events'", queries[0])


if __name__ == '__main__':
    unittest.main()

"""A saved forecast lookup must stay small and must never create forecasts."""
import json
import unittest
from unittest import mock

import app as core


class SavedPredictionTests(unittest.TestCase):
    def setUp(self):
        self.client = core.app.test_client()
        self.row = {'id': 'auto:20261019:01:08', 'date': '20261019',
                    'stadium': '01', 'race': 8, 'prediction_origin': 'automatic',
                    'prediction': {'main': 1, 'bets': [{'bet': '1-2-3', 'category': 'gachi'}]},
                    'bets': [{'bet': '1-2-3', 'category': 'gachi'}]}

    def test_database_lookup_returns_only_one_record_without_loading_the_ledger(self):
        connection = mock.Mock()
        connection.execute.return_value.fetchone.return_value = (self.row,)
        with mock.patch.object(core, '_db_enabled', return_value=True), \
                mock.patch.object(core, '_db_connect') as connect, \
                mock.patch.object(core, 'load_ledger', side_effect=AssertionError('full ledger read')), \
                mock.patch.object(core, 'predict_race') as predict, \
                mock.patch.object(core, 'save_ledger') as save:
            connect.return_value.__enter__.return_value = connection
            response = self.client.get('/api/saved_prediction?date=2026-10-19&stadium=1&race=8')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['record'], self.row)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        statement, values = connection.execute.call_args.args
        self.assertIn('LIMIT 1', statement)
        self.assertIn('WHERE', statement)
        self.assertIn('ORDER BY id DESC', statement)
        self.assertEqual(values, ('20261019', '01', '1', '8'))
        connection.execute.assert_called_once()
        predict.assert_not_called()
        save.assert_not_called()

    def test_json_string_database_record_keeps_its_frozen_bets(self):
        connection = mock.Mock()
        connection.execute.return_value.fetchone.return_value = (json.dumps(self.row),)
        with mock.patch.object(core, '_db_enabled', return_value=True), \
                mock.patch.object(core, '_db_connect') as connect:
            connect.return_value.__enter__.return_value = connection
            self.assertEqual(core.load_saved_prediction('20261019', '01', 8), self.row)

    def test_missing_database_row_is_a_non_cached_404_without_a_full_read(self):
        connection = mock.Mock()
        connection.execute.return_value.fetchone.return_value = None
        with mock.patch.object(core, '_db_enabled', return_value=True), \
                mock.patch.object(core, '_db_connect') as connect, \
                mock.patch.object(core, 'load_ledger', side_effect=AssertionError('full ledger read')):
            connect.return_value.__enter__.return_value = connection
            response = self.client.get('/api/saved_prediction?date=20261019&stadium=01&race=9')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json['code'], 'prediction_missing')
        self.assertEqual(response.headers['Cache-Control'], 'no-store')

    def test_storage_failure_is_not_reported_as_a_missing_prediction(self):
        with mock.patch.object(core, 'load_saved_prediction', side_effect=RuntimeError('offline')), \
                mock.patch.object(core.app.logger, 'exception'):
            response = self.client.get('/api/saved_prediction?date=20261019&stadium=01&race=8')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json['code'], 'prediction_unavailable')
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertNotIn('offline', response.get_data(as_text=True))

    def test_invalid_dates_stadiums_and_races_are_rejected_before_storage_access(self):
        for query in ['date=20260230&stadium=01&race=1',
                      'date=20261019&stadium=99&race=1',
                      'date=20261019&stadium=01&race=nope',
                      'date=20261019&stadium=01&race=0',
                      'date=20261019&stadium=01&race=13',
                      'stadium=01&race=1']:
            with self.subTest(query=query), mock.patch.object(core, 'load_saved_prediction') as read:
                response = self.client.get('/api/saved_prediction?' + query)
                self.assertEqual(response.status_code, 400)
                read.assert_not_called()

    def test_local_legacy_records_are_scoped_to_date_venue_and_race(self):
        old = dict(self.row, id='old', date='2026/10/19', stadium='1')
        other_race = dict(self.row, id='other-race', race=9)
        other_day = dict(self.row, id='other-day', date='20261018')
        other_venue = dict(self.row, id='other-venue', stadium='02')
        latest = dict(old, id='latest', learned=True)
        with mock.patch.object(core, '_db_enabled', return_value=False), \
                mock.patch.object(core, 'load_ledger', return_value=[old, other_race, other_day, other_venue, latest]):
            response = self.client.get('/api/saved_prediction?date=20261019&stadium=01&race=8')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['record'], latest)


if __name__ == '__main__':
    unittest.main()

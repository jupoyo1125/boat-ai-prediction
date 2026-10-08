import copy
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

import requests
import app as target
import model

ROOT = Path(__file__).resolve().parent
OFFICIAL = [
    (1, '615', 25200), (2, '312', 5680), (3, '234', 3000),
    (4, '145', 1540), (5, '153', 11080), (6, '251', 11290),
    (7, '324', 22650), (8, '123', 990), (9, '143', 2590),
    (10, '215', 3170), (11, '132', 890), (12, '316', 4580),
]
MINIMAL_LIST = '<table>' + ''.join(
    f'<tr><td><a>{race}R</a></td><td>{" - ".join(combo)}</td>'
    f'<td>¥{payout:,}</td><td>6 - 1</td><td>¥3,820</td></tr>'
    for race, combo, payout in OFFICIAL
) + '</table>'


def fixture(name, fallback):
    path = ROOT / 'fixtures' / f'mikuni-20261007-{name}.html'
    return path.read_text() if path.exists() else fallback


LIST = fixture('resultlist', MINIMAL_LIST)
RACE = fixture('raceresult', '<table><tr><td>3連単</td><td>6-1-5</td><td>¥25,200</td><td>61</td></tr></table>')


class SettlementChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.rows = []
        self.saves = 0
        self.env = mock.patch.dict(os.environ, {'DATABASE_URL': '', 'POSTGRES_URL': ''})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.path = mock.patch.object(model, 'PATH', str(Path(self.tmp.name) / 'model.json'))
        self.path.start()
        self.addCleanup(self.path.stop)
        self.load = mock.patch.object(target, 'load_ledger', side_effect=lambda: copy.deepcopy(self.rows))
        self.load.start()
        self.addCleanup(self.load.stop)
        self.save = mock.patch.object(target, 'save_ledger', side_effect=self.save_rows)
        self.save.start()
        self.addCleanup(self.save.stop)
        self.clock = mock.patch.object(target.time, 'time', return_value=120)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        target.cached_official_results.cache_clear()
        self.get = mock.patch.object(target, 'get', return_value=LIST).start()
        self.addCleanup(mock.patch.stopall)
        self.client = target.app.test_client()

    def save_rows(self, rows):
        self.rows = copy.deepcopy(rows)
        self.saves += 1

    def add(self, race=1, bets=None):
        if bets is None:
            bets = [
                {'bet': '1-2-3', 'category': 'gachi'},
                {'bet': '6-1-5', 'category': 'roman'},
                {'bet': '6-1-5', 'category': 'oni'},
            ]
        response = self.client.post('/api/performance', json={
            'date': '20261007', 'stadium': '10', 'race': race,
            'combo': bets[0]['bet'].replace('-', '') if bets else '',
            'predicted_first': 1, 'bets': bets, 'investment': 9999,
        })
        self.assertEqual(response.status_code, 200, response.json)
        self.assertTrue(response.json['ok'])
        return response.json['record']

    def settle(self, race=1, **extra):
        return self.client.post('/api/settle_prediction', json={
            'date': '20261007', 'stadium': '10', 'race': race, **extra,
        })

    def stats(self):
        response = self.client.get('/api/performance')
        self.assertEqual(response.status_code, 200)
        return response.json

    def test_twelve_results_share_one_official_request_and_update_category_rates(self):
        for race in range(1, 13):
            self.add(race)
        for race, combo, payout in OFFICIAL:
            response = self.settle(race)
            self.assertEqual(response.status_code, 200, response.json)
            self.assertEqual(response.json['result'], {'race': race, 'combo': combo, 'payout': payout})
            self.assertEqual(response.json['status'], 'settled')
        self.assertEqual(self.get.call_count, 1)
        self.assertEqual(self.get.call_args.kwargs['timeout'], 15)
        self.assertEqual(model.load()['samples'], 12)
        stats = self.stats()['category_stats']
        for category in ['gachi', 'roman', 'oni']:
            self.assertEqual(stats[category]['settled'], 12)
            self.assertEqual(stats[category]['investment'], 1200)
        self.assertEqual(stats['gachi']['payout'], 990)
        self.assertEqual(stats['gachi']['roi'], 82.5)
        self.assertEqual(stats['roman']['payout'], 25200)
        self.assertEqual(stats['roman']['roi'], 2100)
        self.assertEqual(stats['oni']['roi'], 2100)

    def test_timeout_is_retried_and_result_is_saved(self):
        self.add()
        self.get.side_effect = [requests.ReadTimeout(), LIST]
        response = self.settle()
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(self.get.call_count, 2)
        self.assertTrue(all(call.kwargs['timeout'] == 15 for call in self.get.call_args_list))

    def test_failed_list_falls_back_to_individual_result(self):
        self.add()
        self.get.side_effect = [requests.ReadTimeout(), requests.ReadTimeout(), RACE]
        response = self.settle()
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json['result']['payout'], 25200)
        self.assertIn('raceresult', response.json['source'])
        self.assertEqual(self.get.call_count, 3)

    def test_all_network_failures_leave_prediction_pending(self):
        self.add()
        before = copy.deepcopy(self.rows)
        self.get.side_effect = requests.ReadTimeout()
        response = self.settle()
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json['code'], 'official_unavailable')
        self.assertEqual(self.rows, before)
        self.assertEqual(model.load()['samples'], 0)

    def test_missing_prediction_is_not_result_pending_and_does_not_fetch(self):
        response = self.settle()
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json['code'], 'prediction_missing')
        self.get.assert_not_called()

    def test_repeat_settlement_does_not_learn_twice_or_fetch_again(self):
        self.add()
        first = self.settle()
        second = self.settle()
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json['status'], 'already_settled')
        self.assertEqual(model.load()['samples'], 1)
        self.assertEqual(self.get.call_count, 1)
        self.assertEqual(self.stats()['category_stats']['roman']['settled'], 1)

    def test_not_yet_published_result_remains_pending(self):
        self.add()
        self.get.return_value = '<p>結果は未確定</p>'
        before = copy.deepcopy(self.rows)
        response = self.settle()
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json['code'], 'result_pending')
        self.assertEqual(self.rows, before)

    def test_missing_payout_is_a_failure_not_a_zero_return(self):
        self.add()
        self.get.return_value = '<p>3連単 6-1-5</p>'
        before = copy.deepcopy(self.rows)
        response = self.settle()
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json['code'], 'payout_missing')
        self.assertEqual(self.rows, before)
        self.assertEqual(model.load()['samples'], 0)

    def test_fullwidth_symbols_are_parsed(self):
        self.add()
        self.get.side_effect = ['<table></table>', '<p>３連単　６－１－５　￥２５，２００</p>']
        response = self.settle()
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json['result']['combo'], '615')
        self.assertEqual(response.json['result']['payout'], 25200)

    def test_duplicate_boat_is_rejected_without_changing_the_ledger(self):
        self.add()
        self.get.return_value = '<p>3連単 6-6-1 ¥100</p>'
        before = copy.deepcopy(self.rows)
        response = self.settle()
        self.assertEqual(response.status_code, 502)
        self.assertEqual(self.rows, before)

    def test_second_ticket_hit_returns_one_payout_for_the_category(self):
        self.add(bets=[{'bet': '123', 'category': 'gachi'}, {'bet': '615', 'category': 'gachi'}])
        response = self.settle()
        self.assertEqual(response.status_code, 200)
        record = response.json['record']
        self.assertEqual(record['investment'], 200)
        self.assertEqual(record['payout'], 25200)
        self.assertEqual(self.stats()['category_stats']['gachi']['roi'], 12600)

    def test_no_purchase_and_pending_records_do_not_affect_rates(self):
        self.add(race=1, bets=[])
        self.add(race=2)
        self.assertEqual(self.settle(1).status_code, 200)
        for stats in self.stats()['category_stats'].values():
            self.assertEqual(stats['settled'], 0)
            self.assertIsNone(stats['roi'])

    def test_legacy_record_is_preserved_but_not_reclassified(self):
        self.rows = [{'id': 'old', 'date': '20261007', 'stadium': '10', 'race': 1,
                      'combo': '615', 'investment': 1000, 'learned': False}]
        response = self.settle()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['record']['payout'], 252000)
        self.assertEqual(self.stats()['legacy_records'], 1)
        for stats in self.stats()['category_stats'].values():
            self.assertEqual(stats['settled'], 0)

    def test_reanalysis_uses_latest_prediction_once(self):
        self.add()
        self.settle()
        self.add(bets=[{'bet': '615', 'category': 'gachi'}])
        self.assertEqual(self.settle().status_code, 200)
        stats = self.stats()['category_stats']
        self.assertEqual(stats['gachi']['settled'], 1)
        self.assertEqual(stats['gachi']['investment'], 100)
        self.assertEqual(stats['roman']['settled'], 0)

    def test_cache_refreshes_when_new_results_become_available(self):
        self.add()
        self.get.side_effect = ['<p>未確定</p>', '<p>未確定</p>', LIST]
        self.assertEqual(self.settle().json['code'], 'result_pending')
        with mock.patch.object(target.time, 'time', return_value=180):
            response = self.settle()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.get.call_count, 3)

    def test_invalid_race_input_does_not_fetch(self):
        for extra in [{'date': 'not-a-date'}, {'stadium': '25'}, {'race': 0}, {'race': 13}]:
            response = self.settle(**extra)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json['code'], 'invalid_race')
        self.get.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)

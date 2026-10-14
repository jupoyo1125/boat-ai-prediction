import copy
from datetime import datetime, timezone
import unittest
from unittest import mock

import app as core


def record(date, race, *, automatic=True, settled=True, payout=500, bets=None):
    return {
        'id': f'{date}:{race}', 'date': date, 'stadium': '07', 'race': race,
        'performance_version': 2, 'prediction_origin': 'automatic' if automatic else 'manual',
        'settled': settled, 'actual_combo': '123' if settled else '',
        'official_payout': payout,
        'bets': bets if bets is not None else [
            {'category': 'gachi', 'bet': '1-2-3'},
            {'category': 'roman', 'bet': '4-5-6'},
            {'category': 'oni', 'bet': '1-2-3'},
        ],
    }


class DailyPerformanceChecks(unittest.TestCase):
    def setUp(self):
        self.rows = []
        self.load = mock.patch.object(core, 'load_ledger', side_effect=lambda: copy.deepcopy(self.rows)).start()
        self.history = {'ok': True, 'category_stats': {'gachi': {'roi': 9999}}}
        mock.patch.object(core, 'historical_status', return_value=self.history).start()
        self.addCleanup(mock.patch.stopall)
        self.client = core.app.test_client()

    def test_daily_and_cumulative_keep_manual_history_and_pending_separate(self):
        gachi = [{'category': 'gachi', 'bet': '1-2-3'}]
        self.rows = [
            record('20261013', 1, payout=100, bets=gachi),
            record('20261014', 1, payout=900, bets=gachi + [{'category': 'gachi', 'bet': '1-3-2'}]),
            record('20261014', 2, payout=600, bets=[{'category': 'gachi', 'bet': '4-5-6'}]),
            record('20261014', 3, settled=False, bets=gachi),
            record('2026/10/14', 4, automatic=False, payout=2000, bets=gachi),
            record('20261015', 1, bets=[{'category': 'gachi', 'bet': '4-5-6'}]),
            {'date': '20261014', 'investment': 100000, 'payout': 999999, 'settled': True},
        ]
        response = self.client.get('/api/performance?date=2026-10-14')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        data = response.json
        daily = data['daily']['automatic_category_stats']['gachi']
        self.assertEqual(data['daily']['date'], '20261014')
        self.assertEqual((daily['settled'], daily['wins'], daily['pending']), (2, 1, 1))
        self.assertEqual((daily['investment'], daily['payout'], daily['profit']), (300, 900, 600))
        self.assertEqual((daily['roi'], daily['hit_rate']), (300, 50))
        self.assertEqual(data['daily']['category_stats']['gachi']['roi'], 725)
        self.assertEqual(data['daily']['legacy_records'], 1)
        self.assertEqual(data['automatic_category_stats']['gachi']['roi'], 200)
        self.assertEqual(data['historical'], self.history)
        other_day = self.client.get('/api/performance?date=20261013').json
        self.assertEqual(other_day['automatic_category_stats'], data['automatic_category_stats'])
        self.assertEqual(other_day['category_stats'], data['category_stats'])
        self.assertEqual(self.rows[3]['settled'], False)

    def test_three_categories_use_their_own_bets(self):
        self.rows = [record('20261014', 1, payout=900)]
        stats = self.client.get('/api/performance?date=20261014').json['daily']['automatic_category_stats']
        self.assertEqual(set(stats), {'gachi', 'roman', 'oni'})
        self.assertEqual((stats['gachi']['roi'], stats['roman']['roi'], stats['oni']['roi']), (900, 0, 900))
        self.assertEqual((stats['gachi']['profit'], stats['roman']['profit'], stats['oni']['profit']), (800, -100, 800))

    def test_daily_uses_the_full_ledger_not_only_the_last_hundred_records(self):
        self.rows = [record('20261010', 1)] + [record('20261014', race) for race in range(1, 102)]
        data = self.client.get('/api/performance?date=20261010').json
        self.assertEqual(len(data['records']), 100)
        self.assertEqual(data['daily']['category_stats']['gachi']['settled'], 1)

    def test_same_race_is_counted_once_and_no_buy_categories_are_excluded(self):
        original = record('20261014', 1, payout=500)
        latest = record('2026-10-14', 1, payout=1000,
                        bets=[{'category': 'gachi', 'bet': '1-2-3'}])
        self.rows = [original, latest, record('20261014', 2, bets=[])]
        stats = self.client.get('/api/performance?date=20261014').json['daily']['category_stats']
        self.assertEqual((stats['gachi']['settled'], stats['gachi']['investment'], stats['gachi']['payout']), (1, 100, 1000))
        self.assertIsNone(stats['roman']['roi'])
        self.assertEqual(stats['oni']['settled'], 0)

    def test_pending_and_empty_days_have_no_fabricated_zero_percent(self):
        self.rows = [record('20261014', 1, settled=False)]
        for date, pending in [('20261014', 1), ('20261012', 0)]:
            stats = self.client.get('/api/performance?date=' + date).json['daily']['category_stats']
            for category in stats.values():
                self.assertIsNone(category['roi'])
                self.assertIsNone(category['hit_rate'])
                self.assertEqual(category['investment'], 0)
                self.assertEqual(category['pending'], pending)

    def test_default_date_is_today_in_japan_across_utc_midnight_boundary(self):
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                instant = datetime(2026, 10, 14, 15, 30, tzinfo=timezone.utc)
                return instant.astimezone(tz) if tz is not None else instant.replace(tzinfo=None)
        self.rows = [record('20261015', 1), record('20261014', 1)]
        with mock.patch.object(core, 'datetime', Clock):
            data = self.client.get('/api/performance').json
        self.assertEqual(data['daily']['date'], '20261015')
        self.assertEqual(data['daily']['category_stats']['gachi']['settled'], 1)

    def test_invalid_dates_are_rejected_before_reading_the_ledger(self):
        for date in ['', '20260230', 'abc', '202610140', '2026-13-01']:
            with self.subTest(date=date):
                response = self.client.get('/api/performance', query_string={'date': date})
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.json['ok'])
        self.load.assert_not_called()


if __name__ == '__main__':
    unittest.main()

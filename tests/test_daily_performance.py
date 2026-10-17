import copy
from datetime import datetime, timezone
import unittest
from unittest import mock

import app as core


def record(date, race, *, stadium='07', automatic=True, settled=True, payout=500, bets=None):
    return {
        'id': f'{date}:{stadium}:{race}', 'date': date, 'stadium': stadium, 'race': race,
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
        data = self.client.get('/api/performance?date=20261010&stadium=07').json
        self.assertEqual(len(data['records']), 100)
        self.assertEqual(data['daily']['category_stats']['gachi']['settled'], 1)
        self.assertEqual(len(data['daily']['category_hits']['gachi']), 1)

    def test_hit_lists_match_each_category_and_scope_without_combining_payouts(self):
        self.rows = [
            dict(record('20261014', 1, stadium='04', payout=900), payout=1800),
            record('2026/10/14', 2, stadium='04', automatic=False, payout=500,
                   bets=[{'category': 'roman', 'bet': '1-2-3'}]),
            record('20261014', 3, stadium='04', settled=False),
            record('20261014', 1, stadium='06', payout=200),
            record('20261013', 1, stadium='04', payout=100),
            {'date': '20261014', 'stadium': '04', 'settled': True, 'hit': True, 'payout': 99999},
        ]
        original = copy.deepcopy(self.rows)
        with mock.patch.object(core, 'save_ledger') as save:
            daily = self.client.get('/api/performance?date=20261014&stadium=04').json['daily']
            save.assert_not_called()
        expected = {'date': '20261014', 'stadium': '04', 'venue': '平和島',
                    'race': 1, 'actual_combo': '1-2-3', 'payout': 900}
        self.assertEqual(daily['category_hits']['gachi'], [expected])
        self.assertEqual(daily['automatic_category_hits']['oni'], [expected])
        self.assertEqual(daily['automatic_category_hits']['roman'], [])
        self.assertEqual(daily['category_hits']['roman'][0]['race'], 2)
        for stats_key, hits_key in [('category_stats', 'category_hits'),
                                    ('automatic_category_stats', 'automatic_category_hits')]:
            for category, hits in daily[hits_key].items():
                self.assertEqual(len(hits), daily[stats_key][category]['wins'])
                self.assertEqual(sum(hit['payout'] for hit in hits), daily[stats_key][category]['payout'])
        self.assertEqual(self.rows, original)

    def test_hit_lists_use_latest_forecast_and_sort_by_venue_then_numeric_race(self):
        self.rows = [
            record('20261014', 10, stadium='06'),
            record('20261014', 1, stadium='04'),
            record('2026-10-14', 1, stadium='04', payout=700),
            record('20261014', 2, stadium='06'),
            record('20261014', 3, stadium='04'),
            record('2026/10/14', 3, stadium=4, settled=False),
            record('20261014', 4, stadium='04', bets=[]),
        ]
        daily = self.client.get('/api/performance?date=20261014').json['daily']
        hits = daily['category_hits']['gachi']
        self.assertEqual([(hit['stadium'], hit['race']) for hit in hits],
                         [('04', 1), ('06', 2), ('06', 10)])
        self.assertEqual(hits[0]['payout'], 700)
        for category in core.BET_CATEGORIES:
            self.assertEqual(len(daily['category_hits'][category]), daily['category_stats'][category]['wins'])

    def test_hit_lists_include_more_than_a_hundred_daily_winners(self):
        self.rows = [record('20261014', race, stadium=str(stadium).zfill(2))
                     for stadium in range(1, 11) for race in range(1, 13)]
        data = self.client.get('/api/performance?date=20261014').json
        self.assertEqual(len(data['records']), 100)
        self.assertEqual(len(data['daily']['automatic_category_hits']['gachi']), 120)
        self.assertEqual(len(data['daily']['category_hits']['oni']), 120)

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
            daily = self.client.get('/api/performance?date=' + date).json['daily']
            self.assertEqual(daily['category_hits'], {category: [] for category in core.BET_CATEGORIES})

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

    def test_selected_venue_filters_only_the_daily_totals_for_both_forecast_types(self):
        gachi = [{'category': 'gachi', 'bet': '1-2-3'}]
        self.rows = [
            record('20261014', 1, stadium='04', payout=900),
            record('20261014', 2, stadium='04', settled=False),
            record('2026/10/14', 3, stadium='04', automatic=False, payout=500),
            record('20261014', 4, stadium=4, payout=200, bets=gachi),
            record('20261014', 1, stadium='06', payout=200),
            record('20261013', 1, stadium='04', payout=100),
            {'date': '20261014', 'stadium': '04', 'investment': 100, 'settled': True},
            {'date': '20261014', 'stadium': '06', 'investment': 100, 'settled': True},
        ]
        all_venues = self.client.get('/api/performance?date=20261014').json
        response = self.client.get('/api/performance?date=20261014&stadium=04')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        data = response.json
        daily = data['daily']
        self.assertEqual((daily['date'], daily['stadium'], daily['venue']), ('20261014', '04', '平和島'))
        automatic = daily['automatic_category_stats']['gachi']
        self.assertEqual((automatic['settled'], automatic['wins'], automatic['pending']), (2, 2, 1))
        self.assertEqual((automatic['investment'], automatic['payout'], automatic['profit']), (200, 1100, 900))
        self.assertEqual((automatic['roi'], automatic['hit_rate']), (550, 100))
        self.assertEqual(daily['category_stats']['gachi']['settled'], 3)
        self.assertEqual(daily['category_stats']['gachi']['payout'], 1600)
        self.assertEqual(daily['automatic_category_stats']['roman']['roi'], 0)
        self.assertEqual(daily['automatic_category_stats']['oni']['roi'], 900)
        self.assertEqual(daily['legacy_records'], 1)
        for key in ['stats', 'category_stats', 'automatic_category_stats', 'historical', 'records', 'legacy_records']:
            self.assertEqual(data[key], all_venues[key], key)
        unpadded = self.client.get('/api/performance?date=20261014&stadium=4').json
        self.assertEqual(unpadded, data)

    def test_same_numbered_races_in_different_venues_remain_separate_and_duplicates_stay_deduplicated(self):
        self.rows = [record('20261014', 1, stadium='04', payout=500),
                     record('2026-10-14', 1, stadium='04', payout=1000),
                     record('20261014', 1, stadium='06', payout=200)]
        for stadium, settled, payout in [('', 2, 1200), ('04', 1, 1000), ('06', 1, 200)]:
            data = self.client.get('/api/performance', query_string={'date': '20261014', 'stadium': stadium}).json
            for scope in ['category_stats', 'automatic_category_stats']:
                gachi = data['daily'][scope]['gachi']
                self.assertEqual((gachi['settled'], gachi['payout']), (settled, payout))

    def test_all_venues_and_a_venue_without_records_are_distinct(self):
        self.rows = [record('20261014', 1, stadium='04')]
        for query in ['', '&stadium=']:
            daily = self.client.get('/api/performance?date=20261014' + query).json['daily']
            self.assertEqual((daily['stadium'], daily['venue']), ('', '全場'))
            self.assertEqual(daily['automatic_category_stats']['gachi']['settled'], 1)
        daily = self.client.get('/api/performance?date=20261014&stadium=24').json['daily']
        self.assertEqual((daily['stadium'], daily['venue']), ('24', '大村'))
        for category in daily['automatic_category_stats'].values():
            self.assertEqual((category['settled'], category['pending'], category['investment']), (0, 0, 0))
            self.assertIsNone(category['roi'])
            self.assertIsNone(category['hit_rate'])

    def test_invalid_venues_are_rejected_before_reading_or_writing(self):
        with mock.patch.object(core, 'save_ledger') as save:
            for stadium in ['00', '25', '004', '-1', 'abc', '4.0']:
                with self.subTest(stadium=stadium):
                    response = self.client.get('/api/performance', query_string={'date': '20261014', 'stadium': stadium})
                    self.assertEqual(response.status_code, 400)
                    self.assertFalse(response.json['ok'])
            self.load.assert_not_called()
            save.assert_not_called()


if __name__ == '__main__':
    unittest.main()

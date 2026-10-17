import unittest
from unittest import mock

import requests

import app as core
from venue_schedule import VenueScheduleCache


def page(rows='', date='20261017', stadium='04'):
    return ('<title>結果一覧｜BOAT RACE オフィシャルウェブサイト</title>'
            f'<a href="/owpc/pc/race/raceindex?jcd={stadium}&amp;hd={date}">レース一覧</a>'
            f'<table>{rows}</table>')


def result(race, combo='1-2-3', payout='¥900'):
    return f'<tr><td>{race}R</td><td>{combo}</td><td>{payout}</td></tr>'


class RaceStatusChecks(unittest.TestCase):
    def setUp(self):
        self.clock = 0
        self.cache = VenueScheduleCache(core.fetch_venue_race_status, ttl=60, stale_ttl=300,
                                        clock=lambda: self.clock, payload_field='results')
        mock.patch.object(core, '_venue_race_status', self.cache).start()
        self.get = mock.patch.object(core, 'get', return_value=page()).start()
        self.load = mock.patch.object(core, 'load_ledger').start()
        self.save = mock.patch.object(core, 'save_ledger').start()
        self.learn = mock.patch.object(core, 'learn_from_features').start()
        self.addCleanup(mock.patch.stopall)
        self.client = core.app.test_client()

    def test_confirmed_results_and_explicit_cancellations_are_separate_from_pending(self):
        self.get.return_value = page(result(2, '２－３－４', '￥１，９００') + result(1) + result(1)
                                     + result(3, '', '') + result(4, '中止', '')
                                     + result(5, 'レース不成立', '') + result(6, '1-1-2')
                                     + result(7, '1-2-3', '¥0') + result(13))
        response = self.client.get('/api/race_status?date=2026-10-17&stadium=4')
        self.assertEqual(response.status_code, 200)
        data = response.json
        self.assertEqual((data['date'], data['stadium']), ('20261017', '04'))
        self.assertEqual(data['settled_races'], [1, 2])
        self.assertEqual(data['cancelled_races'], [4, 5])
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.get.assert_called_once_with(core.BASE + 'resultlist?hd=20261017&jcd=04', timeout=15)
        self.load.assert_not_called()
        self.save.assert_not_called()
        self.learn.assert_not_called()

    def test_a_valid_unstarted_result_page_is_successful_without_inventing_results(self):
        data = self.client.get('/api/race_status?date=20261017&stadium=04').json
        self.assertTrue(data['ok'])
        self.assertEqual(data['settled_races'], [])
        self.assertEqual(data['cancelled_races'], [])

    def test_cache_is_shared_within_a_minute_then_refreshed_for_new_results(self):
        self.get.side_effect = [page(result(1)), page(result(1) + result(2))]
        for _ in range(3):
            response = self.client.get('/api/race_status?date=20261017&stadium=04')
            self.assertEqual(response.json['settled_races'], [1])
        self.assertEqual(self.get.call_count, 1)
        self.clock = 61
        data = self.client.get('/api/race_status?date=20261017&stadium=04').json
        self.assertEqual(data['settled_races'], [1, 2])
        self.assertEqual(self.get.call_count, 2)

    def test_cache_never_shares_results_across_dates_or_venues(self):
        self.get.side_effect = [page(result(1)), page(result(2), stadium='24'),
                                page(result(3), date='20261016')]
        for date, stadium, race in [('20261017', '04', 1), ('20261017', '24', 2), ('20261016', '04', 3)]:
            response = self.client.get('/api/race_status', query_string={'date': date, 'stadium': stadium})
            self.assertEqual(response.json['settled_races'], [race])
        self.assertEqual(self.get.call_count, 3)

    def test_recent_same_scope_snapshot_survives_a_failed_refresh_but_expires(self):
        self.get.side_effect = [page(result(1)), requests.Timeout(), requests.Timeout(), requests.Timeout()]
        self.client.get('/api/race_status?date=20261017&stadium=04')
        self.clock = 61
        stale = self.client.get('/api/race_status?date=20261017&stadium=04')
        self.assertEqual(stale.json['settled_races'], [1])
        self.assertTrue(stale.json['stale'])
        other = self.client.get('/api/race_status?date=20261016&stadium=04')
        self.assertEqual(other.status_code, 503)
        self.assertNotIn('settled_races', other.json)
        self.clock = 301
        expired = self.client.get('/api/race_status?date=20261017&stadium=04')
        self.assertEqual(expired.status_code, 503)
        self.assertNotIn('settled_races', expired.json)

    def test_wrong_pages_or_scopes_are_errors_not_twelve_pending_races(self):
        for html in ['<title>please wait</title>', page(result(1), date='20261016'),
                     page(result(1), stadium='24')]:
            with self.subTest(html=html):
                self.get.return_value = html
                response = self.client.get('/api/race_status?date=20261017&stadium=04')
                self.assertEqual(response.status_code, 502)
                self.assertFalse(response.json['ok'])
                self.assertNotIn('settled_races', response.json)

    def test_invalid_filters_are_rejected_before_network_or_ledger_access(self):
        for date, stadium in [('', '04'), ('20260230', '04'), ('20261017', ''),
                              ('20261017', '25'), ('20261017', '004'), ('x', '04')]:
            response = self.client.get('/api/race_status', query_string={'date': date, 'stadium': stadium})
            self.assertEqual(response.status_code, 400)
        self.get.assert_not_called()
        self.load.assert_not_called()
        self.save.assert_not_called()


if __name__ == '__main__':
    unittest.main()

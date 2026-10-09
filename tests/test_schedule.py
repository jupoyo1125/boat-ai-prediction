from concurrent.futures import ThreadPoolExecutor
import threading
import unittest
from unittest import mock

import requests

import app as core
from official_http import OfficialRequestSlots
from venue_schedule import VenueScheduleCache


HTML = '''<title>本日のレース｜BOAT RACE オフィシャルウェブサイト</title>
<a href="/owpc/pc/race/raceindex?jcd=01&amp;hd=20261009">桐生</a>
<a href="/owpc/pc/race/racelist?rno=1&amp;jcd=10&amp;hd=20261009">三国</a>
<a href="/owpc/pc/race/resultlist?jcd=10&amp;hd=20261009">同じ開催場</a>
<a href="/owpc/pc/race/raceindex?jcd=02&amp;hd=20261008">別の日付</a>
<a href="/owpc/pc/data/stadium?jcd=24">開催情報ではない場紹介</a>'''
VENUES = [{'stadium': '01', 'venue': '桐生'}, {'stadium': '10', 'venue': '三国'}]


class OfficialQueueTests(unittest.TestCase):
    def wait_for_queue(self, slots, count):
        with slots._condition:
            self.assertTrue(slots._condition.wait_for(
                lambda: len(slots._waiting) >= count, timeout=.2))

    def test_background_reacquisition_cannot_overtake_a_waiting_page_request(self):
        slots = OfficialRequestSlots(1)
        order = []
        with ThreadPoolExecutor(max_workers=1) as pool:
            with slots.slot():
                def page_request():
                    with slots.slot(timeout=1):
                        order.append('schedule')
                page = pool.submit(page_request)
                self.wait_for_queue(slots, 1)
            # A continuously running worker immediately asks for its next slot.
            with slots.slot(timeout=1):
                order.append('automatic-next-request')
            page.result(timeout=1)
        self.assertEqual(order, ['schedule', 'automatic-next-request'])

    def test_expired_waiter_is_removed_and_later_requests_keep_working(self):
        slots = OfficialRequestSlots(1)
        with ThreadPoolExecutor(max_workers=2) as pool:
            with slots.slot():
                def expired_request():
                    with slots.slot(timeout=.03):
                        self.fail('A busy slot should time out.')
                expired = pool.submit(expired_request)
                with self.assertRaises(requests.Timeout):
                    expired.result(timeout=1)
                def next_request():
                    with slots.slot(timeout=1):
                        return 'served'
                next_one = pool.submit(next_request)
                self.wait_for_queue(slots, 1)
            self.assertEqual(next_one.result(timeout=1), 'served')
        with slots.slot(timeout=.1):
            pass

    def test_queue_timeout_does_not_start_an_extra_official_connection(self):
        slots = OfficialRequestSlots(1)
        with mock.patch.object(core, '_official_slots', slots), \
                mock.patch.object(core, 'official_session') as session, \
                mock.patch.object(core.app.logger, 'disabled', True):
            with slots.slot():
                with self.assertRaises(requests.Timeout):
                    core.get('https://official.example/race', timeout=.03)
            session.assert_not_called()

    def test_http_failure_releases_its_slot(self):
        slots = OfficialRequestSlots(1)
        with mock.patch.object(core, '_official_slots', slots), \
                mock.patch.object(core, 'official_session') as session, \
                mock.patch.object(core.app.logger, 'disabled', True):
            session.return_value.get.side_effect = requests.ConnectionError('offline')
            with self.assertRaises(requests.ConnectionError):
                core.get('https://official.example/race', timeout=.03)
        with slots.slot(timeout=.03):
            pass


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.clock = 0
        self.cache = VenueScheduleCache(core.fetch_active_stadiums, clock=lambda: self.clock)
        patch = mock.patch.object(core, '_venue_schedules', self.cache)
        patch.start(); self.addCleanup(patch.stop)
        self.client = core.app.test_client()

    def test_automation_and_ui_share_confirmed_venues_without_cross_date_links(self):
        with mock.patch.object(core, 'get', return_value=HTML) as get:
            venues = core.get_active_stadiums('2026/10/09')
            self.assertEqual(venues, VENUES)
            venues[0]['venue'] = 'caller changed this'
            response = self.client.get('/api/schedule?date=20261009')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['venues'], VENUES)
        self.assertTrue(response.json['cached'])
        self.assertFalse(response.json['stale'])
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        get.assert_called_once()

    def test_failed_refresh_uses_only_a_recent_schedule_for_the_same_date(self):
        with mock.patch.object(core, 'get', return_value=HTML):
            core.get_active_stadiums('20261009')
        self.clock = 901
        with mock.patch.object(core, 'get', side_effect=requests.Timeout('busy')):
            fallback = self.client.get('/api/schedule?date=20261009')
            another_day = self.client.get('/api/schedule?date=20261008')
            self.clock = 3601
            expired = self.client.get('/api/schedule?date=20261009')
        self.assertEqual(fallback.status_code, 200)
        self.assertEqual(fallback.json['venues'], VENUES)
        self.assertTrue(fallback.json['stale'])
        for response in (another_day, expired):
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json['code'], 'official_busy')
            self.assertNotIn('venues', response.json)
            self.assertEqual(response.headers['Retry-After'], '5')

    def test_simultaneous_requests_fetch_once_and_a_waiter_has_a_deadline(self):
        started, release = threading.Event(), threading.Event()
        def loader(date):
            started.set()
            self.assertTrue(release.wait(1))
            return VENUES
        fetch = mock.Mock(side_effect=loader)
        cache = VenueScheduleCache(fetch, wait_timeout=.03)
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(cache.get, '20261009')
            self.assertTrue(started.wait(1))
            with self.assertRaises(requests.Timeout):
                cache.get('20261009')
            release.set()
            self.assertEqual(first.result(timeout=1)['venues'], VENUES)
        self.assertEqual(cache.get('20261009')['venues'], VENUES)
        fetch.assert_called_once_with('20261009')

    def test_simultaneous_successful_requests_share_the_same_fetch(self):
        started, release = threading.Event(), threading.Event()
        def loader(date):
            started.set()
            self.assertTrue(release.wait(1))
            return VENUES
        fetch = mock.Mock(side_effect=loader)
        cache = VenueScheduleCache(fetch)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(cache.get, '20261009')
            self.assertTrue(started.wait(1))
            second = pool.submit(cache.get, '20261009')
            release.set()
            results = [future.result(timeout=1) for future in (first, second)]
        self.assertEqual([result['venues'] for result in results], [VENUES, VENUES])
        fetch.assert_called_once()

    def test_a_confirmed_day_without_races_is_cached(self):
        with mock.patch.object(core, 'get', return_value=HTML.split('<a')[0]) as get:
            for _ in range(2):
                response = self.client.get('/api/schedule?date=20261009')
                self.assertEqual(response.json['count'], 0)
                self.assertTrue(response.json['ok'])
        get.assert_called_once()

    def test_error_page_is_not_cached_as_a_day_without_races(self):
        with mock.patch.object(core, 'get', side_effect=['<html>please wait</html>', HTML]) as get:
            self.assertEqual(self.client.get('/api/schedule?date=20261009').status_code, 502)
            response = self.client.get('/api/schedule?date=20261009')
        self.assertEqual(response.json['venues'], VENUES)
        self.assertEqual(get.call_count, 2)

    def test_invalid_dates_never_access_the_official_site(self):
        with mock.patch.object(core, 'get') as get:
            for date in ['20260230', '2026111', 'not-a-date']:
                self.assertEqual(self.client.get('/api/schedule?date=' + date).status_code, 400)
        get.assert_not_called()


if __name__ == '__main__':
    unittest.main()

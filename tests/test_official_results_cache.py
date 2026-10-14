from concurrent.futures import ThreadPoolExecutor
import copy
import threading
import unittest
from unittest import mock

import requests

import app as core
from official_results_cache import DailyResultsCache


RESULT = [{'stadium': '04', 'race': 1, 'combo': '123', 'payout': 990, 'excluded': None},
          {'stadium': '14', 'race': 1, 'combo': '456', 'payout': 2000, 'excluded': None}]
HTML = '<table><tr><td>1R</td><td>1-2-3</td><td>¥990</td></tr></table>'


class DailyCacheChecks(unittest.TestCase):
    def test_concurrent_venues_share_one_download_and_cannot_mutate_the_cache(self):
        started, release = threading.Event(), threading.Event()
        def load(date, **kwargs):
            started.set()
            self.assertTrue(release.wait(1))
            return RESULT
        loader = mock.Mock(side_effect=load)
        cache = DailyResultsCache(loader)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(cache.get, '20261013', timeout=1)
            self.assertTrue(started.wait(1))
            second = pool.submit(cache.get, '20261013', timeout=1)
            release.set()
            a, b = first.result(1), second.result(1)
        a[0]['combo'] = '654'
        self.assertEqual(b[0]['combo'], '123')
        self.assertEqual(cache.get('20261013')[0]['combo'], '123')
        self.assertEqual(loader.call_count, 1)

    def test_waiter_can_time_out_while_the_valid_download_finishes(self):
        started, release = threading.Event(), threading.Event()
        def load(date, **kwargs):
            started.set(); self.assertTrue(release.wait(1)); return RESULT
        cache = DailyResultsCache(load)
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(cache.get, '20261013', timeout=1)
            self.assertTrue(started.wait(1))
            with self.assertRaises(requests.Timeout):
                cache.get('20261013', timeout=.02)
            release.set()
            self.assertEqual(first.result(1), RESULT)
        self.assertEqual(cache.get('20261013'), RESULT)

    def test_failed_downloads_have_a_bounded_cooldown_without_fabricating_records(self):
        clock = [0]
        loader = mock.Mock(side_effect=[requests.ConnectionError('offline'), RESULT])
        cache = DailyResultsCache(loader, retry_seconds=60, clock=lambda: clock[0])
        for _ in range(2):
            with self.assertRaises(requests.ConnectionError):
                cache.get('20261013')
        self.assertEqual(loader.call_count, 1)
        clock[0] = 61
        self.assertEqual(cache.get('20261013'), RESULT)
        self.assertEqual(loader.call_count, 2)


class SharedHistoryChecks(unittest.TestCase):
    def setUp(self):
        core.historical_stats.cache_clear()
        self.addCleanup(core.historical_stats.cache_clear)
        patch = mock.patch.object(core.app.logger, 'disabled', True)
        patch.start(); self.addCleanup(patch.stop)

    def test_three_daily_files_cover_multiple_venues_with_the_same_statistics(self):
        loader = mock.Mock(return_value=copy.deepcopy(RESULT))
        cache = DailyResultsCache(loader)
        with mock.patch.object(core, '_daily_results', cache), mock.patch.object(core, 'get') as pages:
            heiwajima = core.historical_stats('04', 3, '20261013')
            naruto = core.historical_stats('14', 3, '20261013')
        self.assertEqual(loader.call_count, 3)
        pages.assert_not_called()
        self.assertEqual(heiwajima['races'], 3)
        self.assertEqual(heiwajima['first_win_rate']['1'], 100)
        self.assertEqual(heiwajima['avg_payout'], 990)
        self.assertEqual(naruto['first_win_rate']['4'], 100)
        self.assertEqual(naruto['avg_payout'], 2000)
        self.assertEqual({call.args[0] for call in loader.call_args_list}, {'20261011','20261012','20261013'})

    def test_invalid_or_unavailable_archives_fall_back_to_confirmed_venue_pages(self):
        with mock.patch.object(core._daily_results, 'get', side_effect=ValueError('wrong date')), \
                mock.patch.object(core, 'get', return_value=HTML) as pages:
            stats = core.historical_stats('04', 3, '20261013')
        self.assertEqual(stats['races'], 3)
        self.assertEqual(stats['top_combos'], [('123', 3)])
        self.assertEqual(pages.call_count, 3)
        self.assertTrue(all('resultlist' in call.args[0] for call in pages.call_args_list))

    def test_unstarted_archive_request_expires_without_opening_a_connection(self):
        release = threading.Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(release.wait, 1)
            with mock.patch.object(core, '_official_pool', pool), \
                    mock.patch.object(core._daily_results, 'get') as get:
                request = core.submit_daily_results('20261013', 0)
                release.set()
                with self.assertRaises(requests.Timeout):
                    request.result(1)
                get.assert_not_called()

    def test_current_or_future_daily_files_are_rejected_before_network_access(self):
        with mock.patch.object(core, 'official_session') as session:
            for date in ['29991015','29991016']:
                with self.assertRaises(ValueError):
                    core.download_daily_results(date)
        session.assert_not_called()


if __name__ == '__main__':
    unittest.main()

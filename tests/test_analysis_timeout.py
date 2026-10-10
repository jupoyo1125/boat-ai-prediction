from concurrent.futures import ThreadPoolExecutor
import copy
import threading
import time
import unittest
from unittest import mock

import requests

import app as core
import model


RESULT = '<table><tr><td>1R</td><td>1-2-3</td><td>¥1000</td></tr></table>'


class AnalysisTimeoutTests(unittest.TestCase):
    def setUp(self):
        core.historical_stats.cache_clear()
        self.addCleanup(core.historical_stats.cache_clear)
        patch = mock.patch.object(core.app.logger, 'disabled', True)
        patch.start(); self.addCleanup(patch.stop)
        self.client = core.app.test_client()

    def test_busy_history_returns_before_worker_timeout_and_cancels_queued_requests(self):
        release = threading.Event()
        urls = []

        def stalled(url, **kwargs):
            urls.append(url)
            release.wait(1)
            return ''

        with ThreadPoolExecutor(max_workers=3) as pool:
            with mock.patch.object(core, '_official_pool', pool), \
                    mock.patch.object(core, 'OFFICIAL_PAGE_WAIT_SECONDS', .03), \
                    mock.patch.object(core, 'get', side_effect=stalled), \
                    mock.patch.object(core, 'load_model') as load, \
                    mock.patch.object(core, 'save_ledger') as save:
                try:
                    started = time.monotonic()
                    response = self.client.get('/api/analyze?date=20261010&stadium=07&race=10')
                    self.assertLess(time.monotonic() - started, .5)
                    self.assertEqual(response.status_code, 503)
                    self.assertEqual(response.json['code'], 'analysis_timeout')
                    self.assertEqual(response.headers['Retry-After'], '5')
                    self.assertEqual(response.headers['Cache-Control'], 'no-store')
                    load.assert_not_called(); save.assert_not_called()
                finally:
                    release.set()
        self.assertEqual(len(urls), 3)
        self.assertFalse(any('resultlist' in url for url in urls))

    def test_stalled_race_inputs_are_bounded_even_when_history_is_cached(self):
        release = threading.Event()
        with ThreadPoolExecutor(max_workers=3) as pool:
            with mock.patch.object(core, '_official_pool', pool), \
                    mock.patch.object(core, 'ANALYSIS_WAIT_SECONDS', .03), \
                    mock.patch.object(core, 'historical_stats', return_value={'races': 0}), \
                    mock.patch.object(core, 'load_model', return_value=copy.deepcopy(model.DEFAULT)), \
                    mock.patch.object(core, 'get', side_effect=lambda *a, **k: release.wait(1) or ''), \
                    mock.patch.object(core, 'analyze') as score:
                try:
                    started = time.monotonic()
                    with self.assertRaises(requests.Timeout):
                        core.predict_race('20261010', '07', 10)
                    self.assertLess(time.monotonic() - started, .5)
                    score.assert_not_called()
                finally:
                    release.set()

    def test_expired_queued_page_never_opens_an_official_connection(self):
        release = threading.Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(release.wait, 1)
            with mock.patch.object(core, '_official_pool', pool), \
                    mock.patch.object(core, 'get') as get:
                page = core.submit_official_page('https://official.example/race', time.monotonic() - 1)
                release.set()
                with self.assertRaises(requests.Timeout):
                    page.result(timeout=.5)
                get.assert_not_called()

    def test_transient_history_failure_is_not_cached_as_partial_or_empty_history(self):
        with ThreadPoolExecutor(max_workers=3) as pool:
            with mock.patch.object(core, '_official_pool', pool), \
                    mock.patch.object(core, 'get', side_effect=requests.ConnectionError('offline')):
                with self.assertRaises(requests.ConnectionError):
                    core.historical_stats('07', 3, '20261009')
        with ThreadPoolExecutor(max_workers=3) as pool:
            with mock.patch.object(core, '_official_pool', pool), \
                    mock.patch.object(core, 'get', return_value=RESULT) as get:
                result = core.historical_stats('07', 3, '20261009')
                self.assertEqual(result['races'], 3)
                self.assertEqual(result['dates'], 3)
                self.assertEqual(result['first_win_rate']['1'], 100)
                self.assertEqual(get.call_count, 3)
                self.assertEqual(core.historical_stats('07', 3, '20261009'), result)
                self.assertEqual(get.call_count, 3)


if __name__ == '__main__':
    unittest.main()

import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from itertools import permutations
import os
from pathlib import Path
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import threads_publisher as tp


def forecast():
    bets = ['-'.join(map(str, p)) for p in permutations(range(1, 7), 3)]
    return {
        'id': 'auto:20261015:04:02', 'date': '20261015', 'stadium': '04', 'race': 2,
        'prediction_origin': 'automatic', 'prediction_saved_at': '2026-10-15T10:16:47+09:00',
        'exhibition_detected_at': '2026-10-15T10:16:25+09:00',
        'scheduled_deadline': '2026-10-15T10:30:00+09:00', 'settled': False,
        'prediction': {'main': 1, 'second': 4, 'hole': 3,
                       'boats': [{'boat': n, 'name': f'選手　{n} 東京/東京 30歳/52kg',
                                  'exhibition': 6.7, 'exhibition_st': 0.0 if n == 1 else .1}
                                 for n in range(1, 7)]},
        'bets': [{'bet': b, 'category': c} for c, group in
                 [('gachi', bets[:10]), ('roman', bets[30:40]), ('oni', bets[:3])]
                 for b in group],
    }


class MemoryStore(tp.PostStore):
    """Use the real state transitions with an isolated transactional store."""
    def __init__(self):
        self.state = {'version': 1, 'posts': {}}
        self.lock = threading.Lock()

    def mutate(self, change):
        with self.lock:
            value = copy.deepcopy(self.state)
            result = change(value)
            self.state = value
            return result

    def read(self):
        return copy.deepcopy(self.state)


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.row = forecast()
        self.now = datetime(2026, 10, 15, 10, 20, tzinfo=tp.JST)
        self.config = tp.Config('20261015', 2, 'not-a-real-token')
        self.store = MemoryStore()
        self.client = mock.Mock()
        self.client.create.return_value = '123'
        self.client.publish.return_value = '456'
        self.client.permalink.return_value = 'https://www.threads.com/@example/post/example'
        self.core = mock.Mock()
        self.core.load_ledger.return_value = [self.row]
        patch = mock.patch('automation.configured', return_value=True)
        patch.start(); self.addCleanup(patch.stop)
        self.publisher = tp.Publisher(self.core, self.config, self.store, self.client,
                                      clock=lambda: self.now)

    def state(self):
        return self.store.read()['posts'].get(self.row['id'])

    def test_body_preserves_all_categories_without_any_site_link(self):
        text = tp.caption(self.row)
        self.assertIn('独自作成のAI予想サイトでは、', text)
        self.assertIn('ガチガチ10点', text)
        self.assertIn('ロマン砲10点', text)
        self.assertIn('鬼しぼり3点', text)
        for bet in self.row['bets']:
            self.assertIn(bet['bet'], text)
        self.assertIn('本命①選手1', text)
        self.assertIn('ST0.00', text)
        self.assertIn('10:16保存／締切10:30', text)
        self.assertNotIn('http', text)
        self.assertNotIn('onrender', text)
        self.assertLessEqual(len(text), 500)

    def test_zero_and_flying_exhibition_st_are_preserved(self):
        self.row['prediction']['boats'][2]['exhibition_st'] = -.01
        self.assertTrue(tp.complete_exhibition(self.row['prediction']))
        self.assertIn('F0.01', tp.caption(self.row))

    def test_given_name_is_kept_with_ascii_spaces(self):
        self.row['prediction']['boats'][0]['name'] = '松田 大志郎 福岡/福岡 38歳/52kg'
        self.assertIn('本命①松田大志郎', tp.caption(self.row))

    def test_disabled_status_does_not_reveal_a_configured_token(self):
        with mock.patch.dict(os.environ, {'BOAT_THREADS_ENABLED': '0',
                                         'THREADS_ACCESS_TOKEN': 'not-a-real-token'}):
            result = tp.public_status()
            self.assertFalse(result['enabled'])
            self.assertNotIn('not-a-real-token', str(result))

    def test_incomplete_exhibition_cannot_publish(self):
        for value in [None, float('nan'), True]:
            with self.subTest(value=value):
                self.row['prediction']['boats'][0]['exhibition_st'] = value
                self.publisher.run_once()
                self.client.publish.assert_not_called()
        self.assertIsNone(self.state())

    def test_manual_other_venue_past_day_and_previously_posted_race_are_excluded(self):
        variants = [{'prediction_origin': 'manual'}, {'stadium': '05'}, {'date': '20261014'},
                    {'race': 1, 'id': 'auto:20261015:04:01'}, {'settled': True},
                    {'learned': True}, {'actual_combo': '123'}]
        for changes in variants:
            with self.subTest(changes=changes):
                row = forecast(); row.update(changes)
                self.assertIsNone(tp.candidate(row, self.config, self.now))

    def test_explicit_scope_and_token_are_required(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(tp.configuration())
            os.environ.update(BOAT_THREADS_ENABLED='1', BOAT_THREADS_DATE='20261015',
                              BOAT_THREADS_FROM_RACE='2')
            self.assertIsNone(tp.configuration())
            os.environ['THREADS_ACCESS_TOKEN'] = 'not-a-real-token'
            self.assertEqual(tp.configuration().first_race, 2)
            self.assertNotIn('not-a-real-token', repr(tp.configuration()))
            os.environ['BOAT_THREADS_DATE'] = '20260230'
            self.assertIsNone(tp.configuration())

    def test_save_notify_performs_no_api_or_storage_work(self):
        with mock.patch('threads_publisher.state_connection') as connection, \
                mock.patch('threads_publisher.requests.Session') as session:
            tp.wake()
            connection.assert_not_called()
            session.assert_not_called()

    def test_restart_and_repeat_wakes_publish_only_once(self):
        self.publisher.run_once()
        self.assertEqual(self.state()['state'], 'published')
        restarted = tp.Publisher(self.core, self.config, self.store, self.client, clock=lambda: self.now)
        restarted.run_once(); self.publisher.run_once()
        self.client.publish.assert_called_once_with('123')

    def test_two_workers_cannot_claim_the_same_forecast(self):
        self.store.enqueue([tp.candidate(self.row, self.config, self.now)])
        with ThreadPoolExecutor(max_workers=2) as pool:
            entries = list(pool.map(lambda _: self.store.claim(self.config, self.now), range(2)))
        self.assertEqual(sum(entry is not None for entry in entries), 1)

    def test_worker_that_lost_its_lease_cannot_publish(self):
        self.store.enqueue([tp.candidate(self.row, self.config, self.now)])
        old = self.store.claim(self.config, self.now)
        current = self.store.claim(self.config, self.now + tp.LEASE + timedelta(seconds=1))
        self.assertNotEqual(old['claim'], current['claim'])
        self.assertFalse(self.store.update(old, ('creating',), state='publishing'))

    def test_wrong_account_never_creates_or_publishes(self):
        self.client.verify_account.side_effect = tp.WrongAccount('wrong account')
        self.publisher.run_once()
        self.assertEqual(self.state()['state'], 'error')
        self.client.create.assert_not_called()
        self.client.publish.assert_not_called()

    def test_preparation_network_errors_retry_at_most_three_times(self):
        self.client.verify_account.side_effect = TimeoutError('secret-must-not-be-logged')
        for _ in range(4):
            self.publisher.run_once()
        self.assertEqual(self.client.verify_account.call_count, 3)
        self.assertEqual(self.state()['state'], 'error')
        self.assertNotIn('secret-must-not-be-logged', str(self.state()))
        self.client.publish.assert_not_called()

    def test_publish_timeout_is_not_automatically_retried(self):
        self.client.publish.side_effect = TimeoutError('unknown outcome')
        self.publisher.run_once(); self.publisher.run_once()
        self.assertEqual(self.state()['state'], 'uncertain')
        self.client.publish.assert_called_once()

    def test_db_failure_after_success_does_not_produce_a_duplicate_on_restart(self):
        original = self.store.update
        def fail_after_publish(entry, expected, **values):
            if values.get('state') == 'published':
                raise OSError('storage unavailable')
            return original(entry, expected, **values)
        with mock.patch.object(self.store, 'update', side_effect=fail_after_publish):
            with self.assertRaises(OSError):
                self.publisher.run_once()
        self.assertEqual(self.state()['state'], 'publishing')
        self.now += tp.LEASE + timedelta(seconds=1)
        self.publisher.run_once()
        self.assertEqual(self.state()['state'], 'uncertain')
        self.client.publish.assert_called_once()

    def test_deadline_is_checked_again_after_container_preparation(self):
        def slow_container(text):
            self.now = tp.timestamp(self.row['scheduled_deadline']) - timedelta(seconds=119)
            return '123'
        self.client.create.side_effect = slow_container
        self.publisher.run_once()
        self.assertEqual(self.state()['state'], 'expired')
        self.client.publish.assert_not_called()

    def test_an_expired_scope_or_maintenance_stops_all_processing(self):
        self.now += timedelta(days=1)
        self.publisher.run_once()
        self.core.load_ledger.assert_not_called()
        self.now -= timedelta(days=1)
        with mock.patch('automation.configured', return_value=False):
            self.publisher.run_once()
        self.client.publish.assert_not_called()

    def test_forecast_text_is_frozen_even_when_the_source_is_reordered(self):
        first = tp.candidate(self.row, self.config, self.now)
        self.store.enqueue([first])
        self.row['bets'].reverse()
        self.store.enqueue([tp.candidate(self.row, self.config, self.now)])
        self.publisher.run_once()
        self.client.create.assert_called_once_with(first['text'])


class ClientTests(unittest.TestCase):
    def test_token_stays_in_header_and_target_account_is_checked(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = {'id': '789', 'username': tp.USERNAME}
        session = mock.Mock()
        session.request.return_value = response
        client = tp.ThreadsClient('not-a-real-token', session)
        client.verify_account()
        args, kwargs = session.request.call_args
        self.assertEqual(args, ('GET', tp.API + '/me'))
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer not-a-real-token')
        self.assertNotIn('access_token', kwargs['params'])
        self.assertFalse(kwargs['allow_redirects'])
        response.json.return_value['username'] = 'another_account'
        with self.assertRaises(tp.WrongAccount):
            client.verify_account()


if __name__ == '__main__':
    unittest.main()

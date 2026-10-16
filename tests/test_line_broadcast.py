from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from flask import Flask
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import line_bot as lb
import line_broadcast as broadcast
import test_line_bot as fixtures


class MemoryStore(broadcast.BroadcastStore):
    def __init__(self):
        self.state = {'version': 1, 'days': {}}
        self.lock = threading.Lock()

    mutate = fixtures.MemoryStore.mutate


class BroadcastTests(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {
            'BOAT_LINE_BROADCAST_ENABLED': '1',
            'BOAT_LINE_SAMPLE_RACE': '20261017:04:5',
        })
        env.start()
        self.addCleanup(env.stop)
        self.now = datetime(2026, 10, 17, 10, 10, tzinfo=lb.JST)
        self.row = fixtures.forecast(date='20261017', race=5)
        self.rows = [self.row]
        self.core = SimpleNamespace(
            app=Flask(__name__), STADIUMS={'04': '平和島', '15': '丸亀'},
            load_ledger=mock.Mock(side_effect=lambda: deepcopy(self.rows)),
            load_line_record=mock.Mock(side_effect=lambda record_id: next(
                (deepcopy(row) for row in self.rows if row.get('id') == record_id), None)),
        )
        self.store = MemoryStore()
        self.store.set_subscription(fixtures.USER, 'adult', self.now)
        self.config = lb.Config('not-a-secret', 'not-a-token', 'live')
        self.sender = mock.Mock()
        self.sender.free_quota_available.return_value = True
        self.sender.multicast.return_value = 'accepted'
        self.factory = mock.Mock(return_value=self.sender)
        self.publisher = self.new_publisher()

    def new_publisher(self):
        return broadcast.Publisher(
            self.core, store=self.store, clock=lambda: self.now,
            config_provider=lambda: self.config, client_factory=self.factory,
            record_loader=self.core.load_line_record)

    def sample(self):
        return lb.frozen_sample(self.row, self.core.STADIUMS, self.now)

    def test_saved_designated_prediction_broadcasts_without_any_incoming_message(self):
        self.assertEqual(self.publisher.run_once(), 'accepted')
        payload, key = self.sender.multicast.call_args.args
        self.assertEqual(payload, {'to': [fixtures.USER], 'messages': [{'type': 'text', 'text': lb.format_sample(self.sample())}]})
        self.assertEqual(len(key), 36)
        self.assertIn('10/17 平和島5R', payload['messages'][0]['text'])
        self.assertIn('ガチガチ｜10点', payload['messages'][0]['text'])
        self.assertIn('ロマン砲｜10点', payload['messages'][0]['text'])
        self.assertIn('鬼しぼり｜3点', payload['messages'][0]['text'])
        self.assertEqual(self.store.sample(self.now), self.sample())
        self.assertEqual(self.store.read_day(self.now)['events'], {})
        self.assertNotIn('not-a-token', json.dumps(self.store.state))

    def test_repeated_scans_and_restart_do_not_repeat_an_accepted_broadcast(self):
        self.publisher.run_once()
        self.publisher.run_once()
        self.new_publisher().run_once()
        self.sender.multicast.assert_called_once()
        self.sender.verify_account.assert_called_once()

    def test_disabled_test_mode_or_missing_configuration_cannot_broadcast(self):
        for mode in ('off', 'test', 'unconfigured'):
            with self.subTest(mode=mode):
                self.config = lb.Config('s', 't', 'test', fixtures.USER)
                if mode == 'off':
                    os.environ['BOAT_LINE_BROADCAST_ENABLED'] = '0'
                if mode == 'unconfigured':
                    self.config = None
                self.assertEqual(self.publisher.run_once(), 'disabled')
                self.assertEqual(self.store.state['days'], {})
                self.factory.assert_not_called()
                self.core.load_line_record.assert_not_called()
                os.environ['BOAT_LINE_BROADCAST_ENABLED'] = '1'

    def test_only_the_configured_jst_day_and_race_can_be_sent(self):
        for value in ('', '20261016:04:5', '20261018:04:5', '20261017:99:5', '20261017:04:13'):
            with self.subTest(value=value):
                os.environ['BOAT_LINE_SAMPLE_RACE'] = value
                self.assertEqual(self.publisher.run_once(), 'waiting_for_target')
                self.assertEqual(self.store.state['days'], {})
                self.core.load_line_record.assert_not_called()
                self.sender.multicast.assert_not_called()

    def test_other_saved_race_does_not_replace_the_target_and_later_target_is_sent(self):
        self.rows = [fixtures.forecast(date='20261017', stadium='15', deadline='10:20:00')]
        self.assertEqual(self.publisher.run_once(), 'waiting_for_prediction')
        self.assertIsNone(self.store.sample(self.now))
        self.sender.multicast.assert_not_called()
        self.rows.append(self.row)
        self.assertEqual(self.publisher.run_once(), 'accepted')
        self.assertEqual(self.store.sample(self.now)['id'], self.row['id'])

    def test_invalid_or_settled_source_cannot_be_broadcast(self):
        for field, value in [('prediction_origin', 'manual'), ('settled', True),
                             ('learned', True), ('actual_combo', '123'),
                             ('exhibition_detected_at', '2026-10-17T10:11:00+09:00'),
                             ('prediction_saved_at', '2026-10-17T10:30:00+09:00')]:
            with self.subTest(field=field):
                row = deepcopy(self.row)
                row[field] = value
                self.rows = [row]
                self.assertEqual(self.publisher.run_once(), 'waiting_for_prediction')
                self.sender.multicast.assert_not_called()

    def test_cutoff_or_next_day_stops_first_delivery(self):
        self.now = self.now.replace(minute=28, second=30)
        self.assertEqual(self.publisher.run_once(), 'closed')
        self.assertIsNone(self.store.sample(self.now))
        self.now += timedelta(days=1)
        self.assertEqual(self.publisher.run_once(), 'waiting_for_target')
        self.sender.multicast.assert_not_called()

    def test_existing_reply_sample_is_shared_with_broadcast(self):
        bot = lb.SampleBot(self.core, self.store, lambda: self.now)
        text, _ = bot.answer('買い目')
        self.publisher.run_once()
        self.assertEqual(self.sender.multicast.call_args.args[0]['messages'][0]['text'], text)

    def test_different_pinned_sample_cannot_be_replaced_or_sent(self):
        other = lb.frozen_sample(fixtures.forecast(date='20261017', stadium='15'),
                                 self.core.STADIUMS, self.now)
        self.store.pin(other, self.now)
        self.assertEqual(self.publisher.run_once(), 'different_sample_pinned')
        self.assertEqual(self.store.sample(self.now), other)
        self.sender.multicast.assert_not_called()

    def test_changed_source_after_reply_is_not_rebuilt_or_broadcast(self):
        self.store.pin(self.sample(), self.now)
        self.row['bets'] = self.row['bets'][:-1]
        self.assertEqual(self.publisher.run_once(), 'source_changed')
        self.sender.multicast.assert_not_called()

    def test_quota_unavailable_leaves_the_sample_unpinned(self):
        self.sender.free_quota_available.return_value = False
        self.assertEqual(self.publisher.run_once(), 'quota_blocked')
        self.assertIsNone(self.store.sample(self.now))
        self.sender.multicast.assert_not_called()

    def test_wrong_account_or_unknown_quota_never_sends(self):
        self.sender.verify_account.side_effect = lb.LineError('wrong account')
        with self.assertRaises(lb.LineError):
            self.publisher.run_once()
        self.sender.free_quota_available.assert_not_called()
        self.sender.verify_account.side_effect = None
        self.sender.free_quota_available.side_effect = lb.LineError('unknown quota')
        with self.assertRaises(lb.LineError):
            self.publisher.run_once()
        self.sender.multicast.assert_not_called()

    def test_closing_during_api_checks_cannot_pin_or_send_a_late_sample(self):
        def quota(_recipient_count):
            self.now = self.now.replace(minute=28, second=30)
            return True
        self.sender.free_quota_available.side_effect = quota
        self.assertEqual(self.publisher.run_once(), 'busy')
        self.assertIsNone(self.store.sample(self.now))
        self.sender.multicast.assert_not_called()

    def test_midnight_during_api_checks_does_not_pin_yesterdays_sample(self):
        def quota(_recipient_count):
            self.now += timedelta(days=1)
            return True
        self.sender.free_quota_available.side_effect = quota
        self.assertEqual(self.publisher.run_once(), 'busy')
        self.assertIsNone(self.store.sample(self.now))
        self.sender.multicast.assert_not_called()

    def test_source_change_during_api_checks_cancels_before_http(self):
        def quota(_recipient_count):
            self.row['bets'] = self.row['bets'][:-1]
            return True
        self.sender.free_quota_available.side_effect = quota
        self.assertEqual(self.publisher.run_once(), 'cancelled')
        self.sender.multicast.assert_not_called()
        self.assertEqual(self.store.read_day(self.now)['broadcast']['status'], 'cancelled')

    def test_disabled_or_retargeted_during_api_checks_cancels_before_http(self):
        for key, value in [('BOAT_LINE_BROADCAST_ENABLED', '0'),
                           ('BOAT_LINE_SAMPLE_RACE', '20261017:15:1')]:
            with self.subTest(key=key):
                self.store = MemoryStore()
                self.store.set_subscription(fixtures.USER, 'adult', self.now)
                def quota(_recipient_count):
                    os.environ[key] = value
                    return True
                self.sender.free_quota_available.side_effect = quota
                self.assertEqual(self.new_publisher().run_once(), 'cancelled')
                self.sender.multicast.assert_not_called()
                os.environ['BOAT_LINE_BROADCAST_ENABLED'] = '1'
                os.environ['BOAT_LINE_SAMPLE_RACE'] = '20261017:04:5'

    def test_storage_failure_after_claim_cannot_send_and_preserves_retry_key(self):
        self.core.load_line_record.side_effect = [deepcopy(self.row), RuntimeError('storage failed')]
        with self.assertRaises(RuntimeError):
            self.publisher.run_once()
        self.sender.multicast.assert_not_called()
        key = self.store.read_day(self.now)['broadcast']['retry_key']
        self.core.load_line_record.side_effect = lambda record_id: deepcopy(self.row)
        self.now += timedelta(seconds=46)
        self.new_publisher().run_once()
        self.assertEqual(self.sender.multicast.call_args.args[1], key)

    def test_ambiguous_delivery_retries_only_with_the_same_key_and_frozen_payload(self):
        self.sender.multicast.side_effect = ['retry', 'accepted']
        self.assertEqual(self.publisher.run_once(), 'retry')
        original = deepcopy(self.sender.multicast.call_args.args)
        self.assertEqual(self.publisher.run_once(), 'busy')
        self.now += timedelta(seconds=20)
        self.assertEqual(self.new_publisher().run_once(), 'accepted')
        self.assertEqual(self.sender.multicast.call_args.args, original)
        self.new_publisher().run_once()
        self.assertEqual(self.sender.multicast.call_count, 2)

    def test_retries_are_bounded_and_do_not_run_after_cutoff(self):
        self.sender.multicast.return_value = 'retry'
        for _ in range(3):
            self.publisher.run_once()
            self.now += timedelta(seconds=45)
        self.assertEqual(self.publisher.run_once(), 'unknown')
        self.assertEqual(self.sender.multicast.call_count, 3)
        self.now = self.now.replace(minute=28, second=30)
        self.publisher.run_once()
        self.assertEqual(self.sender.multicast.call_count, 3)

    def test_retry_key_is_never_reused_after_the_short_retry_window(self):
        self.sender.multicast.return_value = 'retry'
        self.publisher.run_once()
        self.now += timedelta(minutes=10)
        self.publisher.run_once()
        self.assertEqual(self.store.read_day(self.now)['broadcast']['status'], 'unknown')
        self.sender.multicast.assert_called_once()

    def test_unresolved_first_attempt_is_not_retried_at_the_cutoff(self):
        self.sender.multicast.return_value = 'retry'
        self.publisher.run_once()
        self.now = self.now.replace(minute=28, second=30)
        self.assertEqual(self.new_publisher().run_once(), 'closed')
        self.sender.multicast.assert_called_once()

    def test_concurrent_workers_can_only_claim_one_first_broadcast(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: self.new_publisher().run_once(), range(8)))
        self.sender.multicast.assert_called_once()
        self.assertEqual(self.store.read_day(self.now)['broadcast']['status'], 'accepted')

    def test_failed_request_is_terminal_even_after_restart(self):
        self.sender.multicast.return_value = 'failed'
        self.publisher.run_once()
        self.now += timedelta(seconds=60)
        self.assertEqual(self.new_publisher().run_once(), 'failed')
        self.sender.multicast.assert_called_once()

    def test_late_failure_cannot_undo_acceptance(self):
        entry = self.store.claim_broadcast(self.sample(), self.now)
        self.store.finish_broadcast(entry, self.now, 'accepted')
        self.store.finish_broadcast(entry, self.now, 'retry')
        self.assertEqual(self.store.read_day(self.now)['broadcast']['status'], 'accepted')

    def test_public_status_reports_future_target_without_tokens_or_user_ids(self):
        self.core.app.add_url_rule('/existing_status', endpoint='status',
                                   view_func=lambda: 'existing status')
        with mock.patch('line_broadcast.enabled_config', return_value=self.config):
            broadcast.register_broadcast_routes(self.core)
            response = self.core.app.test_client().get('/api/line_publishing')
        self.assertEqual(self.core.app.test_client().get('/existing_status').text, 'existing status')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['target']['id'], 'auto:20261017:04:05')
        self.assertTrue(response.json['free_plan_only'])
        self.assertFalse(response.json['requires_user_message'])
        self.assertNotIn('not-a-token', response.get_data(as_text=True))
        self.assertEqual(response.headers['Cache-Control'], 'no-store')


class BroadcastHttpTests(unittest.TestCase):
    def response(self, status, payload=None, headers=None):
        response = mock.Mock(status_code=status, headers=headers or {})
        response.json.return_value = payload
        return response

    def test_only_known_free_plan_with_remaining_quota_is_allowed(self):
        client = broadcast.BroadcastClient('not-a-token')
        for quota, usage, allowed in [
            ({'type': 'limited', 'value': 200}, {'totalUsage': 0}, True),
            ({'type': 'limited', 'value': 200}, {'totalUsage': 199}, True),
            ({'type': 'limited', 'value': 200}, {'totalUsage': 200}, False),
            ({'type': 'limited', 'value': 5000}, {'totalUsage': 0}, False),
            ({'type': 'none'}, {'totalUsage': 0}, False),
            ({'type': 'limited', 'value': '200'}, {'totalUsage': 0}, False),
            ({'type': 'limited', 'value': 200}, {'totalUsage': False}, False),
            ({'type': 'limited', 'value': 200}, {'totalUsage': -1}, False),
        ]:
            with self.subTest(quota=quota, usage=usage):
                with mock.patch('line_broadcast.requests.get', side_effect=[
                    self.response(200, quota), self.response(200, usage)]) as get:
                    self.assertEqual(client.free_quota_available(), allowed)
                    self.assertEqual(get.call_count, 2)
                    self.assertFalse(get.call_args.kwargs['allow_redirects'])

    def test_quota_http_or_json_failure_is_not_an_allow(self):
        client = broadcast.BroadcastClient('not-a-token')
        for response in [self.response(403), self.response(200, None)]:
            with mock.patch('line_broadcast.requests.get', return_value=response):
                with self.assertRaises(lb.LineError):
                    client.free_quota_available()

    def test_broadcast_endpoint_retry_key_and_acceptance_contract(self):
        client = broadcast.BroadcastClient('not-a-token')
        payload = {'to': [fixtures.USER], 'messages': [{'type': 'text', 'text': 'test sample'}]}
        for status, headers, expected in [
            (200, {}, 'accepted'),
            (409, {'x-line-accepted-request-id': 'accepted-request'}, 'accepted'),
            (409, {}, 'failed'), (400, {}, 'failed'), (429, {}, 'failed'),
            (302, {}, 'failed'), (500, {}, 'retry'), (503, {}, 'retry'),
        ]:
            with self.subTest(status=status, headers=headers):
                with mock.patch('line_broadcast.requests.post', return_value=self.response(status, headers=headers)) as post:
                    self.assertEqual(client.multicast(payload, 'same-uuid'), expected)
                    self.assertEqual(post.call_args.args[0], 'https://api.line.me/v2/bot/message/multicast')
                    self.assertEqual(post.call_args.kwargs['headers']['X-Line-Retry-Key'], 'same-uuid')
                    self.assertEqual(post.call_args.kwargs['json'], payload)
                    self.assertFalse(post.call_args.kwargs['allow_redirects'])
        with mock.patch('line_broadcast.requests.post', side_effect=requests.Timeout('hidden')):
            self.assertEqual(client.multicast(payload, 'same-uuid'), 'retry')


@unittest.skipUnless(os.getenv('LINE_TEST_DATABASE_URL'), 'isolated PostgreSQL test database not configured')
class PostgresBroadcastTests(unittest.TestCase):
    def setUp(self):
        fixtures.PostgresLineStoreTests.setUp(self)
        broadcast.BroadcastStore().set_subscription(fixtures.USER, 'adult', self.now)

    def test_loads_only_the_designated_record_from_postgres(self):
        from state_store import state_connection
        row = fixtures.forecast()
        with state_connection() as connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS performance_ledger (
                id TEXT PRIMARY KEY, record JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''')
            connection.execute('''INSERT INTO performance_ledger(id, record)
                VALUES (%s, %s::jsonb) ON CONFLICT (id) DO UPDATE SET record=EXCLUDED.record''',
                (row['id'], json.dumps(row)))
            connection.commit()
        store = broadcast.BroadcastStore()
        self.assertEqual(store.saved_record(row['id']), row)
        self.assertIsNone(store.saved_record('auto:20990101:04:01'))

    def test_one_broadcast_claim_across_postgres_connections_and_restart(self):
        sample = lb.frozen_sample(fixtures.forecast(), {'04': '平和島'}, self.now)
        with ThreadPoolExecutor(max_workers=8) as pool:
            claims = list(pool.map(lambda _: broadcast.BroadcastStore().claim_broadcast(sample, self.now), range(8)))
        accepted = [value for value in claims if value is not None]
        self.assertEqual(len(accepted), 1)
        entry = accepted[0]
        restarted = broadcast.BroadcastStore()
        retried = restarted.claim_broadcast(sample, self.now + timedelta(seconds=46))
        self.assertEqual(retried['retry_key'], entry['retry_key'])
        self.assertEqual(retried['payload'], entry['payload'])
        restarted.finish_broadcast(retried, self.now, 'accepted')
        self.assertIsNone(broadcast.BroadcastStore().claim_broadcast(sample, self.now + timedelta(minutes=2)))

    def test_reply_and_broadcast_cannot_pin_different_samples_in_postgres(self):
        samples = [lb.frozen_sample(fixtures.forecast(stadium=stadium),
                                   {'04': '平和島', '15': '丸亀'}, self.now)
                   for stadium in ('04', '15')]
        with ThreadPoolExecutor(max_workers=2) as pool:
            reply = pool.submit(lb.LineStore().pin, samples[0], self.now)
            publication = pool.submit(broadcast.BroadcastStore().claim_broadcast, samples[1], self.now)
            reply_sample, entry = reply.result(), publication.result()
        pinned = lb.LineStore().sample(self.now)
        self.assertEqual(reply_sample, pinned)
        if entry is not None:
            self.assertEqual(entry['sample_id'], pinned['id'])
        else:
            self.assertEqual(pinned, samples[0])


if __name__ == '__main__':
    unittest.main()


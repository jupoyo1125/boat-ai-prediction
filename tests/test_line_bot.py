import base64
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import hmac
from itertools import permutations
import json
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from flask import Flask

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import line_bot as lb


USER = 'U' + '1' * 32
OTHER_USER = 'U' + '2' * 32
DESTINATION = 'U' + '3' * 32


def forecast(*, stadium='04', race=1, date='20261015',
             saved='10:01:00', deadline='10:30:00'):
    day = datetime.strptime(date, '%Y%m%d').strftime('%Y-%m-%d')
    combos = ['-'.join(map(str, item)) for item in permutations(range(1, 7), 3)]
    return {
        'id': f'auto:{date}:{stadium}:{race:02d}',
        'date': date, 'stadium': stadium, 'race': race,
        'prediction_origin': 'automatic', 'performance_version': 2,
        'prediction_saved_at': f'{day}T{saved}+09:00',
        'exhibition_detected_at': f'{day}T{saved}+09:00',
        'scheduled_deadline': f'{day}T{deadline}+09:00',
        'settled': False, 'learned': False, 'actual_combo': '',
        'bets': [{'category': category, 'bet': combo}
                 for category, picks in [('gachi', combos[:10]),
                                         ('roman', combos[20:30]),
                                         ('oni', combos[45:48])]
                 for combo in picks],
    }


class MemoryStore(lb.LineStore):
    """Run the production state transitions against an isolated transaction."""
    def __init__(self):
        self.state = {'version': 1, 'days': {}}
        self.lock = threading.Lock()

    def mutate(self, change):
        with self.lock:
            state = deepcopy(self.state)
            result = change(state)
            self.state = state
            return result


class LineBotTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 15, 10, 10, tzinfo=lb.JST)
        self.row = forecast()
        self.rows = [self.row]
        self.core = SimpleNamespace(
            app=Flask(__name__),
            STADIUMS={'04': '平和島', '15': '丸亀', '24': '大村'},
            load_ledger=mock.Mock(side_effect=lambda: deepcopy(self.rows)),
        )
        self.store = MemoryStore()
        self.bot = lb.SampleBot(self.core, self.store, lambda: self.now)
        self.config = lb.Config('test-secret', 'test-token', 'test', USER)
        self.sender = mock.Mock()
        self.sender.verify_account.return_value = DESTINATION
        self.factory = mock.Mock(return_value=self.sender)
        self.clock = lambda: self.now
        lb.register_line_routes(
            self.core, store=self.store, client_factory=self.factory,
            clock=lambda: self.clock(), config_provider=lambda: self.config)
        self.client = self.core.app.test_client()

    def event(self, text='買い目', *, event_id='event-1', user=USER):
        return {
            'type': 'message', 'webhookEventId': event_id,
            'replyToken': 'reply-not-a-real-token',
            'timestamp': int(self.now.timestamp() * 1000),
            'source': {'type': 'user', 'userId': user},
            'message': {'type': 'text', 'id': 'message-1', 'text': text},
        }

    def post(self, events=None, *, payload=None, signature=None):
        if payload is None:
            payload = {'destination': DESTINATION,
                       'events': [self.event()] if events is None else events}
        body = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()
        signature = signature if signature is not None else base64.b64encode(
            hmac.new(b'test-secret', body, hashlib.sha256).digest()).decode()
        return self.client.post('/api/line/webhook', data=body,
                                headers={'X-Line-Signature': signature},
                                content_type='application/json')

    def reply(self):
        return self.sender.reply.call_args.args[1]

    def test_returns_exact_saved_lists_and_metadata_for_all_three_categories(self):
        text, deadline = self.bot.answer('買い目')
        for bet in self.row['bets']:
            self.assertIn(bet['bet'], text)
        self.assertIn('ガチガチ｜10点', text)
        self.assertIn('ロマン砲｜10点', text)
        self.assertIn('鬼しぼり｜3点', text)
        self.assertIn('保存 10:01:00／締切予定 10:30', text)
        self.assertEqual(deadline, lb.timestamp(self.row['scheduled_deadline']))
        self.assertNotIn('http', text)

    def test_empty_oni_is_a_real_skip_and_not_invented_picks(self):
        self.row['bets'] = [bet for bet in self.row['bets'] if bet['category'] != 'oni']
        text, _ = self.bot.answer('鬼しぼり')
        self.assertIn('鬼しぼり｜0点\n見送り（買い目なし）', text)

    def test_global_sample_is_pinned_across_requests_and_bot_restarts(self):
        first = self.bot.answer('買い目')[0]
        self.rows.insert(0, forecast(stadium='15', race=2, deadline='10:20:00'))
        restarted = lb.SampleBot(self.core, self.store, lambda: self.now)
        self.assertEqual(restarted.answer('ロマン砲')[0], first)
        self.assertEqual(self.store.state['days']['20261015']['sample']['id'], self.row['id'])

    def test_another_requested_race_does_not_change_or_disclose_a_second_race(self):
        self.bot.answer('買い目')
        second = forecast(stadium='15', race=2, deadline='11:00:00')
        second['bets'][0]['bet'] = '6-5-4'
        self.rows.append(second)
        text, deadline = self.bot.answer('丸亀2R')
        self.assertIn('平和島1Rの1レース', text)
        self.assertNotIn('6-5-4', text)
        self.assertIsNone(deadline)

    def test_full_width_request_and_matching_venue_race_work(self):
        expected = self.bot.answer('買い目')[0]
        self.assertEqual(self.bot.answer('今日の平和島１Ｒ')[0], expected)
        self.assertEqual(self.bot.answer('今日平和島１Ｒ')[0], expected)
        self.assertEqual(self.bot.answer('平和島１Ｒの買い目')[0], expected)

    def test_expired_sample_is_not_replaced_with_another_live_race(self):
        self.bot.answer('買い目')
        self.now = self.now.replace(hour=10, minute=29)
        self.rows.append(forecast(stadium='15', deadline='11:00:00'))
        text, deadline = self.bot.answer('買い目')
        self.assertIn('配信を終了', text)
        self.assertIsNone(deadline)
        self.assertNotIn('ガチガチ｜', text)

    def test_missing_or_changed_saved_record_fails_closed_without_reselection(self):
        self.bot.answer('買い目')
        for rows in [[], [forecast(stadium='15')]]:
            self.rows = rows
            self.assertIn('配信を停止', self.bot.answer('買い目')[0])
        self.rows = [deepcopy(self.row)]
        self.rows[0]['bets'][0]['bet'] = '6-5-4'
        self.assertIn('配信を停止', self.bot.answer('買い目')[0])

    def test_rejects_unfrozen_historical_result_and_invalid_forecasts(self):
        changes = [
            {'prediction_origin': 'manual'}, {'performance_version': 1},
            {'settled': True}, {'learned': True}, {'actual_combo': '123'},
            {'date': '20261014'}, {'race': True},
            {'prediction_saved_at': '2026-10-15T10:01:00'},
            {'prediction_saved_at': '2026-10-15T10:40:00+09:00'},
            {'prediction_saved_at': '2026-10-15T10:29:45+09:00'},
            {'bets': [{'category': 'gachi', 'bet': '1-1-2'}]},
            {'bets': [{'category': 'other', 'bet': '1-2-3'}]},
        ]
        for change in changes:
            with self.subTest(change=change):
                row = dict(deepcopy(self.row), **change)
                self.assertIsNone(lb.frozen_sample(row, self.core.STADIUMS, self.now))
        bad = deepcopy(self.row)
        bad['bets'].append(deepcopy(bad['bets'][0]))
        self.assertIsNone(lb.frozen_sample(bad, self.core.STADIUMS, self.now))

    def test_no_record_does_not_make_up_predictions(self):
        self.rows = []
        text, deadline = self.bot.answer('買い目')
        self.assertIn('準備中', text)
        self.assertIsNone(deadline)
        self.assertIsNone(self.store.state['days']['20261015']['sample'])

    def test_unknown_text_is_help_without_loading_predictions(self):
        self.assertEqual(self.bot.answer('20261014 平和島1R'), (lb.HELP, None))
        self.core.load_ledger.assert_not_called()

    def test_invalid_signature_is_rejected_before_any_side_effect(self):
        response = self.post(signature='invalid')
        self.assertEqual(response.status_code, 403)
        self.factory.assert_not_called()
        self.assertEqual(self.store.state['days'], {})

    def test_signature_covers_the_exact_japanese_utf8_body(self):
        self.assertEqual(self.post().status_code, 200)
        self.assertIn('ガチガチ｜', self.reply())
        self.assertFalse(lb.valid_signature(b'changed body', 'invalid', 'test-secret'))
        self.assertFalse(lb.valid_signature(b'body', '非ASCII', 'test-secret'))

    def test_verification_request_checks_account_but_does_not_send(self):
        self.assertEqual(self.post(events=[]).status_code, 200)
        self.sender.verify_account.assert_called_once()
        self.sender.reply.assert_not_called()
        self.assertEqual(self.store.state['days'], {})

    def test_wrong_destination_or_account_error_stops_sending(self):
        response = self.post(payload={'destination': 'wrong', 'events': [self.event()]})
        self.assertEqual(response.status_code, 503)
        self.sender.reply.assert_not_called()
        self.sender.verify_account.side_effect = lb.LineError('account check failed')
        self.assertEqual(self.post().status_code, 503)
        self.sender.reply.assert_not_called()

    def test_test_mode_ignores_other_users_and_group_messages(self):
        other = self.event(user=OTHER_USER)
        group = self.event(event_id='group')
        group['source']['type'] = 'group'
        self.assertEqual(self.post(events=[other, group]).status_code, 200)
        self.sender.reply.assert_not_called()
        self.assertEqual(self.store.state['days'], {})

    def test_live_mode_answers_different_users_with_the_same_daily_sample(self):
        self.config = lb.Config('test-secret', 'test-token', 'live')
        self.post()
        first = self.reply()
        self.post(events=[self.event(event_id='event-2', user=OTHER_USER)])
        self.assertEqual(self.sender.reply.call_count, 2)
        self.assertEqual(self.reply(), first)

    def test_duplicate_events_are_replied_to_at_most_once(self):
        self.post()
        self.post()
        self.assertEqual(self.sender.reply.call_count, 1)

    def test_uncertain_delivery_is_not_retried_on_redelivery(self):
        self.sender.reply.side_effect = lb.LineError('unknown delivery result')
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(self.sender.reply.call_count, 1)
        events = self.store.state['days']['20261015']['events']
        self.assertEqual(list(events.values()), ['failed'])

    def test_old_or_future_or_malformed_events_are_not_replied_to(self):
        old = self.event()
        old['timestamp'] -= 60000
        future = self.event(event_id='future')
        future['timestamp'] += 10000
        bad = self.event(event_id='bad')
        bad['source'] = []
        response = self.post(events=[old, future, bad])
        self.assertEqual(response.status_code, 200)
        self.sender.reply.assert_not_called()

    def test_disabled_configuration_has_no_network_or_database_work(self):
        self.config = None
        self.assertEqual(self.post().status_code, 503)
        self.factory.assert_not_called()
        self.assertEqual(self.store.state['days'], {})

    def test_invalid_or_oversized_webhook_is_rejected(self):
        self.assertEqual(self.post(payload=[]).status_code, 400)
        self.assertEqual(self.post(events=[{}] * 21).status_code, 400)
        response = self.client.post('/api/line/webhook', data=b'x' * 65537)
        self.assertEqual(response.status_code, 413)
        self.sender.reply.assert_not_called()

    def test_deadline_is_checked_again_just_before_sending(self):
        self.now = self.now.replace(minute=28, second=29)
        earlier, later = self.now, self.now + timedelta(seconds=2)
        times = iter([earlier, earlier, later])
        self.clock = lambda: next(times)
        self.assertEqual(self.post().status_code, 200)
        self.assertIn('配信を終了', self.reply())
        self.assertNotIn('ガチガチ｜', self.reply())

    def test_storage_read_failure_does_not_fall_back_to_fake_or_local_predictions(self):
        self.core.load_ledger.side_effect = RuntimeError('database unavailable')
        self.assertEqual(self.post().status_code, 200)
        self.sender.reply.assert_not_called()
        self.assertEqual(list(self.store.state['days']['20261015']['events'].values()), ['failed'])

    def test_state_claim_is_atomic_for_duplicate_events(self):
        with ThreadPoolExecutor(max_workers=10) as pool:
            claims = list(pool.map(lambda _: self.store.claim('same-event', self.now), range(10)))
        self.assertEqual(claims.count(True), 1)

    def test_concurrent_pin_never_selects_two_races(self):
        samples = [lb.frozen_sample(forecast(stadium=stadium), self.core.STADIUMS, self.now)
                   for stadium in ['04', '15']]
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(lambda sample: self.store.pin(sample, self.now), samples))
        self.assertEqual(replies[0], replies[1])
        self.assertEqual(len(self.store.state['days']), 1)

    def test_a_new_jst_day_can_have_one_new_sample(self):
        self.bot.answer('買い目')
        self.now += timedelta(days=1)
        self.rows = [forecast(date='20261016', stadium='15')]
        text, _ = self.bot.answer('買い目')
        self.assertIn('10/16 丸亀1R', text)
        self.assertEqual(len(self.store.state['days']), 2)

    def test_config_defaults_to_off_and_then_operator_only_test_mode(self):
        env = {'BOAT_LINE_ENABLED': '0', 'LINE_CHANNEL_SECRET': 'secret',
               'LINE_CHANNEL_ACCESS_TOKEN': 'token', 'DATABASE_URL': 'postgresql://example',
               'BOAT_STORAGE_PHASE': 'render', 'BOAT_LINE_MODE': 'test',
               'LINE_TEST_USER_ID': USER}
        with mock.patch.dict(os.environ, env):
            self.assertIsNone(lb.configuration())
            os.environ['BOAT_LINE_ENABLED'] = '1'
            config = lb.configuration()
            self.assertEqual(config.mode, 'test')
            self.assertEqual(config.test_user, USER)
            self.assertNotIn('secret', repr(config))
            self.assertNotIn('token', repr(config))
            os.environ['LINE_TEST_USER_ID'] = ''
            self.assertIsNone(lb.configuration())
            os.environ['BOAT_LINE_MODE'] = 'live'
            self.assertEqual(lb.configuration().mode, 'live')
            os.environ['BOAT_STORAGE_PHASE'] = 'prepare'
            self.assertIsNone(lb.configuration())

    def test_client_verifies_basic_id_and_never_follows_redirects(self):
        client = lb.LineClient('private-token')
        response = mock.Mock(status_code=200)
        response.json.return_value = {'basicId': '@other', 'userId': DESTINATION}
        with mock.patch('line_bot.requests.get', return_value=response) as get:
            with self.assertRaises(lb.LineError):
                client.verify_account()
            self.assertFalse(get.call_args.kwargs['allow_redirects'])
            response.json.return_value = {'basicId': lb.EXPECTED_BASIC_ID, 'userId': DESTINATION}
            self.assertEqual(client.verify_account(), DESTINATION)

    def test_reply_client_only_uses_reply_endpoint_not_proactive_push(self):
        response = mock.Mock(status_code=200)
        with mock.patch('line_bot.requests.post', return_value=response) as post:
            lb.LineClient('private-token').reply('reply-token', '検証用')
            self.assertEqual(post.call_args.args[0], 'https://api.line.me/v2/bot/message/reply')
            self.assertEqual(post.call_args.kwargs['json']['messages'][0]['text'], '検証用')
            self.assertFalse(post.call_args.kwargs['allow_redirects'])


@unittest.skipUnless(os.getenv('LINE_TEST_DATABASE_URL'), 'isolated PostgreSQL test database not configured')
class PostgresLineStoreTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {
            'DATABASE_URL': os.environ['LINE_TEST_DATABASE_URL'],
            'BOAT_STORAGE_PHASE': 'render',
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        import automation
        from state_store import state_connection
        self.ready = mock.patch.object(automation, '_table_ready', False)
        self.ready.start()
        self.addCleanup(self.ready.stop)
        automation.ensure_status_table()
        with state_connection() as connection:
            connection.execute('DELETE FROM boat_automation_state WHERE id=%s', (lb.STATE_ID,))
            connection.commit()
        self.now = datetime(2026, 10, 15, 10, 10, tzinfo=lb.JST)

    def test_postgres_row_lock_pins_one_sample_across_store_instances(self):
        samples = [lb.frozen_sample(forecast(stadium=stadium),
                                   {'04': '平和島', '15': '丸亀'}, self.now)
                   for stadium in ['04', '15']]
        def pin(sample):
            return lb.LineStore().pin(sample, self.now)
        with ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(pin, samples))
        self.assertEqual(result[0], result[1])
        self.assertEqual(lb.LineStore().sample(self.now), result[0])

    def test_postgres_claim_is_durable_and_atomic_across_connections(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            claimed = list(pool.map(
                lambda _: lb.LineStore().claim('same-event', self.now), range(8)))
        self.assertEqual(claimed.count(True), 1)
        self.assertFalse(lb.LineStore().claim('same-event', self.now))

    def test_failed_state_change_rolls_back_without_erasing_the_sample(self):
        sample = lb.frozen_sample(forecast(), {'04': '平和島'}, self.now)
        lb.LineStore().pin(sample, self.now)
        def fail(state):
            state['days'] = {}
            raise RuntimeError('simulated failure')
        with self.assertRaises(RuntimeError):
            lb.LineStore().mutate(fail)
        self.assertEqual(lb.LineStore().sample(self.now), sample)


if __name__ == '__main__':
    unittest.main()

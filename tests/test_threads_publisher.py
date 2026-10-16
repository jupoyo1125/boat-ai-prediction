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


class LinePromotionTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 17, 10, 20, tzinfo=tp.JST)
        environment = mock.patch.dict(os.environ, {
            'BOAT_THREADS_ENABLED': '1', 'BOAT_THREADS_MODE': 'daily',
            'BOAT_THREADS_START_DATE': '20261016', 'BOAT_THREADS_UNTIL_DATE': '20261018',
            'BOAT_THREADS_FROM_RACE': '1', 'THREADS_ACCESS_TOKEN': 'promotion-private-token',
            'BOAT_THREADS_LINE_PROMO_DATE': '20261017',
            'BOAT_THREADS_LINE_PROMO_RACE': '5',
            'BOAT_THREADS_LINE_PROMO_URL': 'https://lin.ee/rPOD0ur',
        }, clear=True)
        environment.start(); self.addCleanup(environment.stop)
        configured = mock.patch('automation.configured', return_value=True)
        configured.start(); self.addCleanup(configured.stop)
        clock = mock.patch('threads_publisher.now_jst', side_effect=lambda: self.now)
        clock.start(); self.addCleanup(clock.stop)
        self.row = DailyPublisherTests.row('20261017', 5)
        self.config = tp.configuration(self.now)

    def test_target_invites_to_line_without_revealing_racers_or_buy_lists(self):
        entry = tp.candidate(self.row, self.config, self.now)
        self.assertIsNotNone(entry)
        text = entry['text']
        self.assertIn('10/17 平和島5R｜無料予想LINE', text)
        self.assertIn('https://lin.ee/rPOD0ur', text)
        self.assertIn('20歳以上・配信希望', text)
        self.assertIn('締切10:30', text)
        for private in ['本命', '相手軸', '穴', '選手', 'AI予想', 'onrender.com']:
            self.assertNotIn(private, text)
        for bet in self.row['bets']:
            self.assertNotIn(bet['bet'], text)
        self.assertLessEqual(len(text), 500)

    def test_other_races_and_next_day_keep_the_original_forecast(self):
        for date, race in [('20261017', 4), ('20261017', 6), ('20261018', 5)]:
            with self.subTest(date=date, race=race):
                row = DailyPublisherTests.row(date, race)
                now = self.now.replace(day=int(date[-2:]))
                entry = tp.candidate(row, tp.configuration(now), now)
                self.assertIsNotNone(entry)
                self.assertIn('本命①選手1', entry['text'])
                self.assertIn('ガチガチ10点', entry['text'])
                self.assertNotIn('https://lin.ee/', entry['text'])

    def test_promotion_still_requires_exhibition_and_two_minutes_before_close(self):
        incomplete = copy.deepcopy(self.row)
        incomplete['prediction']['boats'][0]['exhibition'] = None
        self.assertIsNone(tp.candidate(incomplete, self.config, self.now))
        self.assertIsNone(tp.candidate(self.row, self.config, self.now.replace(minute=29)))
        wrong_venue = dict(self.row, stadium='05')
        self.assertIsNone(tp.candidate(wrong_venue, self.config, self.now))

    def test_restarts_publish_only_one_invitation_and_leave_saved_picks_intact(self):
        original = copy.deepcopy(self.row)
        store = MemoryStore()
        core = mock.Mock()
        core.load_ledger.return_value = [self.row]
        client = mock.Mock()
        client.create.return_value = 'promotion-container'
        client.publish.return_value = 'promotion-post'
        for _ in range(2):
            tp.Publisher(core, self.config, store, client, clock=lambda: self.now).run_once()
        client.publish.assert_called_once_with('promotion-container')
        self.assertIn('https://lin.ee/rPOD0ur', client.create.call_args.args[0])
        self.assertEqual(self.row, original)

    def test_incomplete_or_unsafe_campaign_disables_publication(self):
        for changes in [
            {'BOAT_THREADS_LINE_PROMO_URL': ''},
            {'BOAT_THREADS_LINE_PROMO_DATE': '20260230'},
            {'BOAT_THREADS_LINE_PROMO_RACE': '13'},
            {'BOAT_THREADS_LINE_PROMO_URL': 'http://lin.ee/rPOD0ur'},
            {'BOAT_THREADS_LINE_PROMO_URL': 'https://lin.ee.evil.example/rPOD0ur'},
            {'BOAT_THREADS_LINE_PROMO_URL': 'https://lin.ee/rPOD0ur?redirect=elsewhere'},
        ]:
            with self.subTest(changes=changes), mock.patch.dict(os.environ, changes):
                self.assertIsNone(tp.configuration(self.now))

    def test_unset_campaign_keeps_existing_behavior(self):
        with mock.patch.dict(os.environ, {
            'BOAT_THREADS_LINE_PROMO_DATE': '', 'BOAT_THREADS_LINE_PROMO_RACE': '',
            'BOAT_THREADS_LINE_PROMO_URL': '',
        }):
            config = tp.configuration(self.now)
            self.assertIsNone(config.line_promotion)
            self.assertIn('本命①選手1', tp.candidate(self.row, config, self.now)['text'])

    def test_status_reports_the_exact_day_and_race_without_credentials(self):
        with mock.patch.object(tp.PostStore, 'read', return_value={'version': 1, 'posts': {}}):
            status = tp.public_status()
        self.assertTrue(status['enabled'])
        self.assertEqual(status['line_promotion'], {
            'date': '20261017', 'stadium': '04', 'race': 5,
            'url': 'https://lin.ee/rPOD0ur', 'active_today': True,
            'content': 'line_invitation',
        })
        self.assertNotIn('promotion-private-token', str(status))


class DailyPublisherTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 15, 17, tzinfo=tp.JST)
        self.env = {
            'BOAT_THREADS_ENABLED': '1', 'BOAT_THREADS_MODE': 'daily',
            'BOAT_THREADS_START_DATE': '20261016', 'BOAT_THREADS_UNTIL_DATE': '20261017',
            'BOAT_THREADS_FROM_RACE': '1', 'THREADS_ACCESS_TOKEN': 'daily-private-test-token',
        }
        environment = mock.patch.dict(os.environ, self.env, clear=True)
        environment.start(); self.addCleanup(environment.stop)
        configured = mock.patch('automation.configured', return_value=True)
        configured.start(); self.addCleanup(configured.stop)
        clock = mock.patch('threads_publisher.now_jst', side_effect=lambda: self.now)
        clock.start(); self.addCleanup(clock.stop)
        self.core = mock.Mock()
        self.store = MemoryStore()
        self.client = mock.Mock()
        self.client.create.side_effect = lambda _: str(self.client.create.call_count)
        self.client.publish.side_effect = lambda container: 'post-' + container
        self.client.permalink.side_effect = lambda post: (
            f'https://www.threads.com/@{tp.USERNAME}/post/{post}')

    @staticmethod
    def row(date, race):
        row = forecast()
        day = datetime.strptime(date, '%Y%m%d').strftime('%Y-%m-%d')
        row.update(id=f'auto:{date}:04:{race:02d}', date=date, race=race)
        for key in ('prediction_saved_at', 'exhibition_detected_at', 'scheduled_deadline'):
            row[key] = row[key].replace('2026-10-15', day)
        return row

    def publisher(self, config=None):
        return tp.Publisher(self.core, config or tp.configuration(self.now), self.store,
                            self.client, clock=lambda: self.now)

    def test_daily_scope_requires_a_valid_start_and_all_twelve_races(self):
        for changes in [
            {'BOAT_THREADS_START_DATE': ''}, {'BOAT_THREADS_START_DATE': '20260230'},
            {'BOAT_THREADS_UNTIL_DATE': '20261014'}, {'BOAT_THREADS_UNTIL_DATE': 'forever'},
            {'BOAT_THREADS_MODE': 'unknown'}, {'BOAT_THREADS_FROM_RACE': '12'},
        ]:
            with self.subTest(changes=changes), mock.patch.dict(os.environ, changes):
                self.assertIsNone(tp.configuration(self.now))
        config = tp.configuration(self.now)
        self.assertEqual((config.mode, config.start_date, config.until_date, config.first_race),
                         ('daily', '20261016', '20261017', 1))
        self.assertNotIn(self.env['THREADS_ACCESS_TOKEN'], repr(config))

    def test_a_missing_daily_mode_does_not_expand_a_single_day_setting(self):
        os.environ.pop('BOAT_THREADS_MODE')
        os.environ['BOAT_THREADS_DATE'] = '20261015'
        config = tp.configuration(self.now)
        self.assertEqual(config.mode, 'date')
        self.assertFalse(tp.scope_active(config, self.now + timedelta(days=1)))

    def test_daily_scope_rolls_over_at_japanese_midnight(self):
        utc = datetime(2026, 10, 15, 14, 59, 59, tzinfo=tp.timezone.utc)
        before = tp.configuration(utc)
        after = tp.configuration(utc + timedelta(seconds=1))
        self.assertEqual((before.date, after.date), ('20261015', '20261016'))
        self.assertFalse(tp.scope_active(before, utc))
        self.assertTrue(tp.scope_active(after, utc + timedelta(seconds=1)))

    def test_waiting_scope_only_checks_connection_and_never_loads_forecasts(self):
        publisher = self.publisher()
        publisher.probe_connection()
        publisher.run_once()
        self.client.verify_account.assert_called_once()
        self.core.load_ledger.assert_not_called()
        self.client.create.assert_not_called()
        self.client.publish.assert_not_called()
        with mock.patch.object(tp.PostStore, 'read') as read:
            status = tp.public_status()
        read.assert_not_called()
        self.assertTrue(status['armed'])
        self.assertFalse(status['enabled'])
        self.assertEqual(status['phase'], 'scheduled')
        self.assertEqual(status['connection']['state'], 'connected')
        self.assertEqual(status['from_race'], 1)
        self.assertNotIn(self.env['THREADS_ACCESS_TOKEN'], str(status))

    def test_connection_errors_are_redacted_and_probes_have_a_retry_interval(self):
        self.client.verify_account.side_effect = RuntimeError(self.env['THREADS_ACCESS_TOKEN'])
        publisher = self.publisher()
        publisher.probe_connection()
        self.now += timedelta(seconds=20)
        publisher.probe_connection()
        self.client.verify_account.assert_called_once()
        status = tp.public_status()
        self.assertEqual(status['connection']['state'], 'error')
        self.assertNotIn(self.env['THREADS_ACCESS_TOKEN'], str(status))
        self.now += timedelta(minutes=15)
        publisher.probe_connection()
        self.assertEqual(self.client.verify_account.call_count, 2)
        self.client.create.assert_not_called()
        self.client.publish.assert_not_called()

    def test_healthy_idle_connection_is_not_rechecked_every_twenty_seconds(self):
        publisher = self.publisher()
        publisher.probe_connection()
        for _ in range(10):
            self.now += timedelta(seconds=20)
            publisher.probe_connection()
        self.client.verify_account.assert_called_once()
        self.now += timedelta(hours=1)
        publisher.probe_connection()
        self.assertEqual(self.client.verify_account.call_count, 2)

    def test_daily_worker_waits_then_publishes_each_days_races_once_without_restart(self):
        self.core.load_ledger.return_value = [
            self.row(date, race) for date in ('20261015', '20261016', '20261017', '20261018')
            for race in (1, 12)]
        times = iter([
            datetime(2026, 10, 16, 10, 20, tzinfo=tp.JST),
            datetime(2026, 10, 16, 23, 59, 50, tzinfo=tp.JST),
            datetime(2026, 10, 17, 0, 0, 10, tzinfo=tp.JST),
            datetime(2026, 10, 17, 10, 20, tzinfo=tp.JST),
            datetime(2026, 10, 17, 10, 21, tzinfo=tp.JST),
            datetime(2026, 10, 18, 7, tzinfo=tp.JST),
        ])
        def wait(_):
            self.now = next(times)
        real_publisher = tp.Publisher
        def build(core, config, clock):
            return real_publisher(core, config, self.store, self.client, clock)
        with mock.patch.object(tp, 'Publisher', side_effect=build):
            tp.run_worker(self.core, clock=lambda: self.now, wait=wait)
        self.assertEqual(self.client.publish.call_count, 4)
        self.assertEqual(self.core.load_ledger.call_count, 3)
        self.assertEqual(set(self.store.read()['posts']), {
            f'auto:{date}:04:{race:02d}' for date in ('20261016', '20261017') for race in (1, 12)})
        self.assertTrue(all(entry['state'] == 'published'
                            for entry in self.store.read()['posts'].values()))
        self.assertFalse(tp.public_status()['armed'])
        self.assertEqual(tp.public_status()['phase'], 'expired')

    def test_a_day_without_heiwajima_forecasts_produces_no_post(self):
        self.now = datetime(2026, 10, 16, 10, 20, tzinfo=tp.JST)
        row = self.row('20261016', 1)
        row.update(stadium='05', id='auto:20261016:05:01')
        self.core.load_ledger.return_value = [row, self.row('20261015', 1)]
        self.publisher().run_once()
        self.assertEqual(self.store.read()['posts'], {})
        self.client.create.assert_not_called()
        self.client.publish.assert_not_called()

    def test_buffer_daily_configuration_keeps_credentials_private(self):
        with mock.patch.dict(os.environ, {
            'BOAT_THREADS_PROVIDER': 'buffer', 'BUFFER_API_KEY': 'private-buffer-test-key',
            'BOAT_BUFFER_CHANNEL_ID': 'channel-1',
        }):
            config = tp.configuration(self.now)
            self.assertEqual((config.provider, config.mode, config.channel_id),
                             ('buffer', 'daily', 'channel-1'))
            self.assertNotIn('private-buffer-test-key', repr(config))
            self.assertNotIn('private-buffer-test-key', str(tp.public_status()))


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


def buffer_post(status='draft'):
    return {'id': 'buffer-post-1', 'status': status, 'text': tp.caption(forecast()),
            'sentAt': '2026-10-15T10:21:00+09:00' if status == 'sent' else None,
            'externalLink': 'https://www.threads.com/@ton123kyotei456/post/example',
            'schedulingType': 'automatic', 'assets': [], 'metadata': {'threadCount': 0},
            'channel': {'id': 'channel-1', 'service': 'threads', 'name': tp.USERNAME}}


class BufferClientTests(unittest.TestCase):
    def setUp(self):
        from buffer_publisher import BufferClient
        self.response = mock.Mock(status_code=200)
        self.session = mock.Mock()
        self.session.request.return_value = self.response
        self.client = BufferClient('private-buffer-key', 'channel-1', self.session)

    def test_configuration_requires_explicit_provider_and_channel(self):
        with mock.patch.dict(os.environ, {
            'BOAT_THREADS_ENABLED': '1', 'BOAT_THREADS_DATE': '20261015',
            'BOAT_THREADS_FROM_RACE': '10', 'BOAT_THREADS_PROVIDER': 'buffer',
            'BUFFER_API_KEY': 'private-buffer-key', 'THREADS_ACCESS_TOKEN': 'unused',
        }, clear=True):
            self.assertIsNone(tp.configuration())
            os.environ['BOAT_BUFFER_CHANNEL_ID'] = 'channel-1'
            config = tp.configuration()
            self.assertEqual((config.provider, config.channel_id), ('buffer', 'channel-1'))
            self.assertNotIn('private-buffer-key', repr(config))
            os.environ['BOAT_THREADS_PROVIDER'] = 'unknown'
            self.assertIsNone(tp.configuration())

    def test_channel_identity_connection_and_header_are_checked(self):
        channel = dict(buffer_post()['channel'], isDisconnected=False,
                       isLocked=False, isQueuePaused=False)
        self.response.json.return_value = {'data': {'channel': channel}}
        self.client.verify_account()
        args, kwargs = self.session.request.call_args
        self.assertEqual(args, ('POST', 'https://api.buffer.com'))
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer private-buffer-key')
        self.assertNotIn('private-buffer-key', str(kwargs['json']))
        self.assertFalse(kwargs['allow_redirects'])
        for changes in [{'name': 'another_user'}, {'service': 'instagram'},
                        {'id': 'another-channel'}, {'isQueuePaused': True}]:
            self.response.json.return_value = {'data': {'channel': dict(channel, **changes)}}
            with self.assertRaises(tp.ThreadsError):
                self.client.verify_account()

    def test_only_a_private_draft_can_be_created_during_preparation(self):
        self.response.json.return_value = {'data': {'createPost': {'post': buffer_post()}}}
        self.assertEqual(self.client.create(tp.caption(forecast())), 'buffer-post-1')
        values = self.session.request.call_args.kwargs['json']['variables']['input']
        self.assertTrue(values['saveToDraft'])
        self.assertEqual(values['mode'], 'addToQueue')
        self.assertEqual(values['assets'], [])
        self.response.json.return_value['data']['createPost']['post']['status'] = 'scheduled'
        with self.assertRaises(tp.WrongAccount):
            self.client.create(tp.caption(forecast()))

    def test_publication_edits_the_same_draft_for_automatic_share_now(self):
        self.response.json.return_value = {'data': {'editPost': {'post': buffer_post('sending')}}}
        self.client.publish('buffer-post-1', tp.caption(forecast()))
        payload = self.session.request.call_args.kwargs['json']
        self.assertIn('editPost', payload['query'])
        values = payload['variables']['input']
        self.assertEqual(values['id'], 'buffer-post-1')
        self.assertEqual(values['mode'], 'shareNow')
        self.assertFalse(values['saveToDraft'])
        self.assertEqual(values['schedulingType'], 'automatic')

    def test_changed_drafts_and_graphql_errors_fail_closed_without_logging_bodies(self):
        post = buffer_post()
        self.response.json.return_value = {'data': {'post': post}}
        self.client.validate_draft(post['id'], post['text'])
        post['text'] += ' unexpected content'
        with self.assertRaises(tp.ThreadsError):
            self.client.validate_draft(post['id'], tp.caption(forecast()))
        self.response.json.return_value = {'errors': [{'message': 'private-buffer-key'}]}
        with self.assertRaises(tp.ThreadsError) as caught:
            self.client.publication('buffer-post-1')
        self.assertNotIn('private-buffer-key', str(caught.exception))

    def test_withdrawal_preserves_the_post_as_a_draft(self):
        self.response.json.return_value = {'data': {'editPost': {'post': buffer_post()}}}
        self.client.withdraw('buffer-post-1')
        payload = self.session.request.call_args.kwargs['json']
        self.assertIn('editPost', payload['query'])
        self.assertNotIn('deletePost', payload['query'])
        self.assertEqual(payload['variables']['input'], {'id': 'buffer-post-1', 'saveToDraft': True})


class BufferFlowTests(unittest.TestCase):
    def setUp(self):
        from buffer_publisher import BufferClient
        self.now = datetime(2026, 10, 15, 10, 20, tzinfo=tp.JST)
        self.config = tp.Config('20261015', 2, 'unused-key', 'buffer', 'channel-1')
        self.row = forecast()
        self.store = MemoryStore()
        self.client = mock.Mock()
        self.client.create.return_value = 'buffer-post-1'
        self.client.publish.return_value = 'buffer-post-1'
        self.client.publication.return_value = buffer_post('sending')
        self.client.permalink.side_effect = BufferClient.permalink
        self.core = mock.Mock()
        self.core.load_ledger.return_value = [self.row]
        patch = mock.patch('automation.configured', return_value=True)
        patch.start(); self.addCleanup(patch.stop)
        self.publisher = tp.Publisher(self.core, self.config, self.store, self.client,
                                      clock=lambda: self.now)

    def state(self):
        return self.store.read()['posts'][self.row['id']]

    def test_acceptance_is_confirmed_before_being_reported_as_published(self):
        self.publisher.run_once()
        self.assertEqual(self.state()['state'], 'submitted')
        self.client.publish.assert_called_once_with('buffer-post-1', tp.caption(self.row))
        self.now += timedelta(seconds=20)
        self.publisher.run_once()
        self.assertEqual(self.state()['state'], 'submitted')
        self.now += timedelta(seconds=60)
        self.client.publication.return_value = buffer_post('sent')
        self.publisher.run_once(); self.publisher.run_once()
        self.assertEqual(self.state()['state'], 'published')
        self.assertIn('/@ton123kyotei456/post/', self.state()['permalink'])
        self.client.publish.assert_called_once()

    def test_multiple_workers_share_one_confirmation_lease(self):
        self.publisher.run_once()
        self.now += timedelta(seconds=20)
        with ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(lambda _: self.store.claim_confirmation(self.config, self.now), range(2)))
        self.assertEqual(sum(value is not None for value in values), 1)

    def test_pending_forecast_returns_to_draft_when_the_deadline_approaches(self):
        self.publisher.run_once()
        self.now = tp.timestamp(self.row['scheduled_deadline']) - tp.MIN_LEAD
        self.client.publication.return_value = buffer_post('scheduled')
        self.publisher.run_once()
        self.client.withdraw.assert_called_once_with('buffer-post-1')
        self.assertEqual(self.state()['state'], 'expired')
        self.client.publish.assert_called_once()

    def test_withdrawal_timeout_does_not_resubmit(self):
        self.publisher.run_once()
        self.now = tp.timestamp(self.row['scheduled_deadline']) - tp.MIN_LEAD
        self.client.publication.return_value = buffer_post('scheduled')
        self.client.withdraw.side_effect = TimeoutError()
        self.publisher.run_once(); self.publisher.run_once()
        self.assertEqual(self.state()['state'], 'uncertain')
        self.client.publish.assert_called_once()

    def test_publication_timeout_is_not_retried_after_restart(self):
        self.client.publish.side_effect = TimeoutError('private-buffer-key')
        self.publisher.run_once(); self.publisher.run_once()
        self.assertEqual(self.state()['state'], 'uncertain')
        self.assertNotIn('private-buffer-key', str(self.state()))
        self.client.publish.assert_called_once()

    def test_buffer_error_is_not_published_and_not_recreated(self):
        self.publisher.run_once()
        self.now += timedelta(seconds=20)
        self.client.publication.return_value = buffer_post('error')
        self.publisher.run_once(); self.publisher.run_once()
        self.assertEqual(self.state()['state'], 'error')
        self.client.create.assert_called_once()
        self.client.publish.assert_called_once()

    def test_sent_after_deadline_is_reported_as_late(self):
        self.publisher.run_once()
        self.now = tp.timestamp(self.row['scheduled_deadline']) + timedelta(seconds=10)
        self.client.publication.return_value = dict(buffer_post('sent'), sentAt=self.now.isoformat())
        self.publisher.run_once()
        self.assertEqual(self.state()['state'], 'published_late')

    def test_changing_provider_never_reuses_another_providers_prepared_post(self):
        old_config = tp.Config('20261015', 2, 'unused-key')
        self.store.enqueue([tp.candidate(self.row, old_config, self.now)])
        self.publisher.run_once()
        self.client.create.assert_not_called()
        self.client.publish.assert_not_called()


if __name__ == '__main__':
    unittest.main()

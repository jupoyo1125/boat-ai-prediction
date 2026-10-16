from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import line_bot as lb
import line_broadcast as delivery
import test_line_bot as fixtures
import test_line_broadcast as delivery_fixtures


class AgeWebhookTests(unittest.TestCase):
    event = fixtures.LineBotTests.event
    post = fixtures.LineBotTests.post

    def setUp(self):
        fixtures.LineBotTests.setUp(self)
        self.store.state['subscriptions'] = {}

    def register(self, user=fixtures.USER, event_id='age-yes'):
        self.config = lb.Config('test-secret', 'test-token', 'live')
        return self.post(events=[self.event(lb.AGE_YES, user=user, event_id=event_id)])

    def test_unknown_user_receives_age_choices_and_no_prediction_or_sample(self):
        response = self.post()
        self.assertEqual(response.status_code, 200)
        text = self.sender.reply.call_args.args[1]
        self.assertEqual(text, lb.AGE_PROMPT)
        choices = self.sender.reply.call_args.kwargs['buttons']['template']['actions']
        self.assertEqual([choice['text'] for choice in choices], [lb.AGE_YES, lb.AGE_NO])
        self.core.load_ledger.assert_not_called()
        self.assertIsNone(self.store.sample(self.now))
        self.assertEqual(self.store.recipients(self.now), [])

    def test_follow_event_prompts_without_registering_or_disclosing_bets(self):
        event = self.event(event_id='new-follow')
        event['type'] = 'follow'
        event.pop('message')
        self.assertEqual(self.post(events=[event]).status_code, 200)
        self.assertEqual(self.store.subscription_status(fixtures.USER), 'pending')
        self.assertEqual(self.sender.reply.call_args.args[1], lb.AGE_PROMPT)
        self.assertEqual(self.sender.reply.call_args.kwargs['buttons'], lb.AGE_BUTTONS)
        self.core.load_ledger.assert_not_called()
        self.assertEqual(self.store.recipients(self.now), [])

    def test_explicit_adult_and_delivery_consent_then_prediction_request(self):
        self.register()
        self.assertEqual(self.store.recipients(self.now), [fixtures.USER])
        self.core.load_ledger.assert_not_called()
        self.assertNotIn('ガチガチ｜', self.sender.reply.call_args.args[1])
        self.now += timedelta(seconds=1)
        self.post(events=[self.event(event_id='buy-after-consent')])
        self.assertIn('ガチガチ｜10点', self.sender.reply.call_args.args[1])
        self.assertEqual(self.store.sample(self.now)['id'], self.row['id'])

    def test_age_statement_without_delivery_consent_is_not_registration(self):
        self.post(events=[self.event('20歳以上')])
        self.assertFalse(self.store.is_adult(fixtures.USER, self.now))
        self.assertEqual(self.sender.reply.call_args.args[1], lb.AGE_PROMPT)

    def test_underage_answer_and_later_buy_request_never_load_predictions(self):
        self.post(events=[self.event(lb.AGE_NO)])
        self.now += timedelta(seconds=1)
        self.post(events=[self.event('買い目', event_id='underage-buy')])
        self.assertIn('20歳未満', self.sender.reply.call_args.args[1])
        self.assertEqual(self.store.recipients(self.now), [])
        self.assertNotIn(fixtures.USER, json.dumps(self.store.state))
        self.core.load_ledger.assert_not_called()

    def test_stop_removes_recipient_id_and_blocks_both_reply_and_automatic_delivery(self):
        self.register()
        self.now += timedelta(seconds=1)
        self.post(events=[self.event('配信停止', event_id='stop')])
        self.assertEqual(self.store.recipients(self.now), [])
        self.assertFalse(self.store.is_adult(fixtures.USER, self.now))
        self.assertNotIn(fixtures.USER, json.dumps(self.store.state))
        self.now += timedelta(seconds=1)
        self.post(events=[self.event('買い目', event_id='after-stop')])
        self.assertEqual(self.sender.reply.call_args.args[1], lb.AGE_PROMPT)
        self.core.load_ledger.assert_not_called()

    def test_unfollow_removes_recipient_without_trying_to_reply(self):
        self.register()
        self.sender.reply.reset_mock()
        self.now += timedelta(seconds=1)
        event = self.event(event_id='block')
        event.update(type='unfollow')
        event.pop('message')
        event.pop('replyToken')
        self.assertEqual(self.post(events=[event]).status_code, 200)
        self.assertEqual(self.store.recipients(self.now), [])
        self.assertNotIn(fixtures.USER, json.dumps(self.store.state))
        self.sender.reply.assert_not_called()

    def test_refollow_requires_fresh_consent(self):
        self.register()
        self.now += timedelta(seconds=1)
        event = self.event(event_id='refollow')
        event.update(type='follow')
        event.pop('message')
        self.post(events=[event])
        self.assertEqual(self.store.recipients(self.now), [])
        self.now += timedelta(seconds=1)
        self.register(event_id='fresh-opt-in')
        self.assertEqual(self.store.recipients(self.now), [fixtures.USER])

    def test_old_consent_event_cannot_override_newer_stop(self):
        earlier = self.event(lb.AGE_YES, event_id='delayed-consent')
        self.now += timedelta(seconds=5)
        self.store.set_subscription(fixtures.USER, 'stopped', self.now)
        self.post(events=[earlier])
        self.assertEqual(self.store.recipients(self.now), [])
        self.assertEqual(self.sender.reply.call_args.args[1], lb.AGE_PROMPT)

    def test_same_millisecond_denial_cannot_be_overridden_by_consent(self):
        self.store.set_subscription(fixtures.USER, 'underage', self.now)
        self.register()
        self.assertEqual(self.store.recipients(self.now), [])

    def test_duplicate_consent_event_is_not_replied_to_twice(self):
        event = self.event(lb.AGE_YES, event_id='same-consent')
        self.post(events=[event])
        self.post(events=[event])
        self.sender.reply.assert_called_once()
        self.assertEqual(self.store.recipients(self.now), [fixtures.USER])

    def test_signature_and_destination_checks_precede_subscription_changes(self):
        self.post(events=[self.event(lb.AGE_YES)], signature='invalid')
        self.assertEqual(self.store.recipients(self.now), [])
        self.sender.verify_account.return_value = 'wrong-destination'
        self.post(events=[self.event(lb.AGE_YES)])
        self.assertEqual(self.store.recipients(self.now), [])

    def test_group_consent_and_test_mode_other_user_are_ignored(self):
        event = self.event(lb.AGE_YES)
        event['source']['type'] = 'group'
        self.post(events=[event])
        self.post(events=[self.event(lb.AGE_YES, user=fixtures.OTHER_USER, event_id='other-user')])
        self.assertEqual(self.store.recipients(self.now), [])
        self.sender.reply.assert_not_called()

    def test_stop_during_prediction_read_prevents_prediction_reply(self):
        self.register()
        self.now += timedelta(seconds=1)
        def answer(_text):
            self.store.set_subscription(fixtures.USER, 'stopped', self.now)
            return 'private prediction 1-2-3', lb.timestamp(self.row['scheduled_deadline'])
        with mock.patch.object(lb.SampleBot, 'answer', side_effect=answer):
            self.post(events=[self.event(event_id='concurrent-stop')])
        self.assertEqual(self.sender.reply.call_args.args[1], lb.AGE_PROMPT)
        self.assertNotIn('private prediction', self.sender.reply.call_args.args[1])

    def test_subscription_storage_failure_cannot_disclose_prediction(self):
        with mock.patch.object(self.store, 'is_adult', side_effect=RuntimeError('storage unavailable')):
            self.post()
        self.sender.reply.assert_not_called()
        self.core.load_ledger.assert_not_called()

    def test_no_birthdate_name_or_message_body_is_retained(self):
        self.register()
        profile = self.store.state['subscriptions'][self.store.user_key(fixtures.USER)]
        self.assertEqual(profile['status'], 'adult')
        self.assertEqual(profile['user_id'], fixtures.USER)
        self.assertNotIn('name', profile)
        self.assertNotIn('birthdate', profile)
        self.assertNotIn(lb.AGE_YES, json.dumps(self.store.state, ensure_ascii=False))

    def test_age_choices_use_a_chat_card_with_valid_message_actions(self):
        response = mock.Mock(status_code=200)
        with mock.patch('line_bot.requests.post', return_value=response) as post:
            lb.LineClient('not-a-token').reply('reply-token', lb.AGE_PROMPT,
                                             buttons=lb.AGE_BUTTONS)
        messages = post.call_args.kwargs['json']['messages']
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0], {'type': 'text', 'text': lb.AGE_PROMPT})
        card = messages[1]
        self.assertEqual(card['type'], 'template')
        self.assertTrue(card['altText'])
        self.assertLessEqual(len(card['altText']), 1500)
        template = card['template']
        self.assertEqual(template['type'], 'buttons')
        self.assertLessEqual(len(template['title']), 40)
        self.assertLessEqual(len(template['text']), 60)
        self.assertEqual(len(template['actions']), 2)
        self.assertEqual([action['label'] for action in template['actions']],
                         [lb.AGE_YES, lb.AGE_NO])
        for action in template['actions']:
            self.assertEqual(action['type'], 'message')
            self.assertLessEqual(len(action['label']), 20)
            self.assertLessEqual(len(action['text']), 300)

    def test_card_taps_register_or_decline_without_typed_text(self):
        for index, expected_status in ((0, 'adult'), (1, 'underage')):
            with self.subTest(choice=index):
                self.now += timedelta(seconds=1)
                event = self.event(event_id=f'follow-for-card-{index}')
                event['type'] = 'follow'
                event.pop('message')
                self.post(events=[event])
                card = self.sender.reply.call_args.kwargs['buttons']
                action = card['template']['actions'][index]
                self.now += timedelta(seconds=1)
                self.post(events=[self.event(action['text'], event_id=f'card-tap-{index}')])
                self.assertEqual(self.store.subscription_status(fixtures.USER), expected_status)
                self.assertEqual(self.store.recipients(self.now),
                                 [fixtures.USER] if expected_status == 'adult' else [])
                self.core.load_ledger.assert_not_called()

    def test_stopped_profile_hash_is_expired_during_later_processing(self):
        self.store.set_subscription(fixtures.USER, 'stopped', self.now)
        self.assertEqual(len(self.store.state['subscriptions']), 1)
        self.store.recipients(self.now + timedelta(days=31))
        self.assertEqual(self.store.state['subscriptions'], {})


class AgeDeliveryTests(unittest.TestCase):
    def setUp(self):
        delivery_fixtures.BroadcastTests.setUp(self)

    new_publisher = delivery_fixtures.BroadcastTests.new_publisher
    sample = delivery_fixtures.BroadcastTests.sample

    def test_only_explicitly_confirmed_adults_are_sent_to_multicast(self):
        self.store.set_subscription(fixtures.OTHER_USER, 'underage', self.now)
        self.publisher.run_once()
        payload = self.sender.multicast.call_args.args[0]
        self.assertEqual(payload['to'], [fixtures.USER])
        self.sender.free_quota_available.assert_called_once_with(1)
        self.assertNotIn('to', self.store.read_day(self.now)['broadcast']['payload'])

    def test_no_confirmed_users_means_no_api_call_or_sample_pin(self):
        self.store.state['subscriptions'] = {}
        self.assertEqual(self.publisher.run_once(), 'waiting_for_age_confirmation')
        self.factory.assert_not_called()
        self.assertIsNone(self.store.sample(self.now))

    def test_legacy_profile_without_explicit_consent_is_not_eligible(self):
        key = self.store.user_key(fixtures.USER)
        self.store.state['subscriptions'][key] = {'status': 'adult', 'user_id': fixtures.USER}
        self.assertEqual(self.publisher.run_once(), 'waiting_for_age_confirmation')
        self.sender.multicast.assert_not_called()

    def test_pending_publication_is_cancelled_and_ids_removed_on_withdrawal(self):
        self.sender.multicast.return_value = 'retry'
        self.publisher.run_once()
        self.now += timedelta(seconds=1)
        self.store.set_subscription(fixtures.USER, 'stopped', self.now)
        self.now += timedelta(seconds=45)
        self.assertEqual(self.new_publisher().run_once(), 'cancelled')
        self.sender.multicast.assert_called_once()
        self.assertNotIn(fixtures.USER, json.dumps(self.store.state))

    def test_withdrawal_during_quota_check_prevents_first_send(self):
        def quota(_count):
            self.store.set_subscription(fixtures.USER, 'stopped', self.now)
            return True
        self.sender.free_quota_available.side_effect = quota
        self.assertEqual(self.publisher.run_once(), 'busy')
        self.sender.multicast.assert_not_called()

    def test_new_subscriber_does_not_change_the_frozen_retry_audience(self):
        self.sender.multicast.side_effect = ['retry', 'accepted']
        self.publisher.run_once()
        original = deepcopy(self.sender.multicast.call_args.args)
        self.store.set_subscription(fixtures.OTHER_USER, 'adult', self.now)
        self.now += timedelta(seconds=20)
        self.publisher.run_once()
        self.assertEqual(self.sender.multicast.call_args.args, original)

    def test_old_unrestricted_broadcast_state_is_never_replayed(self):
        day = self.store.read_day(self.now)
        day['broadcast'] = {'status': 'retry', 'sample_id': self.row['id'],
                            'payload': {'messages': [{'type': 'text', 'text': 'old'}]}}
        self.store.mutate(lambda state: state['days'].__setitem__(self.row['date'], day))
        self.assertEqual(self.publisher.run_once(), 'legacy_delivery_blocked')
        self.sender.multicast.assert_not_called()

    def test_http_client_requires_specific_recipients_and_does_not_fallback_to_broadcast(self):
        client = delivery.BroadcastClient('not-a-token')
        with mock.patch('line_broadcast.requests.post') as post:
            self.assertEqual(client.multicast({'messages': []}, 'retry-key'), 'failed')
            self.assertEqual(client.multicast({'to': [], 'messages': []}, 'retry-key'), 'failed')
            self.assertEqual(client.multicast({'to': ['bad-id'], 'messages': []}, 'retry-key'), 'failed')
            post.assert_not_called()

    def test_free_quota_must_cover_the_whole_confirmed_audience(self):
        client = delivery.BroadcastClient('not-a-token')
        quota, usage = mock.Mock(status_code=200), mock.Mock(status_code=200)
        quota.json.return_value = {'type': 'limited', 'value': 200}
        usage.json.return_value = {'totalUsage': 199}
        with mock.patch('line_broadcast.requests.get', side_effect=[quota, usage]):
            self.assertFalse(client.free_quota_available(2))


@unittest.skipUnless(os.getenv('LINE_TEST_DATABASE_URL'), 'isolated PostgreSQL test database not configured')
class PostgresAgeTests(unittest.TestCase):
    setUp = fixtures.PostgresLineStoreTests.setUp

    def test_newer_unsubscribe_wins_even_when_older_consent_finishes_later(self):
        stamp = int(self.now.timestamp() * 1000)
        def change(index):
            status, event_ms = ('adult', stamp) if index % 2 else ('stopped', stamp + 1)
            return lb.LineStore().set_subscription(fixtures.USER, status, self.now, event_ms)
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(change, range(8)))
        restarted = lb.LineStore()
        self.assertFalse(restarted.is_adult(fixtures.USER, self.now))
        self.assertEqual(restarted.recipients(self.now), [])

    def test_confirmed_recipients_survive_restart_and_withdrawal_removes_id(self):
        lb.LineStore().set_subscription(fixtures.USER, 'adult', self.now)
        self.assertEqual(lb.LineStore().recipients(self.now), [fixtures.USER])
        delivery.BroadcastStore().set_subscription(fixtures.USER, 'stopped', self.now + timedelta(seconds=1))
        self.assertEqual(lb.LineStore().recipients(self.now), [])
        self.assertNotIn(fixtures.USER, json.dumps(lb.LineStore().mutate(lambda state: state)))


if __name__ == '__main__':
    unittest.main()

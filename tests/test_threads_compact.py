"""Dated multi-venue scopes and attachment-free saved forecast summaries."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import os
import unittest
from unittest import mock

import threads_publisher as tp
from buffer_publisher import BufferClient
from test_threads_publisher import MemoryStore, forecast


class CompactMultiVenueTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 20, 10, 20, tzinfo=tp.JST)
        patch = mock.patch.dict(os.environ, {
            'BOAT_THREADS_ENABLED': '1', 'BOAT_THREADS_PROVIDER': 'buffer',
            'BOAT_THREADS_MODE': 'daily', 'BOAT_THREADS_FROM_RACE': '1',
            'BOAT_THREADS_STADIUM': '18', 'BOAT_THREADS_START_DATE': '20261019',
            'BOAT_THREADS_UNTIL_DATE': '20261023',
            'BOAT_THREADS_STADIUMS': '09,12', 'BOAT_THREADS_STADIUMS_DATE': '20261020',
            'BOAT_THREADS_POST_FORMAT': 'compact_text',
            'BUFFER_API_KEY': 'private-test-key', 'BOAT_BUFFER_CHANNEL_ID': 'channel-1',
        }, clear=True)
        patch.start(); self.addCleanup(patch.stop)
        self.config = tp.configuration(self.now)

    @staticmethod
    def row(stadium='09', race=1):
        row = forecast()
        row.update(date='20261020', stadium=stadium, race=race,
                   id=f'auto:20261020:{stadium}:{race:02d}')
        for key in ('prediction_saved_at', 'exhibition_detected_at', 'scheduled_deadline'):
            row[key] = row[key].replace('2026-10-15', '2026-10-20')
        return row

    def test_exact_requested_body_uses_saved_names_and_times_without_extra_content(self):
        row = self.row('12', 12)
        row['prediction']['boats'][0]['name'] = '松田 大志郎 福岡/福岡 38歳/52kg'
        before = deepcopy(row)
        entry = tp.candidate(row, self.config, self.now)
        self.assertEqual(entry['text'],
            '【10/20 住之江12R】\n\n本命：①松田大志郎\n軸：④選手4\n穴：③選手3\n\n'
            '10:16保存／締切10:30')
        self.assertEqual(entry['format'], 'compact-text-v1')
        self.assertEqual(row, before)
        for field in ('image_url', 'image_data', 'image_hash', 'asset_ids'):
            self.assertNotIn(field, entry)

    def test_both_venues_cover_all_twelve_races_and_never_render_images(self):
        with mock.patch('ticket_image.freeze') as freeze:
            for stadium, venue in [('09', '津'), ('12', '住之江')]:
                for race in range(1, 13):
                    with self.subTest(stadium=stadium, race=race):
                        entry = tp.candidate(self.row(stadium, race), self.config, self.now)
                        self.assertTrue(entry['text'].startswith(f'【10/20 {venue}{race}R】\n\n'))
            freeze.assert_not_called()
        for stadium in ('04', '18', '23'):
            self.assertIsNone(tp.candidate(self.row(stadium), self.config, self.now))

    def test_dated_override_uses_jst_and_does_not_replace_the_other_days_scope(self):
        self.assertEqual(self.config.target_stadiums, ('09', '12'))
        self.assertEqual(tp.configuration(self.now.astimezone(timezone.utc)), self.config)
        for day in (19, 21, 23):
            self.assertEqual(tp.configuration(self.now.replace(day=day)).target_stadiums, ('18',))
        last = self.now.replace(day=24)
        self.assertTrue(tp.scope_finished(tp.configuration(last), last))

    def test_partial_invalid_or_duplicate_scope_and_unknown_format_fail_closed(self):
        for values in ({'BOAT_THREADS_STADIUMS': ''},
                       {'BOAT_THREADS_STADIUMS': '09,99'},
                       {'BOAT_THREADS_STADIUMS': '09,09'},
                       {'BOAT_THREADS_STADIUMS': '09,,12'},
                       {'BOAT_THREADS_STADIUMS_DATE': '20260230'},
                       {'BOAT_THREADS_POST_FORMAT': 'unknown'}):
            with self.subTest(values=values), mock.patch.dict(os.environ, values):
                self.assertIsNone(tp.configuration(self.now))

    def test_eligibility_still_requires_complete_exhibition_and_distinct_saved_picks(self):
        for changes in ('exhibition', 'picks', 'identity', 'settled'):
            with self.subTest(changes=changes):
                row = self.row()
                if changes == 'exhibition': row['prediction']['boats'][0]['exhibition'] = None
                elif changes == 'picks': row['prediction']['hole'] = row['prediction']['main']
                elif changes == 'identity': row['id'] = 'auto:20261020:12:01'
                else: row['settled'] = True
                self.assertIsNone(tp.candidate(row, self.config, self.now))
        promotion = tp.LinePromotion('20261020', 1, 'https://lin.ee/test', '09')
        self.assertIsNone(tp.candidate(self.row(), replace(self.config, line_promotion=promotion), self.now))

    def test_outbox_claims_and_confirms_only_the_selected_venues(self):
        store = MemoryStore()
        entries = [dict(key=self.row(s)['id'], deadline=self.row(s)['scheduled_deadline'],
                        provider='buffer', format='compact-text-v1', state='queued', attempts=0)
                   for s in ('18', '09', '12')]
        store.enqueue(entries)
        self.assertEqual([store.claim(self.config, self.now)['key'] for _ in range(2)],
                         [entries[1]['key'], entries[2]['key']])
        self.assertIsNone(store.claim(self.config, self.now))
        for entry in store.state['posts'].values(): entry['state'] = 'submitted'
        self.assertEqual([store.claim_confirmation(self.config, self.now)['key'] for _ in range(2)],
                         [entries[1]['key'], entries[2]['key']])
        self.assertNotIn('next_check_at', store.state['posts'][entries[0]['key']])

    @mock.patch('automation.configured', return_value=True)
    def test_all_24_races_publish_once_without_attachments_and_confirm_after_restart(self, configured):
        rows = [self.row(s, r) for s in ('09', '12') for r in range(1, 13)]
        rows.append(self.row('18'))
        core = mock.Mock(); core.load_publication_records.side_effect = lambda *args: core.load_ledger(); core.load_ledger.return_value = rows
        store = MemoryStore(); client = mock.Mock()
        client.create.side_effect = [f'buffer-{n}' for n in range(24)]
        client.publish.side_effect = lambda post, text: post
        client.permalink.side_effect = BufferClient.permalink
        clock = lambda: self.now
        publisher = tp.Publisher(core, self.config, store, client, clock=clock)
        publisher.run_once()
        restarted = tp.Publisher(core, self.config, store, client, clock=clock)
        restarted.run_once()
        self.assertEqual(client.create.call_count, 24)
        self.assertEqual(client.publish.call_count, 24)
        for call in client.create.call_args_list + client.validate_draft.call_args_list + client.publish.call_args_list:
            self.assertFalse(call.kwargs)
        self.assertEqual(len(store.state['posts']), 24)
        self.assertEqual({p['state'] for p in store.state['posts'].values()}, {'submitted'})
        self.now += timedelta(seconds=21)
        client.publication.side_effect = lambda post, text: {
            'id': post, 'status': 'sent', 'sentAt': self.now.isoformat(),
            'externalLink': f'https://www.threads.com/@ton123kyotei456/post/{post}'}
        restarted.run_once(); restarted.run_once()
        self.assertEqual({p['state'] for p in store.state['posts'].values()}, {'published'})
        self.assertEqual(client.publish.call_count, 24)

    def test_text_only_setting_never_claims_a_prepared_image(self):
        store = MemoryStore()
        ticket = tp.candidate(self.row(), replace(self.config, post_format='auto'), self.now)
        store.enqueue([ticket])
        self.assertIsNone(store.claim(self.config, self.now))

    @mock.patch('automation.configured', return_value=True)
    def test_public_status_reports_both_venues_and_no_images_without_private_values(self, configured):
        store = MemoryStore()
        for stadium in ('09', '12'):
            store.enqueue([tp.candidate(self.row(stadium), self.config, self.now)])
        with mock.patch.object(tp, 'now_jst', return_value=self.now), \
                mock.patch.object(tp, 'PostStore', return_value=store):
            status = tp.public_status()
        self.assertEqual(status['venues'], ['津', '住之江'])
        self.assertEqual(status['stadiums'], ['09', '12'])
        self.assertEqual(status['venue'], '津・住之江')
        self.assertEqual(status['post_format'], 'compact_text')
        self.assertFalse(status['with_images'])
        self.assertEqual(len(status['posts']), 2)
        self.assertNotIn('private-test-key', str(status))


if __name__ == '__main__':
    unittest.main()

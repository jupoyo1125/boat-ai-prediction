"""An operator's dated main-pick constraint must not leak across races or days."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
import os
import unittest
from unittest import mock

import threads_publisher as tp
from test_threads_publisher import MemoryStore, forecast


class MainConditionTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 21, 10, 20, tzinfo=tp.JST)
        env = mock.patch.dict(os.environ, {
            'BOAT_THREADS_ENABLED': '1', 'BOAT_THREADS_PROVIDER': 'buffer',
            'BOAT_THREADS_MODE': 'daily', 'BOAT_THREADS_FROM_RACE': '1',
            'BOAT_THREADS_START_DATE': '20261019', 'BOAT_THREADS_UNTIL_DATE': '20261023',
            'BOAT_THREADS_STADIUM': '18', 'BOAT_THREADS_STADIUMS': '16',
            'BOAT_THREADS_STADIUMS_DATE': '20261021',
            'BOAT_THREADS_POST_FORMAT': 'compact_text',
            'BOAT_THREADS_EXCLUDE_MAIN': '20261021:16:1',
            'BUFFER_API_KEY': 'test-key', 'BOAT_BUFFER_CHANNEL_ID': 'test-channel',
        }, clear=True)
        env.start(); self.addCleanup(env.stop)
        self.config = tp.configuration(self.now)

    @staticmethod
    def row(race=1, stadium='16', day=21):
        row = forecast()
        row.update(id=f'auto:202610{day:02d}:{stadium}:{race:02d}',
                   date=f'202610{day:02d}', stadium=stadium, race=race)
        for key in ('prediction_saved_at', 'exhibition_detected_at', 'scheduled_deadline'):
            row[key] = row[key].replace('2026-10-15', f'2026-10-{day:02d}')
        scores = {1: 100, 2: 60, 3: 95, 4: 70, 5: 50, 6: 80}
        for boat in row['prediction']['boats']: boat['score'] = scores[boat['boat']]
        return row

    def test_body_uses_highest_eligible_score_and_preserves_the_original_forecast(self):
        row = self.row(12); before = deepcopy(row)
        entry = tp.candidate(row, self.config, self.now)
        self.assertEqual(entry['text'],
            '【10/21 児島12R】\n\n本命：③選手3\n軸：①選手1\n穴：⑥選手6\n\n'
            '10:16保存／締切10:30')
        self.assertEqual(entry['selected_picks'], {'main': 3, 'second': 1, 'hole': 6})
        self.assertEqual(entry['main_condition'], self.config.main_condition.snapshot())
        self.assertEqual(row, before)
        self.assertEqual(row['prediction']['main'], 1)
        self.assertNotIn('image_url', entry)

    def test_all_twelve_races_choose_from_two_to_six_by_their_saved_scores(self):
        with mock.patch('ticket_image.freeze') as freeze:
            for race in range(1, 13):
                row = self.row(race)
                best = 2 + (race - 1) % 5
                for boat in row['prediction']['boats']:
                    boat['score'] = 100 if boat['boat'] == 1 else 99 if boat['boat'] == best else 50
                entry = tp.candidate(row, self.config, self.now)
                with self.subTest(race=race):
                    self.assertEqual(entry['selected_picks']['main'], best)
                    self.assertEqual(len(set(entry['selected_picks'].values())), 3)
            freeze.assert_not_called()

    def test_conditions_use_frozen_scores_with_deterministic_ties(self):
        row = self.row()
        for boat in row['prediction']['boats']: boat['score'] = 100 if boat['boat'] == 1 else 50
        row['prediction']['boats'].reverse()
        self.assertEqual(tp.candidate(row, self.config, self.now)['selected_picks'],
                         {'main': 2, 'second': 1, 'hole': 3})

    def test_missing_or_invalid_scores_and_exhibition_never_produce_guessed_picks(self):
        for value in (None, True, '99', float('nan'), float('inf')):
            row = self.row(); row['prediction']['boats'][0]['score'] = value
            with self.subTest(value=value):
                self.assertIsNone(tp.candidate(row, self.config, self.now))
        row = self.row(); row['prediction']['boats'][2]['exhibition'] = None
        self.assertIsNone(tp.candidate(row, self.config, self.now))

    def test_another_venue_or_day_keeps_its_normal_saved_main(self):
        config = replace(self.config, stadiums=('16', '18'))
        entry = tp.candidate(self.row(stadium='18'), config, self.now)
        self.assertIn('本命：①選手1', entry['text'])
        self.assertNotIn('main_condition', entry)
        tomorrow = self.now.replace(day=22)
        config = tp.configuration(tomorrow)
        self.assertEqual(config.target_stadiums, ('18',))
        entry = tp.candidate(self.row(stadium='18', day=22), config, tomorrow)
        self.assertIn('本命：①選手1', entry['text'])
        self.assertNotIn('main_condition', entry)

    def test_invalid_condition_and_incompatible_format_or_scope_disable_publication(self):
        for value in ('1', '20260230:16:1', '20261021:99:1', '20261021:16:1,1',
                      '20261021:16:1,2,3,4,5,6', '20261021:16:0', '20261021:16:'):
            with self.subTest(value=value), mock.patch.dict(os.environ, {'BOAT_THREADS_EXCLUDE_MAIN': value}):
                self.assertIsNone(tp.configuration(self.now))
        for values in ({'BOAT_THREADS_POST_FORMAT': 'auto'}, {'BOAT_THREADS_STADIUMS': '18'}):
            with self.subTest(values=values), mock.patch.dict(os.environ, values):
                self.assertIsNone(tp.configuration(self.now))

    def test_a_draft_prepared_before_the_condition_is_not_claimed(self):
        row = self.row(); store = MemoryStore()
        unconditioned = tp.candidate(row, replace(self.config, main_condition=None), self.now)
        store.enqueue([unconditioned])
        self.assertIsNone(store.claim(self.config, self.now))
        self.assertIn('本命：①選手1', store.read()['posts'][row['id']]['text'])

    @mock.patch('automation.configured', return_value=True)
    def test_restart_sends_each_conditioned_forecast_once_without_attachments(self, configured):
        core = mock.Mock(); core.load_publication_records.side_effect = lambda *args: core.load_ledger(); core.load_ledger.return_value = [self.row(r) for r in range(1, 13)]
        store = MemoryStore(); client = mock.Mock()
        client.create.side_effect = [f'buffer-{r}' for r in range(1, 13)]
        client.publish.side_effect = lambda post, text: post
        publisher = tp.Publisher(core, self.config, store, client, clock=lambda: self.now)
        publisher.run_once()
        restarted = tp.Publisher(core, self.config, store, client, clock=lambda: self.now)
        restarted.run_once()
        self.assertEqual(client.publish.call_count, 12)
        for call in client.create.call_args_list + client.publish.call_args_list:
            self.assertFalse(call.kwargs)
        self.assertTrue(all(p['selected_picks']['main'] == 3 for p in store.read()['posts'].values()))
        self.assertEqual({p['state'] for p in store.read()['posts'].values()}, {'submitted'})

    @mock.patch('automation.configured', return_value=True)
    def test_status_reports_the_exact_active_condition_without_private_scores(self, configured):
        with mock.patch.object(tp, 'now_jst', return_value=self.now), \
                mock.patch.object(tp, 'PostStore', return_value=MemoryStore()):
            status = tp.public_status()
        self.assertEqual(status['venues'], ['児島'])
        self.assertEqual(status['main_condition'], {**self.config.main_condition.snapshot(), 'active_today': True})
        self.assertEqual(status['post_format'], 'compact_text')
        self.assertFalse(status['with_images'])
        self.assertNotIn('test-key', str(status))


if __name__ == '__main__':
    unittest.main()

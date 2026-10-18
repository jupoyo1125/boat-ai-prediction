"""Venue switches and dated LINE conditions must not leak other forecasts."""
import base64
from contextlib import ExitStack, nullcontext
from copy import deepcopy
from datetime import datetime, timedelta
from itertools import permutations
import os
import unittest
from unittest import mock

from flask import Flask

import app as core
import line_bot as lb
import line_prediction as lp
import model
import threads_publisher as tp
import ticket_image as ti
from test_automation import MINIMAL_BEFORE
from test_line_bot import forecast as line_row
from test_prediction_features import snapshot
from test_ranking_preview import RACELIST
from test_threads_publisher import MemoryStore, forecast


class VenueScopeTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 19, 10, 20, tzinfo=tp.JST)
        patch = mock.patch.dict(os.environ, {
            'BOAT_THREADS_ENABLED': '1', 'BOAT_THREADS_MODE': 'daily',
            'BOAT_THREADS_PROVIDER': 'buffer', 'BOAT_THREADS_STADIUM': '23',
            'BOAT_THREADS_START_DATE': '20261019', 'BOAT_THREADS_UNTIL_DATE': '20261023',
            'BOAT_THREADS_FROM_RACE': '1', 'BUFFER_API_KEY': 'test-key',
            'BOAT_BUFFER_CHANNEL_ID': 'test-channel',
        }, clear=True)
        patch.start(); self.addCleanup(patch.stop)
        self.config = tp.configuration(self.now)

    @staticmethod
    def row(stadium='23', race=1):
        row = forecast()
        row.update(id=f'auto:20261019:{stadium}:{race:02d}', date='20261019',
                   stadium=stadium, race=race)
        for key in ('prediction_saved_at', 'exhibition_detected_at', 'scheduled_deadline'):
            row[key] = row[key].replace('2026-10-15', '2026-10-19')
        return row

    def test_all_twelve_karatsu_races_have_correct_text_images_and_venue_identity(self):
        self.assertEqual(self.config.stadium, '23')
        for race in range(1, 13):
            with self.subTest(race=race):
                row = self.row(race=race)
                entry = tp.candidate(row, self.config, self.now)
                self.assertIn(f'唐津{race}R', entry['text'])
                self.assertNotIn('平和島', entry['text'])
                self.assertNotIn('http', entry['text'])
                self.assertIn(f'/20261019/23/{race}/', entry['image_url'])
                self.assertEqual(ti.snapshot(row)['venue'], '唐津')
                self.assertIsNone(tp.candidate(self.row('04', race), self.config, self.now))

    def test_new_venue_cannot_claim_or_confirm_another_venues_outbox(self):
        store = MemoryStore()
        entries = [dict(key=self.row(s)['id'], deadline=self.row(s)['scheduled_deadline'],
                        provider='buffer', state='queued', attempts=0) for s in ('04', '23')]
        store.enqueue(entries)
        self.assertEqual(store.claim(self.config, self.now)['key'], entries[1]['key'])
        for entry in store.state['posts'].values():
            entry['state'] = 'submitted'
        self.assertEqual(store.claim_confirmation(self.config, self.now)['key'], entries[1]['key'])
        self.assertNotIn('next_check_at', store.state['posts'][entries[0]['key']])

    def test_ends_after_the_final_day_and_rejects_unknown_venues(self):
        last_day = self.now.replace(day=23)
        self.assertTrue(tp.scope_active(tp.configuration(last_day), last_day))
        tomorrow = last_day + timedelta(days=1)
        self.assertTrue(tp.scope_finished(tp.configuration(tomorrow), tomorrow))
        self.assertFalse(tp.scope_active(tp.configuration(tomorrow), tomorrow))
        with mock.patch.dict(os.environ, {'BOAT_THREADS_STADIUM': '99'}):
            self.assertIsNone(tp.configuration(self.now))

    def test_image_urls_are_isolated_by_venue_and_old_heiwajima_urls_still_work(self):
        store = MemoryStore()
        rows = [self.row(s) for s in ('04', '23')]
        entries = [dict(ti.freeze(r), key=r['id']) for r in rows]
        store.enqueue(entries)
        app = Flask(__name__); ti.register_route(app, lambda: store)
        client = app.test_client()
        for entry in entries:
            response = client.get(entry['image_url'].removeprefix(ti.ORIGIN))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, base64.b64decode(entry['image_data']))
        karatsu = entries[1]['image_url'].removeprefix(ti.ORIGIN)
        self.assertEqual(client.get(karatsu.replace('/23/', '/04/')).status_code, 404)
        self.assertEqual(client.get(karatsu.replace('/23/', '/99/')).status_code, 404)


class FixedLineSelectionTests(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {
            'BOAT_LINE_SAMPLE_RACE': '20261019:02:12',
            'BOAT_LINE_FIXED_FIRST': '20261019:02:12:4',
        })
        env.start(); self.addCleanup(env.stop)
        self.now = datetime(2026, 10, 19, 16, 10, tzinfo=lb.JST)
        self.boats = [{'boat': b, 'score': 50} for b in range(1, 7)]
        self.odds = {''.join(map(str, p)): 100.0 for p in permutations(range(1, 7), 3)}
        self.venues = {'02': '戸田', '23': '唐津'}

    def prepared(self):
        return lp.prepare_forecast('20261019', '02', 12, self.boats, self.odds,
                                   '逃げ', model.DEFAULT, core.build_bets)

    def row(self):
        row = line_row(date='20261019', stadium='02', race=12,
                       saved='16:01:00', deadline='16:25:00')
        row['prediction'] = {'line_forecast': self.prepared()}
        return row

    def test_fixed_head_applies_only_to_the_exact_dated_race_without_changing_inputs(self):
        before = deepcopy((self.boats, self.odds, model.DEFAULT))
        special = self.prepared()
        groups = {c: [b['bet'] for b in special['bets'] if b['category']==c]
                  for c in ('gachi', 'roman', 'oni')}
        self.assertEqual([len(groups[c]) for c in groups], [10, 10, 3])
        self.assertTrue(all(b['bet'].startswith('4-') for b in special['bets']))
        self.assertFalse(set(groups['gachi']) & set(groups['roman']))
        self.assertTrue(set(groups['oni']) <= set(groups['gachi']))
        self.assertEqual((self.boats, self.odds, model.DEFAULT), before)
        for scope in [('20261020', '02', 12), ('20261019', '23', 12), ('20261019', '02', 11)]:
            self.assertIsNone(lp.fixed_first_for_race(*scope))

    def test_reply_uses_the_saved_fixed_selection_and_labels_the_operator_condition(self):
        row = self.row(); before = deepcopy(row)
        sample = lb.frozen_sample(row, self.venues, self.now)
        self.assertEqual(sample['fixed_first'], 4)
        self.assertEqual(lb.designated_race(self.venues, self.now)['fixed_first'], 4)
        self.assertTrue(all(c.startswith('4-') for group in sample['groups'].values() for c in group))
        self.assertIn('指定条件：4号艇を1着固定', lb.format_sample(sample))
        self.assertIn('戸田12R', lb.format_sample(sample))
        self.assertEqual(row, before)

    def test_missing_wrong_or_tampered_fixed_snapshot_never_falls_back_to_normal_picks(self):
        row = self.row()
        variants = [None, {'rule': lp.FIXED_FIRST_RULE, 'first': 3, 'bets': row['bets']},
                    dict(self.prepared(), bets=[{'category': 'gachi', 'bet': '1-2-3'}])]
        for special in variants:
            with self.subTest(special=special):
                row['prediction']['line_forecast'] = special
                self.assertIsNone(lb.frozen_sample(row, self.venues, self.now))
        with mock.patch.dict(os.environ, {'BOAT_LINE_FIXED_FIRST': '20261019:02:12:9'}):
            self.assertIsNone(lb.designated_race(self.venues, self.now))
            self.assertIsNone(lb.frozen_sample(self.row(), self.venues, self.now))

    def test_prediction_prepares_line_snapshot_and_freezes_it_without_changing_site_bets(self):
        before = core.parse_before(MINIMAL_BEFORE)
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(core, 'load_model', return_value=deepcopy(model.DEFAULT)))
            stack.enter_context(mock.patch.object(core.course_history, 'get', return_value=snapshot('20261019')))
            stack.enter_context(mock.patch.object(core, 'submit_official_page', return_value=mock.Mock()))
            stack.enter_context(mock.patch.object(core, 'await_official_page', return_value=RACELIST))
            stack.enter_context(mock.patch.object(core, 'parse_odds', return_value=self.odds))
            with mock.patch.dict(os.environ, {'BOAT_LINE_FIXED_FIRST': ''}):
                ordinary = core.predict_race('20261019', '02', 12, before=before)
            selected = core.predict_race('20261019', '02', 12, before=before)
            self.assertEqual({k:v for k,v in selected.items() if k!='line_forecast'}, ordinary)
            self.assertTrue(all(b['bet'].startswith('4-') for b in selected['line_forecast']['bets']))
            stack.enter_context(mock.patch.object(core, 'atomic_state', return_value=nullcontext()))
            stack.enter_context(mock.patch.object(core, 'load_ledger', return_value=[]))
            saved = stack.enter_context(mock.patch.object(core, 'save_ledger'))
            row = core.save_automatic_prediction('20261019', '02', 12, selected,
                self.now.replace(minute=25), self.now-timedelta(seconds=1), clock=lambda:self.now)
            self.assertEqual(row['bets'], core.normalize_category_bets(ordinary['bets']))
            self.assertEqual(row['prediction']['line_forecast'], selected['line_forecast'])
            self.assertEqual(row['predicted_first'], ordinary['main'])
            self.assertIsNotNone(lb.frozen_sample(row, self.venues, self.now))
            saved.assert_called_once()


if __name__ == '__main__':
    unittest.main()

"""Exercise operator constraints through saved predictions and LINE delivery."""
from contextlib import ExitStack, nullcontext
from copy import deepcopy
from datetime import datetime, timedelta
from itertools import permutations
import os
import unittest
from unittest import mock

import app as core
import line_bot as lb
import line_broadcast as broadcast
import line_prediction as lp
import model
from test_automation import MINIMAL_BEFORE
from test_line_bot import forecast, USER
from test_line_broadcast import MemoryStore
from test_prediction_features import snapshot
from test_ranking_preview import RACELIST


class LinePartnerTests(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {
            'BOAT_LINE_SAMPLE_RACE': '20261020:08:12',
            'BOAT_LINE_FIXED_FIRST': '20261020:08:12:1:5,6',
            'BOAT_LINE_BROADCAST_ENABLED': '1',
        }, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.now = datetime(2026, 10, 20, 16, 10, tzinfo=lb.JST)
        self.boats = [{'boat': b, 'score': 50} for b in range(1, 7)]
        self.odds = {''.join(map(str, p)): 100.0 for p in permutations(range(1, 7), 3)}

    def prepared(self):
        return lp.prepare_forecast('20261020', '08', 12, self.boats,
                                   self.odds, '逃げ', model.DEFAULT, core.build_bets)

    def row(self):
        row = forecast(date='20261020', stadium='08', race=12,
                       saved='16:01:00', deadline='16:25:00')
        row['prediction'] = {'line_forecast': self.prepared()}
        return row

    def test_selection_uses_either_partner_without_renormalizing_or_mutating_inputs(self):
        before = deepcopy((self.boats, self.odds, model.DEFAULT))
        selected = self.prepared()
        groups = {c: {b['bet'] for b in selected['bets'] if b['category'] == c}
                  for c in ('gachi', 'roman', 'oni')}
        expected = {f'1-{b}-{c}' for b, c in permutations(range(2, 7), 2)
                    if b in (5, 6) or c in (5, 6)}
        self.assertEqual(groups['gachi'] | groups['roman'], expected)
        self.assertEqual([len(groups[c]) for c in groups], [10, 4, 3])
        self.assertFalse(groups['gachi'] & groups['roman'])
        self.assertTrue(groups['oni'] <= groups['gachi'])
        unrestricted = {b['bet']: b for b in core.build_bets(
            self.boats, self.odds, '1', '逃げ', model=model.DEFAULT,
            exclude_gachi_from_roman=True)}
        for bet in selected['bets']:
            self.assertEqual(bet['probability'], unrestricted[bet['bet']]['probability'])
            self.assertEqual(bet['ev'], unrestricted[bet['bet']]['ev'])
        self.assertEqual((self.boats, self.odds, model.DEFAULT), before)

    def test_dated_scope_validation_and_backward_compatibility(self):
        for scope in [('20261021', '08', 12), ('20261020', '02', 12), ('20261020', '08', 11)]:
            self.assertIsNone(lp.condition_for_race(*scope))
        for value in ['20261020:08:12:1:1,5', '20261020:08:12:1:5,5',
                      '20261020:08:12:1:5,7', '20261020:08:12:1:']:
            with self.subTest(value=value), mock.patch.dict(os.environ, {'BOAT_LINE_FIXED_FIRST': value}):
                self.assertIsNone(self.prepared())
                self.assertIsNone(lb.designated_race(core.STADIUMS, self.now))
                self.assertIsNone(lb.frozen_sample(self.row(), core.STADIUMS, self.now))
        with mock.patch.dict(os.environ, {'BOAT_LINE_FIXED_FIRST': '20261020:08:12:1'}):
            special = self.prepared()
            self.assertEqual(special['rule'], lp.FIXED_FIRST_RULE)
            self.assertNotIn('partners', special)
            self.assertIsNotNone(lb.frozen_sample(self.row(), core.STADIUMS, self.now))

    def test_reply_rejects_missing_changed_or_tampered_partner_snapshot(self):
        good = self.row()
        variants = [None, {**self.prepared(), 'rule': lp.FIXED_FIRST_RULE},
                    {**self.prepared(), 'partners': [5]},
                    {**self.prepared(), 'partners': [5.0, 6.0]},
                    {**self.prepared(), 'bets': [{'category': 'gachi', 'bet': '1-2-3'}]},
                    {**self.prepared(), 'bets': [{'category': 'gachi', 'bet': '5-1-6'}]}]
        for special in variants:
            row = deepcopy(good)
            row['prediction']['line_forecast'] = special
            self.assertIsNone(lb.frozen_sample(row, core.STADIUMS, self.now))
        sample = lb.frozen_sample(good, core.STADIUMS, self.now)
        self.assertEqual(sample['partners'], [5, 6])
        self.assertEqual(lb.designated_race(core.STADIUMS, self.now)['partners'], [5, 6])
        self.assertIn('相手軸：5・6号艇（いずれかを2着または3着に含む）', lb.format_sample(sample))

    def test_saved_prediction_broadcasts_once_and_waits_for_matching_snapshot(self):
        store = MemoryStore()
        store.set_subscription(USER, 'adult', self.now)
        sender = mock.Mock()
        sender.free_quota_available.return_value = True
        sender.multicast.return_value = 'accepted'
        row = self.row()
        publisher = broadcast.Publisher(
            core, store=store, clock=lambda: self.now,
            config_provider=lambda: lb.Config('test-secret', 'test-token', 'live'),
            client_factory=lambda token: sender,
            record_loader=lambda record_id: deepcopy(row))
        special = row['prediction'].pop('line_forecast')
        self.assertEqual(publisher.run_once(), 'waiting_for_prediction')
        sender.multicast.assert_not_called()
        row['prediction']['line_forecast'] = special
        for _ in range(2):
            self.assertEqual(publisher.run_once(), 'accepted')
        sender.multicast.assert_called_once()
        text = sender.multicast.call_args.args[0]['messages'][0]['text']
        self.assertIn('10/20 常滑12R', text)
        self.assertIn('指定条件：1号艇を1着固定', text)
        self.assertIn('相手軸：5・6号艇', text)
        self.assertEqual(store.sample(self.now)['partners'], [5, 6])

    def test_predict_save_pipeline_preserves_site_bets_and_line_constraints(self):
        before = core.parse_before(MINIMAL_BEFORE)
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(core, 'load_model', return_value=deepcopy(model.DEFAULT)))
            stack.enter_context(mock.patch.object(core.course_history, 'get', return_value=snapshot('20261020')))
            stack.enter_context(mock.patch.object(core, 'submit_official_page', return_value=mock.Mock()))
            stack.enter_context(mock.patch.object(core, 'await_official_page', return_value=RACELIST))
            stack.enter_context(mock.patch.object(core, 'parse_odds', return_value=self.odds))
            with mock.patch.dict(os.environ, {'BOAT_LINE_FIXED_FIRST': ''}):
                ordinary = core.predict_race('20261020', '08', 12, before=before)
            selected = core.predict_race('20261020', '08', 12, before=before)
            self.assertEqual({k: v for k, v in selected.items() if k != 'line_forecast'}, ordinary)
            self.assertEqual(selected['line_forecast']['partners'], [5, 6])
            stack.enter_context(mock.patch.object(core, 'atomic_state', return_value=nullcontext()))
            stack.enter_context(mock.patch.object(core, 'load_ledger', return_value=[]))
            stack.enter_context(mock.patch.object(core, 'save_ledger'))
            row = core.save_automatic_prediction('20261020', '08', 12, selected,
                self.now.replace(minute=25), self.now - timedelta(seconds=1), clock=lambda: self.now)
            self.assertEqual(row['bets'], core.normalize_category_bets(ordinary['bets']))
            self.assertEqual(row['predicted_first'], ordinary['main'])
            self.assertEqual(lb.frozen_sample(row, core.STADIUMS, self.now)['partners'], [5, 6])


if __name__ == '__main__':
    unittest.main()

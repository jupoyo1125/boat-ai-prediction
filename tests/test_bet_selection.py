"""Selection contract: Oni narrows the Gachi tickets rather than chasing odds."""
import unittest

import app as core
import model


class OniSelectionTests(unittest.TestCase):
    def boats(self, equal=False):
        return [{'boat': boat, 'score': 50 if equal else 95 - boat * 10}
                for boat in range(1, 7)]

    def select(self, odds, fixed='none', equal=False, scenario='逃げ'):
        bets = core.build_bets(self.boats(equal), odds, fixed, scenario,
                               model=model.DEFAULT)
        return {category: [bet for bet in bets if bet['category'] == category]
                for category in core.BET_CATEGORIES}

    def test_high_payout_outside_gachi_cannot_enter_oni(self):
        odds = {''.join(map(str, order)): 10.0
                for order in core.permutations(range(1, 7), 3)}
        # Deliberately inflated test odds must not add an outside longshot.
        for combo in ('615', '653', '635'):
            odds[combo] = 1000000.0
        odds['312'] = 100.0
        selected = self.select(odds)
        gachi = {bet['bet'] for bet in selected['gachi']}
        oni = {bet['bet'] for bet in selected['oni']}
        self.assertEqual((len(gachi), len(oni)), (10, 3))
        self.assertTrue(oni <= gachi)
        self.assertTrue(oni.isdisjoint({'6-1-5', '6-5-3', '6-3-5'}))
        self.assertIn('3-1-2', [bet['bet'] for bet in selected['roman']])

    def test_oni_prioritizes_hit_probability_over_odds_inside_gachi(self):
        odds = {''.join(map(str, order)): 10.0
                for order in core.permutations(range(1, 7), 3)}
        initial = self.select(odds)
        longshot = min(initial['gachi'], key=lambda bet: bet['probability'])
        odds[longshot['bet'].replace('-', '')] = 1000000.0
        selected = self.select(odds)
        self.assertNotIn(longshot['bet'], [bet['bet'] for bet in selected['oni']])
        probabilities = sorted((bet['probability'] for bet in selected['gachi']), reverse=True)
        self.assertEqual([bet['probability'] for bet in selected['oni']], probabilities[:3])

    def test_three_gachi_tickets_are_kept_even_when_all_ev_is_below_one(self):
        odds = {''.join(map(str, order)): 2.0
                for order in core.permutations(range(1, 7), 3)}
        selected = self.select(odds, equal=True)
        self.assertEqual((len(selected['gachi']), len(selected['oni'])), (10, 3))
        self.assertTrue(all(bet['ev'] < 1 for bet in selected['oni']))

    def test_equal_probabilities_use_expected_return_as_tiebreaker(self):
        selected = self.select({'123': 4, '124': 9, '125': 7, '126': 5}, equal=True)
        self.assertEqual([bet['bet'] for bet in selected['oni']], ['1-2-4', '1-2-5', '1-2-6'])
        self.assertEqual(len({bet['probability'] for bet in selected['oni']}), 1)

    def test_incomplete_odds_never_invents_replacement_tickets(self):
        for odds, count in [({}, 0), ({'123': 10}, 1), ({'123': 10, '132': 8}, 2)]:
            with self.subTest(odds=odds):
                selected = self.select(odds)
                self.assertEqual(len(selected['oni']), count)
                self.assertEqual({bet['bet'] for bet in selected['oni']},
                                 {bet['bet'] for bet in selected['gachi']})

    def test_fixed_first_and_scenarios_keep_oni_within_gachi(self):
        odds = {''.join(map(str, order)): 100.0
                for order in core.permutations(range(1, 7), 3)}
        for fixed in ('none', '1', '3', '6'):
            for scenario in ('逃げ', '差し', 'まくり・まくり差し', None):
                with self.subTest(fixed=fixed, scenario=scenario):
                    selected = self.select(odds, fixed=fixed, scenario=scenario)
                    self.assertEqual(len(selected['oni']), 3)
                    self.assertTrue({bet['bet'] for bet in selected['oni']}
                                    <= {bet['bet'] for bet in selected['gachi']})
                    if fixed != 'none':
                        self.assertTrue(all(bet['bet'].startswith(fixed + '-')
                                            for bet in selected['oni']))

    def test_ongoing_legacy_replay_retains_its_original_oni_rule(self):
        odds = {''.join(map(str, order)): 10.0
                for order in core.permutations(range(1, 7), 3)}
        for combo in ('615', '653', '635'):
            odds[combo] = 1000000.0
        bets = core.build_bets(self.boats(), odds, 'none', model=model.LEGACY_DEFAULT)
        self.assertEqual({bet['bet'] for bet in bets if bet['category'] == 'oni'},
                         {'6-1-5', '6-5-3', '6-3-5'})


if __name__ == '__main__':
    unittest.main()

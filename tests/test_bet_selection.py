"""Selection contracts: Roman excludes Gachi; Oni narrows Gachi."""
import unittest

import app as core
import model


class BetSelectionTests(unittest.TestCase):
    def boats(self, equal=False):
        return [{'boat': boat, 'score': 50 if equal else 95 - boat * 10}
                for boat in range(1, 7)]

    def select(self, odds, fixed='none', equal=False, scenario='逃げ'):
        bets = core.build_bets(self.boats(equal), odds, fixed, scenario,
                               model=model.DEFAULT)
        return {category: [bet for bet in bets if bet['category'] == category]
                for category in core.BET_CATEGORIES}

    def test_roman_uses_remaining_candidates_to_fill_ten_without_gachi_overlap(self):
        odds = {''.join(map(str, order)): 100.0
                for order in core.permutations(range(1, 7), 3)}
        for fixed in ('none', '1', '2', '3', '4', '5', '6'):
            for scenario in ('逃げ', '差し', 'まくり・まくり差し', None):
                with self.subTest(fixed=fixed, scenario=scenario):
                    selected = self.select(odds, fixed=fixed, equal=True, scenario=scenario)
                    gachi = {bet['bet'] for bet in selected['gachi']}
                    roman = {bet['bet'] for bet in selected['roman']}
                    oni = {bet['bet'] for bet in selected['oni']}
                    self.assertEqual((len(gachi), len(roman), len(oni)), (10, 10, 3))
                    self.assertTrue(gachi.isdisjoint(roman))
                    self.assertTrue(oni <= gachi)
                    self.assertTrue(oni.isdisjoint(roman))
                    if fixed != 'none':
                        self.assertTrue(all(bet.startswith(fixed + '-') for bet in roman))

    def test_roman_also_excludes_gachi_supplements_when_odds_are_incomplete(self):
        selected = self.select({'123': 60, '456': 70, '651': 80}, equal=True)
        self.assertEqual({bet['bet'] for bet in selected['gachi']},
                         {'1-2-3', '4-5-6', '6-5-1'})
        self.assertEqual(selected['roman'], [])

    def test_roman_shortfall_does_not_relax_odds_or_probability(self):
        odds = {''.join(map(str, order)): 10.0
                for order in core.permutations(range(1, 7), 3)}
        odds.update({'123': 100.0, '312': 50.0, '321': 49.9, '654': 1000000.0})
        selected = self.select(odds)
        self.assertIn('1-2-3', {bet['bet'] for bet in selected['gachi']})
        self.assertEqual([bet['bet'] for bet in selected['roman']], ['3-1-2'])

    def test_legacy_replay_can_retain_overlapping_roman_tickets(self):
        odds = {''.join(map(str, order)): 100.0
                for order in core.permutations(range(1, 7), 3)}
        bets = core.build_bets(self.boats(equal=True), odds, '1', model=model.LEGACY_DEFAULT)
        gachi = {bet['bet'] for bet in bets if bet['category'] == 'gachi'}
        roman = {bet['bet'] for bet in bets if bet['category'] == 'roman'}
        self.assertEqual(gachi, roman)

    def test_existing_current_feature_replay_can_keep_its_original_roman_rule(self):
        odds = {''.join(map(str, order)): 100.0
                for order in core.permutations(range(1, 7), 3)}
        bets = core.build_bets(self.boats(equal=True), odds, '1', model=model.DEFAULT,
                               exclude_gachi_from_roman=False)
        gachi = {bet['bet'] for bet in bets if bet['category'] == 'gachi'}
        roman = {bet['bet'] for bet in bets if bet['category'] == 'roman'}
        self.assertEqual(gachi, roman)

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

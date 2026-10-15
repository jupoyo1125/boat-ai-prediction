from concurrent.futures import Future
import copy
from contextlib import ExitStack
import unittest
from unittest import mock

import app as core
import model


RACELIST = '<table>' + ''.join(
    f'<tr><td>{b}</td><td></td><td>選手{b}</td><td>0.{12+b}</td>'
    f'<td>{8-b/3:.2f}</td><td>{6+b/7:.2f}</td><td>42 {30+b*3:.1f}</td></tr>'
    for b in range(1, 7)) + '</table>'
HISTORY = {'races': 36, 'first_win_rate': {str(b): 35 if b == 1 else 13 for b in range(1, 7)}}


class RankingPreviewTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.urls = []
        self.racelist = RACELIST
        self.stack.enter_context(mock.patch.object(core, 'historical_stats', return_value=HISTORY))
        self.stack.enter_context(mock.patch.object(core, 'load_model', return_value=copy.deepcopy(model.DEFAULT)))
        self.stack.enter_context(mock.patch.object(core, 'submit_official_page', side_effect=self.page))
        self.save = self.stack.enter_context(mock.patch.object(core, 'save_ledger'))

    def page(self, url, deadline):
        self.urls.append(url)
        page = Future()
        page.set_result(self.racelist if 'racelist?' in url else '<html></html>')
        return page

    def test_preview_works_without_published_odds_and_does_not_create_bets_or_save(self):
        with mock.patch.object(core, 'parse_odds', side_effect=AssertionError('odds unavailable')) as odds, \
                mock.patch.object(core, 'build_bets', side_effect=AssertionError('no bets')) as bets:
            result = core.predict_race('20261016', '04', 1, days=3, ranking_only=True)
        self.assertTrue(result['ranking_only'])
        self.assertEqual((result['date'], result['stadium'], result['race']), ('20261016', '04', 1))
        self.assertEqual(len(result['boats']), 6)
        self.assertEqual(result['bets'], [])
        self.assertEqual(result['odds_count'], 0)
        self.assertIn('オッズと買い目は含みません', result['notice'])
        self.assertFalse(any('odds3t?' in url for url in self.urls))
        self.assertTrue(any('beforeinfo?' in url for url in self.urls))
        odds.assert_not_called(); bets.assert_not_called(); self.save.assert_not_called()

    def test_preview_keeps_the_existing_scores_and_ranking(self):
        with mock.patch.object(core, 'parse_odds', return_value={'123': 10}), \
                mock.patch.object(core, 'build_bets', return_value=[{'bet': '1-2-3'}]):
            regular = core.predict_race('20261016', '04', 1, days=3)
            preview = core.predict_race('20261016', '04', 1, days=3, ranking_only=True)
        self.assertEqual(preview['boats'], regular['boats'])
        for field in ['main', 'second', 'hole', 'features', 'history', 'weather', 'scenario']:
            self.assertEqual(preview[field], regular[field])
        self.assertNotIn('ranking_only', regular)
        self.assertEqual(regular['bets'], [{'bet': '1-2-3'}])
        self.save.assert_not_called()

    def test_rank_only_api_is_explicit_read_only_and_not_cached(self):
        response = core.app.test_client().get(
            '/api/analyze?date=20261016&stadium=04&race=12&history_days=3&view=ranking')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(response.json['race'], 12)
        self.assertTrue(response.json['ranking_only'])
        self.assertEqual(response.json['bets'], [])
        self.save.assert_not_called()

    def test_regular_api_still_stops_when_odds_cannot_be_parsed(self):
        with mock.patch.object(core, 'parse_odds', side_effect=ValueError('odds not published')), \
                mock.patch.object(core, 'build_bets') as bets:
            response = core.app.test_client().get(
                '/api/analyze?date=20261016&stadium=04&race=1&history_days=3')
        self.assertEqual(response.status_code, 502)
        self.assertFalse(response.json['ok'])
        self.assertIn('odds not published', response.json['error'])
        bets.assert_not_called(); self.save.assert_not_called()

    def test_preview_does_not_invent_missing_entries(self):
        self.racelist = '<table><tr><td>1</td></tr></table>'
        response = core.app.test_client().get(
            '/api/analyze?date=20261016&stadium=04&race=1&view=ranking')
        self.assertEqual(response.status_code, 502)
        self.assertIn('6艇', response.json['error'])
        self.assertNotIn('main', response.json)
        self.save.assert_not_called()


if __name__ == '__main__':
    unittest.main()

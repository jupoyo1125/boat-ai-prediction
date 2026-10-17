import copy
from datetime import datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

import app as core
import course_history_store as store
import historical
import model
from prediction_features import FEATURE_SCHEMA, analyze_features, meeting_results
from racer_course_history import (year_window, parse_course_results, aggregate_days, racer_rates)
from test_automation import MINIMAL_BEFORE


def snapshot(date='20261017'):
    dates = year_window(date)
    return {'target_date': date, 'start_date': dates[0], 'end_date': dates[-1], 'days': len(dates),
            'totals': {(str(4000 + boat), boat): [20, boat, 7 - boat, boat // 2]
                       for boat in range(1, 7)}}


def archive_text():
    # 1号艇 enters course 6; 6号艇 enters course 1. Race ST is not retained.
    rows = '\n'.join(f'  {rank}  {boat} 400{boat} 選手 12 13 6.80 {7-boat} 0.01 1.50.0'
                     for boat, rank in [(1, '01'), (2, '02'), (3, '03'), (4, '04'), (5, 'S1'), (6, 'F')])
    return f'STARTK\n10KBGN\n2026/10/16\n   1R  予選 H1800m 晴\n{rows}\n10KEND\nFINALK\n'


class CourseResultsTests(unittest.TestCase):
    def test_full_calendar_year_excludes_target_day_and_handles_leap_day(self):
        for date, first, last, length in [('20261017', '20251017', '20261016', 365),
                                         ('20240301', '20230301', '20240229', 366),
                                         ('20250301', '20240229', '20250228', 366)]:
            days = year_window(date)
            self.assertEqual((days[0], days[-1], len(days)), (first, last, length))

    def test_counts_use_actual_course_and_include_non_winning_starts(self):
        day = parse_course_results(archive_text(), '20261016')
        counts = {(racer, course): values for racer, course, *values in day['counts']}
        self.assertEqual(counts[(4001, 6)], [1, 1, 0, 0])
        self.assertEqual(counts[(4002, 5)], [1, 0, 1, 0])
        self.assertEqual(counts[(4003, 4)], [1, 0, 0, 1])
        self.assertEqual(counts[(4005, 2)], [1, 0, 0, 0])
        self.assertEqual(counts[(4006, 1)], [1, 0, 0, 0])
        self.assertNotIn('0.01', json.dumps(day))
        self.assertNotIn((4001, 1), counts)

    def test_bad_or_partial_archives_are_rejected(self):
        for text, date in [(archive_text(), '20261015'), (archive_text().replace('FINALK', ''), '20261016'),
                           (archive_text().replace('6.80 6', '6.80 5'), '20261016'),
                           (archive_text().replace('6.80 6', '6.80  '), '20261016')]:
            with self.subTest(date=date), self.assertRaises(ValueError):
                parse_course_results(text, date)

    def test_requires_complete_year_and_distinguishes_zero_from_no_history(self):
        dates = year_window('20261017')
        days = [{'format_version': 1, 'date': d, 'counts': [[4001, 1, 1, 0, 1, 0]]} for d in dates]
        data = aggregate_days(days, '20261017')
        self.assertEqual(racer_rates(data, '4001', 1), {'starts': 365, 'first': 0.0, 'second': 100.0, 'third': 0.0})
        self.assertIsNone(racer_rates(data, '4002', 1)['first'])
        for invalid in [days[:-1], days + days[:1], days[:-1] + [dict(days[-1], date='20261017')]]:
            with self.assertRaises(ValueError):
                aggregate_days(invalid, '20261017')


class PredictionFeatureTests(unittest.TestCase):
    def test_meeting_excludes_target_and_later_races_even_on_old_full_pages(self):
        links = ''.join(f'<td><a href="raceresult?hd={date}&jcd=04&rno={race}">{place}</a></td>'
                        for date, race, place in [('20261016', 12, '１'), ('20261017', 1, '２'),
                                                  ('20261017', 5, '６'), ('20261018', 1, '３')])
        html = '<table><tbody class="is-fs12"><tr><td>１</td><td><a href="profile?toban=4001">選手</a></td></tr><tr></tr><tr></tr><tr>'+links+'</tr></tbody></table>'
        result = meeting_results(html, '20261017', 5, '04')[1]
        self.assertEqual(result['racer_id'], '4001')
        self.assertEqual([row['place'] for row in result['finishes']], [1, 2])

    def test_exhibition_entry_order_is_not_boat_number_and_is_unknown_until_complete(self):
        html = ''.join(f'<div class="table1_boatImage1"><span class="table1_boatImage1Number">{b}</span><span class="table1_boatImage1Time">.10</span></div>'
                       for b in [1, 2, 6, 3, 4, 5])
        self.assertEqual(core.parse_before(html)['entry_course'], {1: 1, 2: 2, 6: 3, 3: 4, 4: 5, 5: 6})
        self.assertEqual(core.parse_before('<table></table>')['entry_course'], {})

    def test_only_four_features_score_and_course_rates_affect_their_own_finish_positions(self):
        raw = {b: [str(b), '', f'400{b}/A1 選手', '.15', '9.0 99', '9.0 99', '12 40 60'] for b in range(1, 7)}
        before = core.parse_before(MINIMAL_BEFORE)
        state = copy.deepcopy(model.DEFAULT)
        state['weights'] = {'motor': 0, 'exhibition': 0, 'meeting': 0, 'course': 1}
        boats = analyze_features(raw, 'none', before, snapshot(), state, {})
        self.assertGreater(boats[5]['score'], boats[0]['score'])
        self.assertLess(boats[5]['second_score'], boats[0]['second_score'])
        self.assertEqual(set(core.feature_snapshot(boats)['1']), {'motor', 'exhibition', 'meeting', 'course'})
        changed = copy.deepcopy(raw)
        for row in changed.values():
            row[3:6] = ['.01', '1.0 0', '1.0 0']
        self.assertEqual(boats, analyze_features(changed, 'none', before, snapshot(), state, {}))

    def test_second_and_third_scores_change_trifecta_probability(self):
        boats = [{'boat': b, 'score': 50.0, 'second_score': 100.0 if b == 2 else 50.0,
                  'third_score': 100.0 if b == 3 else 50.0} for b in range(1, 7)]
        odds = {''.join(map(str, order)): 100 for order in core.permutations(range(1, 7), 3)}
        bets = core.build_bets(boats, odds, '1', model=model.DEFAULT)
        self.assertTrue(bets)
        self.assertGreater(next(b['probability'] for b in bets if b['bet'] == '1-2-3'), 1 / 120)

    def test_requested_baseline_is_used_for_each_finish_position(self):
        raw = {b: [str(b), '', f'400{b}/A1 選手', '.15', '0 0', '0 0', f'12 {b * 10} 60']
               for b in range(1, 7)}
        before = {'exhibition': {b: 7 - b / 10 for b in range(1, 7)},
                  'entry_course': {b: b for b in range(1, 7)}}
        meeting = {b: {'finishes': [{'place': b}]} for b in range(1, 7)}
        boats = analyze_features(raw, 'none', before, snapshot(), model.DEFAULT, meeting)
        # Boat 1 only leads meeting form and the second-place course rate.
        # Boat 6 leads motor, exhibition and first/third-place course rates.
        self.assertEqual([boats[0][key] for key in ('score', 'second_score', 'third_score')],
                         [5.0, 55.0, 5.0])
        self.assertEqual([boats[5][key] for key in ('score', 'second_score', 'third_score')],
                         [95.0, 45.0, 95.0])


class VersionAndStoreTests(unittest.TestCase):
    def test_switch_preserves_old_evidence_without_reusing_its_weights(self):
        old = copy.deepcopy(model.LEGACY_DEFAULT)
        old.update(samples=1730, hits=707)
        old['weights']['history'] = .63
        new = model.upgrade_state(old)
        self.assertEqual(new['legacy_model'], old)
        self.assertEqual((new['samples'], new['hits'], new['feature_samples']), (1730, 707, 0))
        self.assertEqual(new['weights'], {'motor': .15, 'exhibition': .30, 'meeting': .05, 'course': .50})
        old_features = {'nation': 80, 'motor': 40, 'history': 100}
        model.learn_from_features(new, old_features, 2, 1, old_features, persist=False)
        self.assertEqual(new['weights'], model.DEFAULT['weights'])
        self.assertEqual(new['feature_samples'], 0)
        self.assertEqual(new['temperature'], 12)
        self.assertEqual(new['samples'], 1731)

    def test_new_vectors_train_new_weights_and_count_once(self):
        state = copy.deepcopy(model.DEFAULT)
        before = state['weights'].copy()
        predicted = {k: 50 for k in state['weights']}
        actual = dict(predicted, course=100, motor=0)
        model.learn_from_features(state, predicted, 2, 1, actual, persist=False)
        self.assertGreater(state['weights']['course'], before['course'])
        self.assertLess(state['weights']['motor'], before['motor'])
        self.assertEqual((state['feature_samples'], state['samples']), (1, 1))
        self.assertAlmostEqual(sum(state['weights'].values()), 1, places=3)

    def test_existing_model_adopts_baseline_once_and_keeps_subsequent_learning(self):
        old = copy.deepcopy(model.DEFAULT)
        old.pop('weight_baseline_version')
        old.update(weights={'motor': .24, 'exhibition': .26, 'meeting': .23, 'course': .27},
                   samples=1800, hits=720, feature_samples=12, feature_hits=3,
                   temperature=12.4, updated_at='2026-10-17T13:00:00',
                   legacy_model={'samples': 1788, 'hits': 717})
        with TemporaryDirectory() as directory, mock.patch.object(model, '_db_enabled', return_value=False), \
             mock.patch.object(model, 'PATH', str(Path(directory) / 'model.json')):
            Path(model.PATH).write_text(json.dumps(old))
            state = model.load()
            self.assertEqual(state['weights'], {'motor': .15, 'exhibition': .30, 'meeting': .05, 'course': .50})
            self.assertEqual(state['weight_baseline_version'], model.WEIGHT_BASELINE_VERSION)
            for key in ('samples', 'hits', 'feature_samples', 'feature_hits', 'temperature', 'legacy_model'):
                self.assertEqual(state[key], old[key])
            self.assertEqual(state['weight_baseline_history'], [{
                'version': None, 'weights': old['weights'], 'updated_at': old['updated_at']}])
            predicted = {key: 50 for key in model.FEATURE_KEYS}
            model.learn_from_features(state, predicted, 2, 1,
                                      dict(predicted, course=100, motor=0))
            self.assertNotEqual(state['weights'], model.DEFAULT['weights'])
            reloaded = model.load()
            self.assertEqual(reloaded, state)
            self.assertEqual(len(reloaded['weight_baseline_history']), 1)
            self.assertEqual((reloaded['samples'], reloaded['feature_samples']), (1801, 13))

    def test_missing_year_returns_pending_without_official_requests_or_empty_prediction(self):
        with mock.patch.object(store, 'available_days', return_value=set()), \
             mock.patch.object(store, 'read_days') as read, \
             mock.patch.object(core, 'submit_official_page') as pages, \
             mock.patch.object(core, 'load_model', return_value=copy.deepcopy(model.DEFAULT)), \
             mock.patch.object(core, 'course_history', store.CourseHistory()):
            response = core.app.test_client().get('/api/analyze?date=20261017&stadium=04&race=5')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json['code'], 'course_history_pending')
        self.assertEqual(response.json['ready_days'], 0)
        pages.assert_not_called(); read.assert_not_called()

    def test_batch_reports_year_preparation_instead_of_storage_failure(self):
        with mock.patch.object(core.course_history, 'get', side_effect=store.CourseHistoryPending(100, 365)), \
             mock.patch.object(core, 'load_model', return_value=copy.deepcopy(model.DEFAULT)), \
             mock.patch.object(core, 'predict_race') as predict:
            response = core.app.test_client().post('/api/analyze_batch', json={
                'date': '20261017', 'stadium': '04', 'races': [1, 2, 3]})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json['code'], 'course_history_pending')
        self.assertEqual(response.json['ready_days'], 100)
        predict.assert_not_called()

    def test_complete_year_is_aggregated_once_and_pending_does_not_cache_partial_data(self):
        dates = year_window('20261017')
        days = [{'format_version': 1, 'date': d, 'counts': []} for d in dates]
        cache = store.CourseHistory()
        with mock.patch.object(store, 'available_days', side_effect=[set(dates[:-1]), set(dates)]), \
             mock.patch.object(store, 'read_days', return_value=days) as read:
            with self.assertRaises(store.CourseHistoryPending):
                cache.get('20261017')
            first = cache.get('20261017')
            self.assertIs(first, cache.get('20261017'))
            self.assertEqual(read.call_count, 1)

    def test_ready_snapshot_does_not_read_a_full_year_on_worker_polls(self):
        cache = store.CourseHistory()
        cache.snapshots['20261017'] = snapshot()
        with mock.patch.object(store, 'read_days') as read, \
             mock.patch.object(store, 'available_days') as available:
            self.assertFalse(cache.prepare(core, '20261017'))
        read.assert_not_called(); available.assert_not_called()

    def test_v2_historical_forecast_uses_target_date_year_instead_of_current_profile(self):
        entry = '<title>出走表</title><table>' + ''.join(
            f'<tr><td>{b}</td><td></td><td>400{b}/A1 選手</td><td>.15</td>'
            f'<td>6.0</td><td>6.0</td><td>42 40 60</td></tr>' for b in range(1, 7)) + '</table>'
        html = lambda url: entry if 'racelist?' in url else '<title>直前情報</title>'+MINIMAL_BEFORE
        runner = historical.HistoricalRunner(core, fetch_html=html)
        odds = {''.join(map(str, order)): 100 for order in core.permutations(range(1, 7), 3)}
        def page(url):
            return '<h2>締切時オッズ</h2>' if 'odds3t?' in url else html(url)
        runner.fetch_html = page
        with mock.patch.object(core.course_history, 'get', return_value=snapshot('20251017')) as annual, \
             mock.patch.object(core, 'parse_odds', return_value=odds):
            first = runner.forecast('20251017', {'stadium': '04', 'race': 5, 'combo': '123'}, {}, copy.deepcopy(model.DEFAULT))
            second = runner.forecast('20251017', {'stadium': '04', 'race': 5, 'combo': '654'}, {}, copy.deepcopy(model.DEFAULT))
        self.assertEqual(first, second)
        self.assertEqual(annual.call_args_list, [mock.call('20251017'), mock.call('20251017')])
        self.assertEqual(set(first['features']['1']), set(model.DEFAULT['weights']))

    def test_pending_year_keeps_historical_checkpoint_and_retry_budget(self):
        state = historical.new_job('20251017', '20261016')
        state.update(results=[{'stadium': '04', 'race': 5}], failures=4)
        runner = historical.HistoricalRunner(core)
        with mock.patch.object(historical, 'read_job', return_value=state), \
             mock.patch.object(runner, 'forecast', side_effect=store.CourseHistoryPending(30, 365)), \
             mock.patch.object(historical, 'checkpoint') as checkpoint:
            self.assertFalse(runner.run_once())
        after = checkpoint.call_args.args[1]
        self.assertTrue(after['enabled'])
        self.assertEqual(after['phase'], 'course_preparing')
        self.assertEqual((after['position'], after['failures'], after['model']), (0, 4, state['model']))

    def test_old_historical_job_cannot_replace_current_features(self):
        state = historical.new_job('20251017', '20261016')
        state.update(phase='completed', enabled=False)
        state['model'] = copy.deepcopy(model.LEGACY_DEFAULT)
        state['model']['samples'] = 1000
        with mock.patch.object(historical, 'read_job', return_value=state), \
             mock.patch.object(core, 'load_model', return_value=copy.deepcopy(model.DEFAULT)), \
             mock.patch.object(core, 'save_model') as save:
            with self.assertRaisesRegex(ValueError, '旧特徴'):
                historical.control(core, 'apply_model')
        save.assert_not_called()


if __name__ == '__main__':
    unittest.main()

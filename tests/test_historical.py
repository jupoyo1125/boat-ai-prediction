import copy
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as core
import automation
import historical
from historical_archive import parse_daily_results, previous_history
import model
from test_automation import MINIMAL_BEFORE


def official(race=1, combo='123', excluded=None):
    return {'stadium': '10', 'race': race, 'combo': combo, 'payout': 990, 'excluded': excluded}


def prediction(main=1, bets=True):
    return {'main': main, 'features': {str(i): {key: float(i * 10) for key in model.DEFAULT['weights']}
                                       for i in range(1, 7)},
            'bets': [{'bet': '1-2-3', 'category': category} for category in core.BET_CATEGORIES]
                    if bets else None}


def result_text(date='20161009', flying=False, combo='123'):
    day = datetime.strptime(date, '%Y%m%d')
    rows = '\n'.join(f'  {"F " if flying and i == 6 else f"{i:02d}"}  {i} 400{i} 選手{i} 12 13 6.80 1 0.01 1.50.0'
                     for i in range(1, 7))
    return f'STARTK\n10KBGN\n{day.year}/{day.month}/{day.day}\n   1R  予選 H1800m 晴\n{rows}\n        ３連単   {"-".join(combo)} 990 人気 1\n10KEND\nFINALK\n'


class HistoricalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.dict(os.environ, {'DATABASE_URL': '', 'POSTGRES_URL': '', 'BOAT_STORAGE_PHASE': 'render',
                                            'RENDER_SERVICE_ID': '', 'BOAT_HISTORY_RUNNER': '0', 'BOAT_AUTO_RUNNER': '0'})
        patch.start(); self.addCleanup(patch.stop)
        for target, name, value in [(historical, 'PATH', Path(self.tmp.name) / 'historical.json'),
                                    (core, 'LEDGER', Path(self.tmp.name) / 'ledger.json'),
                                    (model, 'PATH', str(Path(self.tmp.name) / 'model.json'))]:
            patch = mock.patch.object(target, name, value)
            patch.start(); self.addCleanup(patch.stop)
        self.runner = historical.HistoricalRunner(core, fetch_results=lambda date: [official()],
                                                  fetch_html=mock.Mock())

    def job(self, days=2, races=4):
        state = historical.new_job('20161009', f'201610{8 + days:02d}')
        historical.write_job(state)
        self.runner.fetch_results = lambda date: [official(race) for race in range(1, races + 1)]
        self.assertTrue(self.runner.run_once())
        return historical.read_job()

    def test_official_download_reads_actual_trifecta_and_never_retains_race_st(self):
        result = parse_daily_results(result_text(), '20161009')[0]
        self.assertEqual(result, official())
        self.assertFalse(any(key in result for key in ('st', 'exhibition_st', 'features')))

    def test_refunds_and_mismatched_outcome_are_excluded(self):
        self.assertEqual(parse_daily_results(result_text(flying=True), '20161009')[0]['excluded'], 'refund')
        self.assertEqual(parse_daily_results(result_text(combo='213'), '20161009')[0]['excluded'], 'invalid_result')
        with self.assertRaises(ValueError):
            parse_daily_results(result_text(), '20261009')
        with self.assertRaises(ValueError):
            parse_daily_results(result_text().replace('FINALK', ''), '20161009')

    def test_history_excludes_same_day_future_and_older_results(self):
        history = [{'date': date, 'results': [official(combo=combo)]} for date, combo in
                   [('20161005', '123'), ('20161008', '213'), ('20161009', '123'), ('20161010', '123')]]
        stats = previous_history(history, '20161009', '10')
        self.assertEqual(stats['races'], 1)
        self.assertEqual(stats['first_win_rate']['2'], 100)

    def test_restarting_resumes_once_and_freezes_the_model_for_each_date(self):
        state = self.job()
        observed = []
        def forecast(date, result, hist, day_model):
            observed.append((date, result['race'], day_model['samples']))
            return prediction()
        self.runner.forecast = forecast
        self.runner.run_once()
        self.assertEqual(historical.read_job()['model']['samples'], 3)
        restarted = historical.HistoricalRunner(core, fetch_results=self.runner.fetch_results)
        restarted.forecast = forecast
        restarted.run_once()
        self.assertEqual(historical.read_job()['model']['samples'], 4)
        restarted.run_once()  # prepare second day
        restarted.run_once()
        restarted.run_once()
        finished = historical.read_job()
        self.assertEqual(finished['model']['samples'], 8)
        self.assertEqual(finished['days_done'], 2)
        self.assertFalse(finished['enabled'])
        self.assertEqual([row[2] for row in observed], [0] * 4 + [4] * 4)
        self.assertFalse(restarted.run_once())
        self.assertEqual(model.load()['samples'], 0)
        self.assertEqual(core.load_ledger(), [])

    def test_missing_odds_can_train_but_never_contributes_to_category_roi(self):
        self.job(days=1, races=1)
        self.runner.forecast = lambda *args: prediction(bets=False)
        self.runner.run_once()
        state = historical.read_job()
        self.assertEqual(state['model']['samples'], 1)
        self.assertEqual(state['missing_odds'], 1)
        self.assertEqual(state['evaluated'], 0)
        self.assertIsNone(historical.public_status()['category_stats']['gachi']['roi'])

    def test_missing_features_and_refunds_are_counted_without_learning(self):
        self.job(days=1, races=2)
        state = historical.read_job()
        state['results'][1]['excluded'] = 'refund'
        historical.write_job(state)
        self.runner.forecast = mock.Mock(return_value=None)
        self.runner.run_once()
        state = historical.read_job()
        self.assertEqual(state['missing_features'], 1)
        self.assertEqual(state['excluded_results'], 1)
        self.assertEqual(state['model']['samples'], 0)
        self.assertEqual(self.runner.forecast.call_count, 1)

    def test_network_failure_rolls_back_entire_batch_and_retries_without_double_learning(self):
        self.job(days=1)
        self.runner.forecast = mock.Mock(side_effect=[prediction(), RuntimeError('network'), prediction()])
        with mock.patch.object(core.app.logger, 'exception'):
            self.runner.run_once()
        state = historical.read_job()
        self.assertEqual(state['position'], 0)
        self.assertEqual(state['visited'], 0)
        self.assertEqual(state['model']['samples'], 0)
        self.assertEqual(state['phase'], 'retry')
        state['retry_at'] = None
        historical.write_job(state)
        self.runner.forecast = lambda *args: prediction()
        self.runner.run_once()
        recovered = historical.read_job()
        self.assertEqual(recovered['phase'], 'running')
        self.assertIsNone(recovered['retry_at'])
        self.assertEqual(recovered['model']['samples'], 3)
        self.runner.run_once()
        self.assertEqual(historical.read_job()['model']['samples'], 4)

    def test_resuming_mid_day_shows_running_after_the_next_saved_batch(self):
        self.job(days=1, races=4)
        historical.control(core, 'pause')
        historical.control(core, 'resume')
        self.assertEqual(historical.public_status()['phase'], 'queued')
        self.runner.forecast = lambda *args: prediction()
        self.runner.run_once()
        self.assertEqual(historical.public_status()['phase'], 'running')
        self.assertEqual(historical.public_status()['samples'], 3)
        self.runner.run_once()
        self.assertEqual(historical.public_status()['phase'], 'completed')

    def test_history_lease_allows_live_status_setup_and_blocks_another_history_worker(self):
        held = set()
        def lease_query(query, parameters):
            key = parameters[0]
            if 'pg_try_advisory_lock' in query:
                acquired = key not in held
                if acquired:
                    held.add(key)
                return mock.Mock(fetchone=lambda: (acquired,))
            if 'pg_advisory_unlock' in query:
                held.remove(key)
            return mock.Mock()
        def status_query(query, parameters=None):
            if 'pg_advisory_xact_lock' in query and parameters[0] in held:
                raise TimeoutError('Live status setup is blocked by the historical worker')
            return mock.Mock()
        history_connection = mock.Mock(execute=mock.Mock(side_effect=lease_query))
        status_connection = mock.Mock(execute=mock.Mock(side_effect=status_query))
        with mock.patch.object(historical, 'database_url', return_value='test'), \
             mock.patch('psycopg.connect') as connect, \
             mock.patch.object(automation, 'state_connection') as connection, \
             mock.patch.object(automation, '_table_ready', False):
            connect.return_value.__enter__.return_value = history_connection
            connection.return_value.__enter__.return_value = status_connection
            with historical.lease() as acquired:
                self.assertTrue(acquired)
                automation.ensure_status_table()
                self.assertTrue(automation._table_ready)
                with historical.lease() as duplicate:
                    self.assertFalse(duplicate)
            self.assertFalse(held)

    def test_pause_during_external_requests_discards_batch_and_resume_retries_safely(self):
        self.job(days=1, races=1)
        def forecast(*args):
            historical.control(core, 'pause')
            return prediction()
        self.runner.forecast = forecast
        self.assertFalse(self.runner.run_once())
        self.assertEqual(historical.read_job()['model']['samples'], 0)
        self.assertFalse(historical.read_job()['enabled'])
        historical.control(core, 'resume')
        self.runner.forecast = lambda *args: prediction()
        self.runner.run_once()
        self.assertEqual(historical.read_job()['model']['samples'], 1)

    def test_stale_worker_cannot_commit_after_another_worker(self):
        state = self.job(days=1, races=1)
        self.runner.forecast = lambda *args: prediction()
        self.runner.process_batch(state)
        self.assertFalse(self.runner.process_batch(state))
        self.assertEqual(historical.read_job()['model']['samples'], 1)

    def test_three_categories_use_saved_bet_count_and_official_payout(self):
        self.job(days=1, races=1)
        self.runner.forecast = lambda *args: prediction()
        self.runner.run_once()
        status = historical.public_status()
        for stats in status['category_stats'].values():
            self.assertEqual(stats['investment'], 100)
            self.assertEqual(stats['payout'], 990)
            self.assertEqual(stats['roi'], 990)
            self.assertEqual(stats['hit_rate'], 100)
        live = core.app.test_client().get('/api/performance').json
        self.assertEqual(live['automatic_category_stats']['gachi']['settled'], 0)
        self.assertEqual(live['historical']['category_stats']['gachi']['settled'], 1)

    def test_model_application_requires_completion_preserves_live_counters_and_is_idempotent(self):
        self.job(days=1, races=1)
        with self.assertRaises(ValueError):
            historical.control(core, 'apply_model')
        state = historical.read_job()
        state.update(phase='completed', enabled=False)
        state['model']['samples'] = 1000
        state['model']['weights']['nation'] = .30
        historical.write_job(state)
        live = copy.deepcopy(model.DEFAULT)
        live.update(samples=855, hits=262)
        model.save(live)
        before = model.load()
        historical.control(core, 'apply_model')
        after = model.load()
        self.assertEqual(after['samples'], 855)
        self.assertEqual(after['hits'], 262)
        self.assertEqual(after['weights']['nation'], .30)
        self.assertEqual(after['historical_training']['samples'], 1000)
        self.assertEqual(historical.read_job()['model_backup'], before)
        with mock.patch.object(core, 'save_model') as save:
            historical.control(core, 'apply_model')
            save.assert_not_called()

    def test_control_routes_reject_invalid_actions_and_ten_year_window_ends_yesterday(self):
        client = core.app.test_client()
        self.assertEqual(client.post('/api/historical', json={'action': 'erase'}).status_code, 400)
        self.assertEqual(client.post('/api/historical', data='x').status_code, 400)
        result = client.post('/api/historical', json={'action': 'start'}).json
        self.assertTrue(result['exists'])
        clock = lambda: datetime(2026, 10, 9, tzinfo=historical.JST)
        self.assertEqual(historical.date_window(clock), ('20161009', '20261008'))

    def test_real_forecast_path_uses_pre_race_features_and_ignores_current_result(self):
        entry = '<title>出走表</title><table>' + ''.join(
            f'<tr><td>{i}</td><td></td><td>400{i}/A1 選手</td><td>F0 L0 0.14</td>'
            f'<td>6.{i} 50 60</td><td>5.{i} 40 50</td><td>12 4{i}.00 50</td></tr>'
            for i in range(1, 7)) + '</table>'
        odds = '<h2>締切時オッズ</h2><table>' + ('<tr>' + '<td class="oddsPoint">50.0</td>' * 6 + '</tr>') * 20 + '</table>'
        def fetch(url):
            return entry if 'racelist?' in url else '<title>直前情報</title>' + MINIMAL_BEFORE if 'beforeinfo?' in url else odds
        runner = historical.HistoricalRunner(core, fetch_html=fetch)
        hist = previous_history([], '20161009', '10')
        fixed = copy.deepcopy(model.DEFAULT)
        with mock.patch.object(core, 'load_model', side_effect=AssertionError('live model must not be used')):
            first = runner.forecast('20161009', official(combo='123'), hist, fixed)
            second = runner.forecast('20161009', official(combo='654'), hist, fixed)
        self.assertEqual(first, second)
        self.assertEqual(len(first['features']), 6)
        self.assertTrue(first['bets'])
        self.assertEqual(fixed, model.DEFAULT)

    def test_projection_and_context_cache_avoid_reloading_results_on_each_checkpoint(self):
        state = self.job(days=1, races=1)
        context_reads = []
        class Connection:
            def execute(self, query, parameters=None):
                if "state - 'history'" in query:
                    header = {key: copy.deepcopy(value) for key, value in state.items() if key not in ('history', 'results')}
                    value = (header, state['results'] is not None)
                else:
                    context_reads.append(query)
                    value = (copy.deepcopy(state['history']), copy.deepcopy(state['results']))
                return mock.Mock(fetchone=lambda: value)
        with mock.patch.object(historical, 'database_url', return_value='test'), \
             mock.patch.object(historical, 'ensure_schema'), \
             mock.patch.object(historical, '_context_cache', None), \
             mock.patch.object(historical, 'state_connection') as connection:
            connection.return_value.__enter__.return_value = Connection()
            self.assertEqual(historical.read_job()['results'], state['results'])
            self.assertEqual(historical.read_job()['results'], state['results'])
            self.assertEqual(len(context_reads), 1)
            self.assertNotIn('results', historical.read_job(include_context=False))
            state['cursor'] = '20161010'
            historical.read_job()
            self.assertEqual(len(context_reads), 2)


if __name__ == '__main__':
    unittest.main()

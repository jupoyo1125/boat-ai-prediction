import copy
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest import mock

import requests
import app as core
import model
import state_store
from test_automation import BEFORE
from batch_processing import settle_batch


ROOT = Path(__file__).resolve().parents[1]
RESULTS = [{'race': race, 'combo': '123' if race % 2 else '615', 'payout': 990 + race}
           for race in range(1, 13)]
LIST = '<table>' + ''.join(
    f'<tr><td>{r["race"]}R</td><td>{"-".join(r["combo"])}</td><td>¥{r["payout"]}</td></tr>'
    for r in RESULTS) + '</table>'
MINIMAL_ENTRIES = '<table>' + ''.join(
    f'<tr><td>{boat}</td><td></td><td>5001 / A1 選手</td><td>.15</td>'
    f'<td>{5+boat/10}</td><td>6.50</td><td>12 {30+boat}</td></tr>'
    for boat in range(1, 7)) + '</table>'


def forecast():
    return {'main': 1, 'features': {str(i): {'nation': i*10, 'motor': 70-i}
                                  for i in range(1, 7)},
            'bets': [{'bet': '123', 'category': 'gachi'}, {'bet': '615', 'category': 'roman'}]}


class BatchTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        for patch in [
            mock.patch.dict(os.environ, {'BOAT_STORAGE_PHASE': 'render', 'DATABASE_URL': '',
                                        'POSTGRES_URL': '', 'BOAT_AUTO_RUNNER': '0'}),
            mock.patch.object(core, 'LEDGER', Path(temporary.name)/'ledger.json'),
            mock.patch.object(model, 'PATH', str(Path(temporary.name)/'model.json')),
            mock.patch.object(core.app.logger, 'disabled', True),
        ]:
            patch.start(); self.addCleanup(patch.stop)
        core.cached_official_results.cache_clear()
        core.historical_stats.cache_clear()
        self.client = core.app.test_client()

    def records(self, races=range(1, 13)):
        rows = [core.performance_record({'date': '20261007', 'stadium': '10', 'race': race,
                                        'combo': '123', 'predicted_first': 1, **forecast()})
                for race in races]
        core.save_ledger(rows)
        return rows

    def post(self, endpoint='settle_batch', races=None):
        return self.client.post('/api/'+endpoint, json={
            'date': '20261007', 'stadium': '10', 'races': races or [1,2,3,4,5,6]})

    def test_batch_matches_sequential_learning_and_accounting_with_two_saves_for_twelve(self):
        original = self.records()
        original[-1]['features'] = {}  # Also exercise legacy learning without features.
        core.save_ledger(original)
        state = copy.deepcopy(model.DEFAULT)
        state['samples'], state['hits'], state['temperature'] = 100, 30, 17.99
        model.save(state)
        with mock.patch.object(core, 'get', return_value=LIST):
            for race in range(1, 13):
                payload, status = core.settle_saved_prediction('20261007', '10', race)
                self.assertEqual(status, 200, payload)
        sequential_rows, sequential_model = core.load_ledger(), model.load()
        core.save_ledger(original); model.save(copy.deepcopy(state))
        core.cached_official_results.cache_clear()
        with mock.patch.object(core, 'get', return_value=LIST) as get, \
                mock.patch.object(core, 'save_ledger', wraps=core.save_ledger) as save, \
                mock.patch.object(core, 'load_model', wraps=core.load_model) as load_model, \
                mock.patch.object(core, 'save_model', wraps=core.save_model) as save_model:
            for first in (1, 7):
                response = self.post(races=list(range(first, first+6)))
                self.assertEqual(response.status_code, 200, response.json)
                self.assertTrue(all(r['status'] == 'settled' for r in response.json['results']))
            self.assertEqual(get.call_count, 1)
            self.assertEqual(save.call_count, 2)
            self.assertEqual(load_model.call_count, 2)
            self.assertEqual(save_model.call_count, 2)
        batch_rows, batch_model = core.load_ledger(), model.load()
        for rows in (batch_rows, sequential_rows):
            for row in rows: row.pop('result_confirmed_at', None)
        for result in (batch_model, sequential_model): result.pop('updated_at', None)
        self.assertEqual(batch_rows, sequential_rows)
        self.assertEqual(batch_model, sequential_model)

    def test_repeated_and_concurrent_batches_learn_each_record_once(self):
        self.records(range(1, 7))
        with mock.patch.object(core, 'get', return_value=LIST):
            with ThreadPoolExecutor(max_workers=3) as workers:
                runs = list(workers.map(lambda _: settle_batch(core, '20261007', '10', list(range(1,7))), range(3)))
            self.assertEqual(sum(r['status'] == 'settled' for run in runs for r in run), 6)
        with mock.patch.object(core, 'get') as get, mock.patch.object(core, 'save_model') as save:
            response = self.post()
            self.assertTrue(all(r['status'] == 'already_settled' for r in response.json['results']))
            get.assert_not_called(); save.assert_not_called()
        self.assertEqual(model.load()['samples'], 6)

    def test_result_wait_missing_and_network_error_preserve_pending_records(self):
        original = self.records([1,2,3])
        with mock.patch.object(core, 'cached_official_results', return_value=[RESULTS[0]]), \
                mock.patch.object(core, 'fetch_official_result', side_effect=lambda d,j,r,**k:
                    (RESULTS[0], 'official', None) if r == 1 else
                    (None, 'official', 'result_pending') if r == 2 else
                    (_ for _ in ()).throw(requests.ReadTimeout())):
            results = self.post().json['results']
        self.assertEqual([r.get('status') or r['code'] for r in results],
                         ['settled','result_pending','official_unavailable',
                          'prediction_missing','prediction_missing','prediction_missing'])
        self.assertEqual(core.load_ledger()[1:], original[1:])
        self.assertEqual(model.load()['samples'], 1)

    def test_failed_venue_list_is_attempted_once_before_individual_fallbacks(self):
        self.records(range(1, 7))
        def page(url, **kwargs):
            if 'resultlist' in url: raise requests.ReadTimeout()
            return '<p>3連単 1-2-3 ¥990</p>'
        with mock.patch.object(core, 'get', side_effect=page) as get:
            response = self.post()
        self.assertTrue(all(r['ok'] for r in response.json['results']))
        self.assertEqual(sum('resultlist' in c.args[0] for c in get.call_args_list), 2)
        self.assertEqual(sum('raceresult' in c.args[0] for c in get.call_args_list), 6)

    def test_transaction_failure_rolls_back_ledger_and_model_together(self):
        original = self.records(range(1, 7)); state = copy.deepcopy(model.DEFAULT)
        model.save(state)
        @contextmanager
        def simulated_transaction():
            ledger_before = core.LEDGER.read_bytes(); model_before = Path(model.PATH).read_bytes()
            try:
                with state_store.atomic_state(): yield
            except Exception:
                core.LEDGER.write_bytes(ledger_before); Path(model.PATH).write_bytes(model_before)
                raise
        with mock.patch.object(core, 'get', return_value=LIST), \
                mock.patch.object(core, 'atomic_state', side_effect=simulated_transaction), \
                mock.patch.object(core, 'save_ledger', side_effect=RuntimeError('commit failed')):
            response = self.post()
        self.assertEqual(response.status_code, 502)
        self.assertFalse(response.json['ok'])
        self.assertEqual(core.load_ledger(), original)
        self.assertEqual(model.load()['samples'], 0)

    def test_analysis_saves_successes_once_and_preserves_frozen_automatic_prediction(self):
        rows = self.records([1]); rows[0]['prediction_origin'] = 'automatic'
        core.save_ledger(rows)
        def predict(date,jcd,race,**kwargs):
            if race == 3: raise requests.ReadTimeout()
            return forecast()
        with mock.patch.object(core, 'predict_race', side_effect=predict), \
                mock.patch.object(core, 'historical_stats', return_value={}), \
                mock.patch.object(core, 'load_model', wraps=core.load_model) as load, \
                mock.patch.object(core, 'save_ledger', wraps=core.save_ledger) as save:
            response = self.post('analyze_batch', [1,2,3])
        self.assertEqual(response.status_code, 200)
        self.assertEqual([r.get('code') or r['status'] for r in response.json['results']],
                         ['prediction_frozen','saved','analysis_failed'])
        self.assertEqual(load.call_count, 1); self.assertEqual(save.call_count, 1)
        self.assertEqual(core.load_ledger()[0], rows[0])
        self.assertEqual(len(core.load_ledger()), 2)

    def test_invalid_or_oversized_requests_and_maintenance_do_no_work(self):
        with mock.patch.object(core, 'get') as get, mock.patch.object(core, 'load_ledger') as ledger:
            for body in [None, [], {'date':'bad','stadium':'10','races':[1]},
                         {'stadium':'10','races':[1,1]}, {'stadium':'25','races':[1]},
                         {'stadium':'10','races':[True]}, {'stadium':'10','races':list(range(1,8))}]:
                response = self.client.post('/api/settle_batch', json=body)
                self.assertEqual(response.status_code, 400, response.json)
            self.assertEqual(self.post('analyze_batch', [1,2,3,4]).status_code, 400)
            with mock.patch.dict(os.environ, {'BOAT_STORAGE_PHASE': 'prepare'}):
                self.assertEqual(self.post().status_code, 503)
                self.assertEqual(self.post('analyze_batch', [1]).status_code, 503)
            get.assert_not_called(); ledger.assert_not_called()

    def test_official_http_limit_applies_across_manual_and_automatic_threads(self):
        active = peak = 0
        lock = threading.Lock()
        class Response:
            apparent_encoding = 'utf-8'; text = 'data'
            def __enter__(self): return self
            def __exit__(self,*args): return False
            def raise_for_status(self): pass
        def get(*args, **kwargs):
            nonlocal active, peak
            with lock: active += 1; peak = max(peak, active)
            time.sleep(.02)
            with lock: active -= 1
            return Response()
        with mock.patch.object(core, 'official_session', return_value=mock.Mock(get=get)):
            with ThreadPoolExecutor(max_workers=12) as workers:
                values = list(workers.map(core.get, ['official']*12))
        self.assertEqual(values, ['data']*12)
        self.assertGreater(peak, 1); self.assertLessEqual(peak, 3)

    def test_concurrent_inputs_produce_same_prediction_and_read_model_once(self):
        before = BEFORE
        path = ROOT/'fixtures/mikuni-racelist.html'
        entries = path.read_text() if path.exists() else MINIMAL_ENTRIES
        hist = {'races': 0, 'first_win_rate': {str(i): 0 for i in range(1,7)}}
        def get(url, **kwargs):
            return before if 'beforeinfo' in url else entries if 'racelist' in url else '<p></p>'
        state = copy.deepcopy(model.DEFAULT)
        boats = core.analyze(core.boats_from(entries), 'none', core.parse_before(before), hist, model=state)
        odds = {f'{a}{b}{c}': 10.0 for a,b,c in core.permutations(range(1,7),3)}
        expected_bets = core.build_bets(boats, odds, 'none',
                                        core.scenario(boats, core.parse_before(before)), model=state)
        with mock.patch.object(core, 'get', side_effect=get), \
                mock.patch.object(core, 'historical_stats', return_value=hist) as history, \
                mock.patch.object(core, 'load_model', return_value=state) as load, \
                mock.patch.object(core, 'parse_odds', return_value=odds):
            result = core.predict_race('20261007', '10', 1)
        self.assertEqual(result['bets'], expected_bets)
        self.assertEqual(result['boats'], sorted(boats,key=lambda row:row['score'],reverse=True))
        load.assert_called_once(); history.assert_called_once_with('10',3,'20261006')

    def test_bulk_ledger_save_writes_changed_rows_once_and_skips_unchanged_rows(self):
        previous = [{'id':'keep','note':'保存済み'}, {'id':'update','value':1}, {'id':'remove'}]
        rows = [previous[0], {'id':'update','value':2}, {'id':'new','features':{'1':{'st':0.0}}}]
        connection = mock.MagicMock()
        @contextmanager
        def connect():
            yield connection
        with mock.patch.object(core, '_db_enabled', return_value=True), \
                mock.patch.object(core, '_ensure_ledger_table'), \
                mock.patch.object(core, '_db_connect', side_effect=connect), \
                mock.patch.object(core, 'ledger_snapshot', return_value={
                    row['id']:json.dumps(row,sort_keys=True,ensure_ascii=False) for row in previous}):
            core.save_ledger(rows)
        calls = connection.execute.call_args_list
        inserts = [call for call in calls if 'INSERT INTO' in call.args[0]]
        self.assertEqual(len(inserts), 1)
        self.assertEqual(json.loads(inserts[0].args[1][0]),
                         [{'id':row['id'],'record':row} for row in rows[1:]])
        self.assertEqual(calls[0].args[1], (['remove'],))


if __name__ == '__main__':
    unittest.main()

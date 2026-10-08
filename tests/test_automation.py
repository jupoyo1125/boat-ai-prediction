import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as core
import automation
import model
import state_store

MINIMAL_BEFORE = '<table><thead><tr><th>枠</th><th>写真</th><th>選手</th><th>体重</th><th>展示タイム</th></tr></thead>' + ''.join(
    f'<tr><td>{boat}</td><td></td><td>選手</td><td>52.0kg</td><td>{ex}</td><td>前走 ST .29</td></tr>'
    for boat, ex in enumerate([6.77,6.77,6.65,6.75,6.69,6.89], 1)
) + '</table>' + ''.join(
    f'<div class="table1_boatImage1"><span class="table1_boatImage1Number">{boat}</span><span class="table1_boatImage1Time">{st}</span></div>'
    for boat, st in enumerate(['.00','.09','.11','.11','F.01','.02'], 1)
)
actual = Path(__file__).resolve().parents[1] / 'fixtures/mikuni-before.html'
BEFORE = actual.read_text() if actual.exists() else MINIMAL_BEFORE


def prediction():
    return {'ok': True, 'venue': '三国', 'main': 1, 'second': 2, 'hole': 3,
            'boats': [{'boat': i, 'score': 50, 'name': '選手'} for i in range(1,7)],
            'features': {str(i): {'nation': 50, 'local': 50} for i in range(1,7)},
            'scenario': '逃げ', 'odds_count': 120,
            'bets': [{'bet': '1-2-3', 'category': name, 'odds': 10, 'probability': .1, 'ev': 1}
                     for name in ['gachi','roman','oni']]}


class AutomaticFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {'DATABASE_URL':'','POSTGRES_URL':'','RENDER_SERVICE_ID':'','BOAT_AUTO_RUNNER':'0'})
        env.start(); self.addCleanup(env.stop)
        for target, name, value in [(core,'LEDGER',Path(self.tmp.name)/'ledger.json'),(model,'PATH',str(Path(self.tmp.name)/'model.json'))]:
            patch = mock.patch.object(target,name,value); patch.start(); self.addCleanup(patch.stop)
        self.now = datetime(2026,10,8,10,0,tzinfo=automation.JST)
        self.statuses = []
        self.runner = automation.AutoRunner(core, clock=lambda:self.now, persist=lambda status:self.statuses.append(copy.deepcopy(status)))
        self.official = {}
        self.deadline = self.now + timedelta(minutes=5)
        for target, name, value in [
            (self.runner,'deadlines',lambda date,jcd:{1:self.deadline}),
            (self.runner,'official_results',lambda date,jcd:self.official),
            (core,'get_active_stadiums',mock.Mock(return_value=[{'stadium':'10','venue':'三国'}])),
            (core,'get',mock.Mock(return_value=BEFORE)),
            (core,'predict_race',mock.Mock(side_effect=lambda *args,**kwargs:prediction())),
        ]:
            patch=mock.patch.object(target,name,value); patch.start(); self.addCleanup(patch.stop)

    def test_actual_exhibition_and_st_are_complete_including_zero_and_flying(self):
        before=core.parse_before(BEFORE)
        self.assertTrue(automation.exhibition_ready(before))
        self.assertEqual(before['exhibition_st'][1],0)
        self.assertEqual(before['exhibition_st'][5],-.01)
        self.assertEqual(before['exhibition'][3],6.65)

    def test_previous_race_st_cannot_trigger_automatic_prediction(self):
        html=MINIMAL_BEFORE.split('<div class="table1_boatImage1">')[0]
        self.assertFalse(automation.exhibition_ready(core.parse_before(html)))

    def test_blank_official_exhibition_is_not_ready(self):
        pending=Path(__file__).resolve().parents[1]/'fixtures/omura-before.html'
        html=pending.read_text() if pending.exists() else '<table><tr><td>1</td><td></td><td>選手</td><td>52kg</td><td></td><td>ST .19</td></tr></table>'
        self.assertFalse(automation.exhibition_ready(core.parse_before(html)))

    def test_complete_flow_freezes_before_deadline_then_updates_learning_and_three_categories(self):
        self.runner.run_once()
        rows=core.load_ledger(); self.assertEqual(len(rows),1)
        row=rows[0]
        self.assertEqual(row['prediction_origin'],'automatic')
        self.assertLess(datetime.fromisoformat(row['prediction_saved_at']),self.deadline)
        self.assertFalse(row['settled'])
        self.runner.run_once(); self.assertEqual(core.predict_race.call_count,1)
        self.now+=timedelta(minutes=10)
        self.official={1:{'race':1,'combo':'123','payout':990}}
        self.runner.run_once()
        self.assertEqual(model.load()['samples'],1)
        self.assertEqual(self.statuses[-1]['settled'],1)
        self.assertEqual(self.statuses[-1]['pending'],0)
        rates=core.app.test_client().get('/api/performance').json['automatic_category_stats']
        for name in ['gachi','roman','oni']:
            self.assertEqual(rates[name]['roi'],990)
            self.assertEqual(rates[name]['hit_rate'],100)
        self.runner.run_once(); self.assertEqual(model.load()['samples'],1)

    def test_late_start_never_backfills_predictions_after_result(self):
        self.now=self.deadline+timedelta(minutes=1)
        self.official={1:{'race':1,'combo':'123','payout':990}}
        self.runner.run_once()
        self.assertEqual(core.load_ledger(),[])
        core.predict_race.assert_not_called()

    def test_incomplete_exhibition_waits_without_model_or_ledger_mutation(self):
        core.get.return_value='<table><tr><td>1</td><td></td><td>選手</td><td>52kg</td><td>6.7</td><td>.19</td></tr></table>'
        self.runner.run_once()
        self.assertEqual(core.load_ledger(),[])
        self.assertEqual(model.load()['samples'],0)
        core.predict_race.assert_not_called()

    def test_prediction_that_finishes_too_late_is_not_saved(self):
        def slow_prediction(*args,**kwargs):
            self.now=self.deadline-timedelta(seconds=10)
            return prediction()
        core.predict_race.side_effect=slow_prediction
        self.runner.run_once()
        self.assertEqual(core.load_ledger(),[])
        self.assertEqual(self.statuses[-1]['predicted'],0)

    def test_just_before_deadline_does_not_start_expensive_analysis(self):
        self.now=self.deadline-timedelta(seconds=89)
        self.runner.run_once()
        core.predict_race.assert_not_called()

    def test_future_races_are_not_polled_before_the_exhibition_window(self):
        self.deadline=self.now+timedelta(minutes=46)
        self.runner.run_once()
        core.get.assert_not_called()

    def test_missing_odds_waits_and_recovers_next_cycle(self):
        result=prediction();result['odds_count']=119
        core.predict_race.return_value=result; core.predict_race.side_effect=None
        self.runner.run_once();self.assertEqual(core.load_ledger(),[])
        core.predict_race.return_value=prediction()
        self.runner.run_once();self.assertEqual(len(core.load_ledger()),1)

    def test_network_failure_does_not_invent_data_and_retries(self):
        core.get.side_effect=TimeoutError('test timeout')
        self.runner.run_once();self.assertEqual(core.load_ledger(),[])
        self.assertEqual(self.statuses[-1]['retries'],1)
        core.get.side_effect=None
        self.runner.run_once();self.assertEqual(len(core.load_ledger()),1)

    def test_manual_save_cannot_change_frozen_automatic_bets(self):
        self.runner.run_once();original=core.load_ledger()[0]
        response=core.app.test_client().post('/api/performance',json={'date':'20261008','stadium':'10','race':1,'bets':[]})
        self.assertEqual(response.status_code,409)
        self.assertEqual(core.load_ledger()[0],original)

    def test_saved_forecast_is_available_without_a_manual_prediction_request(self):
        self.runner.run_once()
        response=core.app.test_client().get('/api/saved_prediction?date=20261008&stadium=10&race=1')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json['record']['prediction']['main'],1)

    def test_next_day_restart_settles_saved_forecast_without_repredicting(self):
        self.runner.run_once()
        self.now+=timedelta(days=1)
        self.official={1:{'race':1,'combo':'123','payout':990}}
        self.deadline=self.now-timedelta(minutes=30)
        self.runner=automation.AutoRunner(core,clock=lambda:self.now,persist=self.statuses.append)
        with mock.patch.object(self.runner,'deadlines',return_value={1:self.deadline}),mock.patch.object(self.runner,'official_results',return_value=self.official):
            self.runner.run_once()
        self.assertEqual(model.load()['samples'],1)
        self.assertTrue(core.load_ledger()[0]['learned'])
        self.assertEqual(core.predict_race.call_count,1)

    def test_manual_and_automatic_requests_settle_only_once_even_when_concurrent(self):
        self.runner.run_once()
        result={'race':1,'combo':'123','payout':990}
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses=list(pool.map(lambda _:core.settle_saved_prediction('20261008','10',1,result),range(4)))
        self.assertTrue(all(status==200 for _,status in responses))
        self.assertEqual(sum(data['status']=='settled' for data,_ in responses),1)
        self.assertEqual(model.load()['samples'],1)

    def test_same_clock_in_different_venues_does_not_collide_record_ids(self):
        for jcd in ['10','24']:
            core.save_automatic_prediction('20261008',jcd,1,prediction(),self.deadline,self.now,clock=lambda:self.now)
        rows=core.load_ledger()
        self.assertEqual(len(rows),2)
        self.assertEqual(len({row['id'] for row in rows}),2)

    def test_scope_preserves_manual_history_and_separates_automatic_stats(self):
        core.app.test_client().post('/api/performance',json={'date':'20261007','stadium':'10','race':1,'bets':[{'bet':'1-2-3','category':'gachi'}]})
        core.settle_saved_prediction('20261007','10',1,{'race':1,'combo':'123','payout':990})
        self.runner.run_once()
        stats=core.app.test_client().get('/api/performance').json
        self.assertEqual(stats['category_stats']['gachi']['settled'],1)
        self.assertEqual(stats['automatic_category_stats']['gachi']['settled'],0)
        self.assertEqual(len(core.load_ledger()),2)

    def test_existing_manual_settled_race_is_not_repredicted(self):
        core.app.test_client().post('/api/performance',json={'date':'20261008','stadium':'10','race':1,'bets':[]})
        core.settle_saved_prediction('20261008','10',1,{'race':1,'combo':'123','payout':990})
        self.assertIsNone(core.save_automatic_prediction('20261008','10',1,prediction(),self.deadline,self.now,clock=lambda:self.now))


class DeadlineAndTransactionTests(unittest.TestCase):
    def test_automatic_processing_cannot_use_empty_ledger_after_database_error(self):
        @state_store.strict_state_reads
        def read():
            return core.load_ledger()
        with mock.patch.dict(os.environ,{'DATABASE_URL':'postgresql://test','POSTGRES_URL':''}),mock.patch.object(core,'_ensure_ledger_table',side_effect=RuntimeError('database down')):
            with self.assertRaisesRegex(RuntimeError,'database down'):
                read()
        self.assertFalse(state_store.storage_required())

    def test_automatic_processing_cannot_use_default_model_after_database_error(self):
        @state_store.strict_state_reads
        def read():
            return model.load()
        with mock.patch.dict(os.environ,{'DATABASE_URL':'postgresql://test','POSTGRES_URL':''}),mock.patch.object(model,'_ensure_table',side_effect=RuntimeError('database down')):
            with self.assertRaisesRegex(RuntimeError,'database down'):
                read()

    def test_schedule_pairs_race_numbers_with_japanese_deadlines(self):
        html='<table><thead><tr><th>レース</th><th><a>1R</a></th><th><a>12R</a></th></tr></thead><tbody><tr><td>締切予定時刻</td><td>１０：３６</td><td>20:38</td></tr></tbody></table>'
        result=automation.parse_deadlines(html,'20261008')
        self.assertEqual(result[1].isoformat(),'2026-10-08T10:36:00+09:00')
        self.assertEqual(result[12].hour,20)
        self.assertEqual(automation.parse_deadlines(html.replace('20:38','未定'),'20261008').keys(),{1})

    def test_database_substeps_cannot_commit_before_outer_transaction(self):
        connection=mock.Mock()
        connection.transaction.return_value.__enter__=mock.Mock(return_value=None)
        connection.transaction.return_value.__exit__=mock.Mock(return_value=False)
        session=mock.MagicMock();session.__enter__.return_value=connection
        psycopg=mock.Mock();psycopg.connect.return_value=session
        with mock.patch.dict(os.environ,{'DATABASE_URL':'postgresql://test','POSTGRES_URL':''}),mock.patch.dict(sys.modules,{'psycopg':psycopg}):
            with state_store.atomic_state():
                with state_store.state_connection() as shared:
                    shared.commit()
                with state_store.atomic_state():
                    with state_store.state_connection() as nested:
                        nested.commit()
            connection.commit.assert_not_called()
            self.assertEqual(psycopg.connect.call_count,1)

    def test_database_error_propagates_to_outer_transaction_for_rollback(self):
        transaction=mock.MagicMock();connection=mock.Mock();connection.transaction.return_value=transaction
        session=mock.MagicMock();session.__enter__.return_value=connection
        psycopg=mock.Mock();psycopg.connect.return_value=session
        with mock.patch.dict(os.environ,{'DATABASE_URL':'postgresql://test','POSTGRES_URL':''}),mock.patch.dict(sys.modules,{'psycopg':psycopg}):
            with self.assertRaisesRegex(RuntimeError,'save failed'):
                with state_store.atomic_state():
                    raise RuntimeError('save failed')
        self.assertIs(transaction.__exit__.call_args.args[0],RuntimeError)
        self.assertFalse(state_store.in_state_transaction())

    def test_wrong_render_service_and_no_database_never_start_automation(self):
        with mock.patch.dict(os.environ,{'RENDER_SERVICE_ID':'other-service','BOAT_AUTO_RUNNER':'0','DATABASE_URL':'postgresql://test'}):
            self.assertFalse(automation.configured())
        with mock.patch.dict(os.environ,{'RENDER_SERVICE_ID':automation.PRODUCTION_SERVICE,'DATABASE_URL':'','POSTGRES_URL':''}):
            self.assertFalse(automation.configured())


if __name__=='__main__':
    unittest.main(verbosity=2)

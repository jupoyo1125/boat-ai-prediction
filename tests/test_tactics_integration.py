"""Guarded integration of official tactics into current AI predictions."""
import copy
import unittest
from unittest import mock

import app as core
import model
import biwako_tactics as tactics
from tactics_integration import integrate_tactical_scores, TACTICS_VERSION


def six_boats():
    return [
        {"boat":n,"racer_id":str(4000+n),"entry_course":n,
         "score":60.0-n,"second_score":50.0+n,"third_score":40.0+n,
         "course_rates":{"starts":75,"first":40.0,"second":25.0,"third":15.0},
         "courses":50.0,"course_firsts":50.0,"course_seconds":50.0,"course_thirds":50.0,
         "motors":50.0,"exhibitions":50.0,"meetings":50.0}
        for n in range(1,7)]


def prepared_rows():
    return [
        {"boat":n, "racer_id":str(4000+n),"course":n,"provisional_course":False,
         "starts":80,"baseline_starts":75,"venue_starts":300,
         "sample_warning":False,
         "rates": {
             "first":{"base":40.0,"delta":4.0 if n==1 else -2.0,"estimated":44.0 if n==1 else 38.0},
             "second":{"base":25.0,"delta":-1.0 if n==1 else 1.0,"estimated":24.0 if n==1 else 26.0},
             "third":{"base":15.0,"delta":-0.5 if n==1 else 2.0,"estimated":14.5 if n==1 else 17.0},
         }} for n in range(1,7)
    ]


class TacticsFusionTests(unittest.TestCase):
    def setUp(self):
        self.readiness={"ready_days":365,"venue_days":365,"total_days":365}
    
    def test_rates_change_three_finish_scores_but_never_learning_features(self):
        boats=six_boats()
        old=copy.deepcopy(boats)
        model_features=core.feature_snapshot(boats)
        status=integrate_tactical_scores(boats,prepared_rows(),self.readiness)
        self.assertTrue(status["applied"])
        self.assertEqual(status["eligible_boats"],6)
        self.assertEqual(status["version"],TACTICS_VERSION)
        self.assertEqual(boats[0]["score"],old[0]["score"]+1.8)
        self.assertEqual(boats[0]["second_score"],old[0]["second_score"]-0.45)
        self.assertEqual(boats[0]["third_score"],old[0]["third_score"]-0.23)
        self.assertEqual(boats[1]["score"],old[1]["score"]-0.9)
        self.assertEqual(core.feature_snapshot(boats),model_features)
        self.assertEqual(set(model.DEFAULT['weights']),set(model.FEATURE_KEYS))
        self.assertEqual(boats[0]["tactics_adjustment"]["status"],"applied")

    def test_shadow_and_off_keep_bets_and_scores_unchanged(self):
        for mode in ("off","shadow"):
            with self.subTest(mode=mode):
                boats=six_boats()
                old=copy.deepcopy(boats)
                status=integrate_tactical_scores(boats,prepared_rows(),self.readiness,mode)
                self.assertFalse(status["applied"])
                self.assertEqual([b["score"] for b in boats],[b["score"] for b in old])
                self.assertEqual([b["second_score"] for b in boats],
                                 [b["second_score"] for b in old])
                if mode=="off":
                    self.assertEqual(boats,old)
                else:
                    self.assertEqual(boats[0]["tactics_adjustment"]["status"],"shadow")

    def test_no_leakage_no_venue_sample_unknown_courses_or_missing_racer(self):
        samples = [
            ({"ready_days":365,"venue_days":12,"total_days":365},prepared_rows(),six_boats()),
            (self.readiness,prepared_rows(),six_boats()),
            (self.readiness,prepared_rows(),six_boats()),
            (self.readiness,prepared_rows(),six_boats()),
        ]
        samples[1][1][0]["racer_id"]="9999"
        samples[2][2][0]["entry_course"]=None
        for row in samples[3][1][2:]:
            row["venue_starts"]=10
        for readiness,rows,boats in samples:
            with self.subTest(readiness=readiness,changed=boats[0].get("entry_course")):
                old=copy.deepcopy(boats)
                status=integrate_tactical_scores(boats,rows,readiness)
                self.assertFalse(status["applied"])
                self.assertEqual([x["score"] for x in old],[x["score"] for x in boats])
        # Confirmed-entry-only: even a clear boat number does not substitute
        # for a missing exhibition lane.
        rows=prepared_rows()
        rows[0]['provisional_course']=True
        b=six_boats()
        self.assertFalse(integrate_tactical_scores(b,rows,self.readiness)['applied'])

    def test_deltas_are_bounded_and_missing_baselines_are_rejected(self):
        boats=six_boats()
        rows=prepared_rows()
        rows[1]['rates']['first']['delta']=40
        status=integrate_tactical_scores(boats,rows,self.readiness)
        self.assertTrue(status['applied'])
        self.assertNotIn('tactics_adjustment',boats[1])
        self.assertLessEqual(max(abs(b.get('tactics_adjustment',{}).get('score_adjustments',{}).get('first',0))
                                 for b in boats),3.6)
        rows=prepared_rows()
        rows[0]['rates']['first']['base']=55
        self.assertFalse(integrate_tactical_scores(six_boats(),rows,self.readiness)['applied'])

    def test_actual_forecast_path_uses_previous_day_hist_and_preserves_existing_tickets(self):
        raw={n: [str(n),'',f'{4000+n} / A1 選手{n}', '.15', '5.0', '5.0', f'20 {50+n} 65']
             for n in range(1,7)}
        before={'entry_course': {n:n for n in range(1,7)},
                'exhibition': {n:6.7-n*.01 for n in range(1,7)},
                'wind':1,'wave':0,'air':20,'water':20}
        snapshot={'start_date':'20251115','end_date':'20261114','days':365,
                  'totals': {(str(4000+n),n):[80,32,20,12] for n in range(1,7)}}
        racer={(str(4000+n),n):(
              [80,42,20,12,46,10,7,0,0,0,0,0] if n==1
              else [80,15,20,16,0,0,0,44,14,11,12,9])
              for n in range(1,7)}
        venue={n:([600,325,115,84,320,85,65,0,0,0,0,0] if n==1
                  else [600,110,130,105,0,0,0,350,60,45,105,88])
               for n in range(1,7)}
        archive={'racer':racer,'venue':venue}
        readiness={'ready_days':365,'venue_days':365,'total_days':365}
        class Page:
            def cancel(self): pass
        results=[]
        all_odds = {''.join(map(str, order)): 100.0
                    for order in core.permutations(range(1, 7), 3)}
        def request(mode, faulty=False, ranking=True):
            with mock.patch.dict('os.environ',{'BOAT_TACTICS_PREDICTION_MODE':mode}),\
                 mock.patch.object(core,'database_url',return_value='db'),\
                 mock.patch.object(core,'submit_official_page',return_value=Page()),\
                 mock.patch.object(core,'await_official_page',return_value='<fixture>'),\
                 mock.patch.object(core,'boats_from',return_value=raw),\
                 mock.patch.object(core,'meeting_results',return_value={}),\
                 mock.patch.object(core,'parse_odds',return_value=all_odds),\
                 mock.patch('line_prediction.prepare_forecast',return_value=None),\
                 mock.patch.object(tactics,'compact_summary',
                                   side_effect=RuntimeError('db error') if faulty
                                   else lambda *a,**kw:(archive,readiness,None,False)) as summary:
                output=core.predict_race('20261115','04',3, before=before,
                        model=copy.deepcopy(model.DEFAULT),hist=snapshot,ranking_only=ranking)
            results.append((output,summary))
            return output
        baseline=request('off')
        fused=request('on')
        shadow=request('shadow')
        failover=request('on',faulty=True)
        self.assertFalse(baseline['tactics_integration']['applied'])
        self.assertTrue(fused['tactics_integration']['applied'])
        self.assertTrue(any(b['score']!=next(o['score'] for o in baseline['boats']
                         if o['boat']==b['boat']) for b in fused['boats']))
        self.assertEqual(baseline['features'],fused['features'])
        self.assertEqual([b['score'] for b in baseline['boats']],
                         [b['score'] for b in shadow['boats']])
        self.assertEqual([b['score'] for b in baseline['boats']],
                         [b['score'] for b in failover['boats']])
        self.assertEqual(failover['tactics_integration']['reason'],'source_unavailable')
        self.assertFalse(failover['tactics_integration']['applied'])
        self.assertEqual(fused['tactics_integration']['version'],TACTICS_VERSION)
        summary_calls=results[1][1].call_args
        self.assertEqual(summary_calls.args[0],'20261115')
        self.assertEqual(summary_calls.args[3],'04')
        self.assertFalse(summary_calls.kwargs['include_baseline'])
        self.assertEqual(fused['history']['end_date'],'20261114')
        self.assertEqual(fused['feature_schema'],model.FEATURE_SCHEMA)
        self.assertTrue(fused['ranking_only'])
        self.assertEqual(fused['bets'],[])

        # Both ticket sets must use the same, already fetched odds snapshot.
        # The old control picks are kept compactly inside the saved prediction.
        original_tickets=request('off',ranking=False)
        integrated_tickets=request('on',ranking=False)
        comparison=integrated_tickets['tactics_comparison']
        self.assertTrue(comparison['same_odds_snapshot'])
        self.assertEqual(comparison['baseline_main'],original_tickets['main'])
        for category in core.BET_CATEGORIES:
            original=[b['bet'] for b in original_tickets['bets']
                      if b['category']==category]
            self.assertEqual(comparison['baseline_bets'][category],original)
        self.assertNotIn('tactics_comparison',original_tickets)
        self.assertEqual(set(b['bet'] for b in integrated_tickets['bets']
                             if b['category']=='oni')
                         .issubset(set(b['bet'] for b in integrated_tickets['bets']
                                       if b['category']=='gachi')),True)
        self.assertFalse(set(b['bet'] for b in integrated_tickets['bets']
                             if b['category']=='roman')
                         & set(b['bet'] for b in integrated_tickets['bets']
                               if b['category']=='gachi'))
        self.assertLessEqual(len(comparison['baseline_bets']['gachi']),10)
        self.assertLessEqual(len(comparison['baseline_bets']['oni']),3)


if __name__=='__main__':
    unittest.main()

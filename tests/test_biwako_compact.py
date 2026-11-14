"""Biwako timeouts: only six racers' numeric totals leave the database."""
from contextlib import contextmanager
import json
import unittest
from unittest import mock

import app as core
import biwako_tactics


class CompactSummaryChecks(unittest.TestCase):
    def test_entrant_scoped_sql_and_provisional_baselines(self):
        queries = []
        class Cursor:
            def __init__(self, rows): self.rows = rows
            def fetchone(self): return self.rows[0]
            def fetchall(self): return self.rows
        class Connection:
            def execute(self, sql, params=None):
                queries.append((sql,params))
                if sql.startswith('SET LOCAL'): return Cursor([])
                if sql.startswith('SELECT (SELECT count'): return Cursor([(364,364)])
                if "boat_tactic_days" in sql and "racer" in sql:
                    return Cursor([('4001',1,*[80,44,15,12,42,10,6,0,0,0,0,0]),
                                   ('4002',2,*[75,13,22,15,0,0,0,40,12,7,15,10])])
                if "boat_tactic_days" in sql and "biwako" in sql:
                    return Cursor([(1,*[200,110,30,20,100,40,30,0,0,0,0,0]),
                                   (2,*[200,30,45,40,0,0,0,100,25,10,44,25])])
                if "boat_racer_course_days" in sql:
                    return Cursor([('4001',1,81,46,15,13),('4002',2,75,13,22,15)])
                raise AssertionError('Unexpected query '+sql)
        @contextmanager
        def connection():
            yield Connection()
        entries = {i:{'racer_id':str(4000+i)} for i in range(1,7)}
        with mock.patch.object(biwako_tactics, 'database_url', return_value='stub'), \
             mock.patch.object(biwako_tactics, 'ensure_schema'), \
             mock.patch.object(biwako_tactics, 'state_connection', connection), \
             mock.patch.object(biwako_tactics, 'read_days', side_effect=AssertionError('full archive read forbidden')):
            summary, readiness, baseline, partial = biwako_tactics.compact_summary(
                '20261115', entries, {'entry_course': {i:i for i in range(1,7)}})
        self.assertEqual(readiness['ready_days'], 364)
        self.assertEqual(readiness['baseline_days'], 364)
        self.assertEqual(readiness['end_date'], '20261114')
        self.assertTrue(readiness['provisional'])
        self.assertTrue(partial)
        self.assertEqual(summary['racer'][('4001',1)][0],80)
        self.assertEqual(summary['biwako'][1][0],200)
        self.assertEqual(baseline['totals'][('4002',2)], [75,13,22,15])
        self.assertEqual(len(queries),5)
        self.assertTrue(all('payload' not in q[0].lower() or
                            'SELECT payload FROM' not in q[0] for q in queries))
        for sql,params in queries[2:]:
            self.assertEqual(params[0], '20251115')
            self.assertEqual(params[1], '20261114')
            if len(params)==3:
                self.assertIn('4001', params[2])
        self.assertTrue(all('jsonb_array_elements' in sql for sql,_ in queries[2:]))

    def test_pending_data_returns_no_fabricated_rates(self):
        class Cursor:
            def fetchone(self): return (10,9)
        class Connection:
            def execute(self, sql, params=None):
                if sql.startswith('SET LOCAL'): return self
                return Cursor()
        @contextmanager
        def connection(): yield Connection()
        with mock.patch.object(biwako_tactics,'database_url',return_value='stub'),\
             mock.patch.object(biwako_tactics,'ensure_schema'),\
             mock.patch.object(biwako_tactics,'state_connection',connection):
            summary,readiness,snapshot,partial=biwako_tactics.compact_summary(
                '20261115',{1:{'racer_id':'4001'}},{})
        self.assertEqual(readiness['ready_days'],10)
        self.assertIsNone(snapshot)
        self.assertEqual(summary['racer'],{})

    def test_api_works_without_before_exhibition_and_does_not_scan_archives(self):
        record1 = [75,45,12,11,44,8,5,0,0,0,0,0]
        record2 = [70,14,19,12,0,0,0,38,10,5,20,15]
        summary={'racer':{('4001',1):record1,
                          **{(f'400{i}',i):record2 for i in range(2,7)}},
                 'biwako':{1:[160,93,33,19,91,40,20,0,0,0,0,0],
                            **{i:[160,20,40,35,0,0,0,90,26,16,24,19] for i in range(2,7)}}}
        readiness={'ready_days':364,'total_days':365,'baseline_days':364,
                   'provisional':True,'start_date':'20251115','end_date':'20261114'}
        snapshot={'totals':{(f'400{i}',i):[75,40 if i==1 else 12,20,11]
                            for i in range(1,7)}}
        racer_entries={i:['','','%d / A1 選手%d'%(4000+i,i)] for i in range(1,7)}
        class Page:
            def __init__(self,url): self.url=url
            def cancel(self): pass
        def page(future, deadline):
            if 'beforeinfo' in future.url:
                raise __import__('requests').Timeout('beforeinfo not ready')
            return '<fixture>'
        with mock.patch.object(core, 'database_url', return_value='fake'),\
             mock.patch.object(core, 'submit_official_page', side_effect=lambda url,d:Page(url)),\
             mock.patch.object(core, 'await_official_page', side_effect=page),\
             mock.patch.object(core, 'boats_from', return_value=racer_entries),\
             mock.patch.object(core, 'meeting_results', return_value={}),\
             mock.patch.object(biwako_tactics, 'compact_summary',
                               return_value=(summary,readiness,snapshot,True)) as compact,\
             mock.patch.object(biwako_tactics.history, 'get',
                               side_effect=AssertionError('do not scan full year')),\
             mock.patch.object(core.course_history, 'get',
                               side_effect=AssertionError('do not scan year')):
            response=core.app.test_client().get('/api/biwako_tactics?date=20261115&stadium=11&race=1')
        self.assertEqual(response.status_code,200,response.get_data(as_text=True))
        body=response.json
        self.assertTrue(body['ok'])
        self.assertTrue(body['baseline_pending'])
        self.assertTrue(body['before_provisional'])
        self.assertEqual(len(body['boats']),6)
        self.assertTrue(body['boats'][0]['provisional_course'])
        self.assertIsNotNone(body['boats'][0]['rates']['first']['estimated'])
        compact.assert_called_once()


if __name__ == '__main__':
    unittest.main()

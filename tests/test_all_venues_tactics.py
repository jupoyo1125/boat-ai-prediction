"""Regression checks for real 24-venue race tactics and incremental archives."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest import mock

import app as core
import biwako_tactics as tactics
from race_venues import STADIUMS


def official_block(stadium, winner, move):
    rows = []
    for boat in range(1, 7):
        rank = (boat - winner) % 6 + 1
        rows.append(f'  {rank:02d}  {boat} 400{boat} 選手 12 13 6.80 {boat} 0.12 1.50.0')
    return (f'{stadium}KBGN\n2026/11/14\n'
            f'   1R 予選 H1800m 晴\n    決まり手 {move}\n'
            + '\n'.join(rows) + f'\n{stadium}KEND\n')


class AllVenuesTacticTests(unittest.TestCase):
    def test_parser_preserves_legacy_biwako_and_separates_venue_course_rates(self):
        text = ('STARTK\n' + official_block('04', 1, '逃げ')
                + official_block('11', 2, '差し') + 'FINALK\n')
        day = tactics.parse_day(text, '20261114')
        self.assertEqual(day['format_version'], 1)
        venues = {(stadium, course): values for stadium, course, *values in day['venues']}
        self.assertEqual(len(venues), 12)
        self.assertEqual(venues[('04', 1)][tactics.ESCAPE], 1)
        self.assertEqual(venues[('04', 1)][tactics.PASSED], 0)
        self.assertEqual(venues[('11', 1)][tactics.PASSED], 1)
        self.assertEqual(venues[('11', 2)][tactics.SASHI], 1)
        self.assertEqual(venues[('04', 2)][tactics.SASHI], 0)
        biwako = {course: values for course, *values in day['biwako']}
        self.assertEqual(biwako[1], venues[('11', 1)])
        self.assertEqual(day['races'], 2)
        self.assertEqual(sum(values[0] for values in (x[2:] for x in day['venues'])), 12)

    def test_write_legacy_and_new_snapshots_without_overwriting_verified_days(self):
        queries, commits = [], []
        class Connection:
            def execute(self, sql, params):
                queries.append((sql, params))
            def commit(self): commits.append(True)
        @contextmanager
        def connection(): yield Connection()
        day = tactics.parse_day(
            'STARTK\n' + official_block('04', 1, '逃げ') + 'FINALK\n',
            '20261114')
        with mock.patch.object(tactics,'database_url',return_value='dummy'),\
             mock.patch.object(tactics,'require_storage_writable'),\
             mock.patch.object(tactics,'ensure_schema'),\
             mock.patch.object(tactics,'state_connection',connection):
            tactics.write_day(day)
        self.assertEqual(len(queries), 2)
        self.assertIn('ON CONFLICT (day) DO NOTHING',queries[0][0])
        self.assertIn('ON CONFLICT (day) DO NOTHING',queries[1][0])
        old_payload = json.loads(queries[0][1][1])
        new_payload = json.loads(queries[1][1][1])
        self.assertNotIn('venues',old_payload)
        self.assertEqual(new_payload['venues'][0][0],'04')
        self.assertEqual(len(commits),1)
        self.assertIn('ENABLE ROW LEVEL SECURITY', tactics.SCHEMA)
        self.assertIn('REVOKE ALL ON public.boat_tactic_venue_days FROM anon',tactics.SCHEMA)

    def test_sql_for_other_venue_never_uses_biwako_averages(self):
        queries=[]
        class Cursor:
            def __init__(self, rows): self.rows=rows
            def fetchone(self): return self.rows[0]
            def fetchall(self): return self.rows
        class Connection:
            def execute(self, sql, params=None):
                queries.append((sql,params))
                if sql.startswith('SET LOCAL'):return Cursor([])
                if sql.startswith('SELECT (SELECT count'):return Cursor([(365,365)])
                if sql.startswith('SELECT count(*) FROM public.boat_tactic_venue_days'):
                    return Cursor([(95,)])
                if "FROM public.boat_tactic_days" in sql and "'racer'" in sql:
                    return Cursor([('4001',1,*[70,38,15,10,40,12,9,0,0,0,0,0])])
                if "FROM public.boat_tactic_venue_days" in sql and "'venues'" in sql:
                    return Cursor([(1,*[420,230,58,60,220,90,62,0,0,0,0,0])])
                if 'FROM public.boat_racer_course_days' in sql:
                    return Cursor([('4001',1,72,40,14,12)])
                raise AssertionError(sql)
        @contextmanager
        def connection():yield Connection()
        with mock.patch.object(tactics,'database_url',return_value='db'),\
             mock.patch.object(tactics,'ensure_schema'),\
             mock.patch.object(tactics,'state_connection',connection),\
             mock.patch.object(tactics,'read_days',side_effect=AssertionError('no full history scan')):
            summary,ready,baseline,pending=tactics.compact_summary(
                '20261115', {1:{'racer_id':'4001'}}, {}, '04')
        self.assertEqual(ready['venue_days'],95)
        self.assertTrue(ready['venue_pending'])
        self.assertEqual(summary['venue'][1][tactics.ESCAPE],220)
        self.assertEqual(summary['racer'][('4001',1)][tactics.ESCAPE],40)
        self.assertEqual(baseline['totals'][('4001',1)],[72,40,14,12])
        self.assertFalse(pending)
        sql,params=[q for q in queries if "FROM public.boat_tactic_venue_days" in q[0] and "'venues'" in q[0]][0]
        self.assertEqual(params,('20251115','20261114','04'))
        self.assertEqual(len(STADIUMS),24)

    def test_worker_backfills_only_missing_venue_days_with_capped_official_fetches(self):
        """Legacy national totals exist, but missing venue stats still get filled."""
        class StopAfterBatch(BaseException):
            pass
        seen=[]
        slots=[]
        class Clock:
            @staticmethod
            def now(tz): return datetime(2026, 11, 15, 5, 0, tzinfo=tz)
        class Response:
            content=b'official-archive'
            def raise_for_status(self): pass
            def __enter__(self):return self
            def __exit__(self,*args):pass
        class Session:
            def get(self,url,headers,timeout):
                seen.append(url)
                return Response()
        class Slots:
            @contextmanager
            def slot(self,timeout,priority):
                slots.append((timeout,priority))
                yield
        class Core:
            _official_slots=Slots()
            HEAD={}
            app=mock.Mock()
            @staticmethod
            def official_session():return Session()
        written=[]
        def parse(raw,date):
            return {'format_version':1,'date':date,'venues':[['04',1,*([0]*12)]]}
        with mock.patch.object(tactics, 'datetime', Clock),\
             mock.patch.object(tactics, 'storage_paused', return_value=False),\
             mock.patch.object(tactics, 'known_days', side_effect=lambda days:set(days)),\
             mock.patch.object(tactics, 'known_venue_days', return_value=set()),\
             mock.patch.object(tactics, 'lease', contextmanager(lambda: (yield True))),\
             mock.patch.object(tactics, 'archive_url', side_effect=lambda day:'archive/'+day),\
             mock.patch.object(tactics, 'decode_archive', return_value='official'),\
             mock.patch.object(tactics, 'parse_day', side_effect=parse),\
             mock.patch.object(tactics, 'write_day', side_effect=written.append),\
             mock.patch.object(tactics.time, 'sleep', side_effect=StopAfterBatch):
            with self.assertRaises(StopAfterBatch):
                tactics.TacticHistory().run(Core())
        self.assertEqual([x['date'] for x in written], ['20261114','20261113'])
        self.assertEqual(seen,['archive/20261114','archive/20261113'])
        self.assertEqual(slots,[(10,1),(10,1)])


    def test_missing_venue_history_displays_raw_without_inventing_adjusted_rates(self):
        records = {('4001',1): [80,50,10,10,42,15,6,0,0,0,0,0]}
        records.update({(str(4000+i),i):[80,12,20,20,0,0,0,40,12,9,14,13]
                        for i in range(2,7)})
        snapshot = {'totals': {(str(4000+i),i):[80,25,20,15] for i in range(1,7)}}
        entries = {i:{'racer_id':str(4000+i),'name':f'選手{i}'} for i in range(1,7)}
        rows = tactics.tactical_rows(entries, {}, snapshot,
            {'racer':records,'venue':{}}, {'ready_days':365,'total_days':365,'venue_days':0})
        self.assertEqual(len(rows),6)
        self.assertEqual(rows[0]['escape'],52.5)
        self.assertEqual(rows[1]['sashi'],15.0)
        self.assertEqual(rows[0]['rates']['first']['base'],31.2)
        self.assertIsNone(rows[0]['rates']['first']['estimated'])
        self.assertIsNone(rows[1]['rates']['second']['estimated'])

    def test_generic_api_fetches_requested_venue_and_legacy_alias_stays_biwako_only(self):
        summary = {'racer':{(str(4000+i),i):[80,40,15,12,42,8,8,0,0,0,0,0]
                            if i==1 else [80,12,22,18,0,0,0,42,14,8,16,13]
                            for i in range(1,7)},
                   'venue':{i:([600,320,90,90,320,150,90,0,0,0,0,0] if i==1
                                 else [600,100,140,125,0,0,0,320,80,50,110,80])
                            for i in range(1,7)}}
        readiness={'ready_days':365,'venue_days':90,'venue_pending':True,'total_days':365,
                   'provisional':True,'start_date':'20251115','end_date':'20261114','baseline_days':365}
        snapshot={'totals':{(str(4000+i),i):[80,32,22,18] for i in range(1,7)}}
        roster={i:['','','400%d / A1 選手%d'%(i,i)] for i in range(1,7)}
        # The source must carry genuine 4001 ... 4006 registration numbers.
        roster={i:['','','%d / A1 選手%d'%(4000+i,i)] for i in range(1,7)}
        class Page:
            def __init__(self,url): self.url=url
            def cancel(self): pass
        pages=[]
        def submit(url,deadline):
            pages.append(url)
            return Page(url)
        def fetch(page,deadline):
            if 'beforeinfo' in page.url:raise __import__('requests').Timeout('not published')
            return '<fixture>'
        with mock.patch.object(core,'database_url',return_value='db'),\
             mock.patch.object(core,'submit_official_page',side_effect=submit),\
             mock.patch.object(core,'await_official_page',side_effect=fetch),\
             mock.patch.object(core,'boats_from',return_value=roster),\
             mock.patch.object(core,'meeting_results',return_value={}),\
             mock.patch.object(tactics,'compact_summary',
                               return_value=(summary,readiness,snapshot,False)) as calc:
            resp=core.app.test_client().get('/api/tactics?date=20261115&stadium=04&race=7')
            legacy=core.app.test_client().get('/api/biwako_tactics?date=20261115&stadium=04&race=7')
        self.assertEqual(resp.status_code,200,resp.get_data(as_text=True))
        self.assertEqual(legacy.status_code,400)
        self.assertEqual(resp.json['venue'],'平和島')
        self.assertEqual(resp.json['stadium'],'04')
        self.assertEqual(len(resp.json['boats']),6)
        self.assertIsNotNone(resp.json['boats'][0]['rates']['first']['estimated'])
        self.assertEqual(calc.call_args.args[-1],'04')
        self.assertTrue(any('jcd=04&rno=07' in url for url in pages))
        self.assertTrue(all('jcd=11' not in url for url in pages))


if __name__ == '__main__':
    unittest.main()

"""Tests for official-result tactical semantics and provisional estimates."""
from datetime import datetime
import unittest
from unittest import mock

from biwako_tactics import (parse_day, pct, tactical_rows, TacticHistory, START,
                           ESCAPE, PASSED, SWEPT, LET_ESCAPE, SASHI, MAKURI,
                           FOLLOW2, FOLLOW3)
from racer_course_history import year_window


def sample(date='20261025', mode='差 し', winner=2, venue='11'):
    rows = []
    for boat in range(1, 7):
        finish = '01' if boat == winner else f'{(boat if boat > winner else boat+1):02d}'
        rows.append(f'  {finish}  {boat} 400{boat} 選手 12 13 6.80 {boat} 0.12 1.50.0')
    return (f'STARTK\n{venue}KBGN\n2026/10/25\n'
            f'   1R 予選 H1800m 晴\n    決まり手 {mode}\n'
            + '\n'.join(rows) + f'\n{venue}KEND\nFINALK\n')


class ParserTests(unittest.TestCase):
    def test_official_race_counts_and_actual_courses(self):
        day = parse_day(sample(), '20261025')
        rows = {(racer, course): values for racer, course, *values in day['racer']}
        self.assertEqual(day['races'], 1)
        self.assertEqual(rows[(4001, 1)][START], 1)
        self.assertEqual(rows[(4001, 1)][PASSED], 1)
        self.assertEqual(rows[(4001, 1)][SWEPT], 0)
        self.assertEqual(rows[(4002, 2)][SASHI], 1)
        self.assertEqual(rows[(4002, 2)][MAKURI], 0)
        self.assertEqual(sum(row[START] for row in rows.values()), 6)
        self.assertEqual(len(day['biwako']), 6)
        self.assertNotIn('0.12', str(day))

    def test_makuri_and_escaped_second(self):
        swept = parse_day(sample(mode='まくり', winner=3), '20261025')
        by = {racer: values for racer, _, *values in swept['racer']}
        self.assertEqual(by[4001][SWEPT], 1)
        self.assertEqual(by[4003][MAKURI], 1)
        escaped = parse_day(sample(mode='逃 げ', winner=1), '20261025')
        by = {racer: values for racer, _, *values in escaped['racer']}
        self.assertEqual(by[4001][ESCAPE], 1)
        self.assertEqual(by[4002][LET_ESCAPE], 1)
        self.assertEqual(by[4002][FOLLOW2], 1)
        self.assertEqual(by[4003][FOLLOW3], 1)

    def test_unknown_method_and_wrong_day_not_fabricated(self):
        with self.assertRaises(ValueError):
            parse_day(sample(mode='決着不明'), '20261025')
        with self.assertRaises(ValueError):
            parse_day(sample(), '20261024')

    def test_entry_course_not_equal_boat_number(self):
        text = sample().replace('6.80 1 0.12', '6.80 4 0.12')
        # Duplicate actual courses invalidate the entire race rather than
        # silently falling back to boat numbers.
        day = parse_day(text, '20261025')
        self.assertEqual(day['races'], 0)


class ModelTests(unittest.TestCase):
    def test_raw_rates_and_bounded_adjustments_without_future_info(self):
        base = [80, 40, 20, 20, 28, 10, 6, 0, 0, 0, 0, 0]
        other = [80, 18, 25, 20, 0, 0, 0, 40, 18, 8, 9, 11]
        venue1 = [700, 380, 150, 100, 340, 110, 105, 0, 0, 0, 0, 0]
        venue2 = [700, 120, 170, 130, 0, 0, 0, 360, 85, 60, 120, 95]
        records = {('4001', 1): base}
        records.update({(f'400{i}', i): other for i in range(2, 7)})
        summary = {'racer': records, 'biwako': {1: venue1, **{i: venue2 for i in range(2, 7)}}}
        snapshot = {'totals': {(f'400{i}', i): [80, 36 if i == 1 else 14, 24, 15]
                               for i in range(1, 7)}}
        entries = {i: {'racer_id': f'400{i}', 'name': '選手'} for i in range(1, 7)}
        ready = {'ready_days': 200, 'total_days': 365}
        rows = tactical_rows(entries, {'entry_course': {i: i for i in range(1, 7)}},
                             snapshot, summary, ready)
        self.assertEqual(rows[0]['escape'], 35.0)
        self.assertEqual(rows[0]['passed'], 12.5)
        self.assertEqual(rows[1]['let_escape'], 50.0)
        self.assertEqual(rows[1]['sashi'], 22.5)
        self.assertTrue(any(r['rates']['first']['delta'] for r in rows))
        self.assertTrue(all(abs(r['rates'][key]['delta']) <= 8.1
                            for r in rows for key in ('first', 'second', 'third')
                            if r['rates'][key]['delta'] is not None))
        unknown = dict(entries)
        unknown[6] = {'racer_id': '9999', 'name': '未経験'}
        rows = tactical_rows(unknown, {}, snapshot, summary, ready)
        self.assertIsNone(rows[5]['rates']['first']['estimated'])
        self.assertTrue(rows[0]['provisional_course'])

    def test_history_uses_only_dates_before_race_and_batches(self):
        history = TacticHistory()
        received = []
        class DummyDay(dict):
            pass
        def read(dates):
            received.extend(dates)
            return []
        with mock.patch('biwako_tactics.known_days', return_value=set()), \
             mock.patch('biwako_tactics.read_days', side_effect=read):
            _, status = history.get('20261026')
        self.assertEqual(status['ready_days'], 0)
        self.assertEqual(status['end_date'], '20261025')
        self.assertNotIn('20261026', received)
        self.assertTrue(all('20251026' <= d < '20261026' for d in received))


if __name__ == '__main__':
    unittest.main()

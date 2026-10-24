import json
import unittest
from unittest import mock

import app as core
import racer_course_history as history
import automation


class MemoryBoundsTests(unittest.TestCase):
    def test_race_update_never_reads_or_deletes_unrelated_ledger_records(self):
        connection = mock.Mock()
        row = {'id': 'auto:20261024:11:05', 'date': '20261024', 'stadium': '11', 'race': 5}
        with mock.patch.object(core, '_db_enabled', return_value=True), \
                mock.patch.object(core, 'in_state_transaction', return_value=True), \
                mock.patch.object(core, '_db_connect') as connect, \
                mock.patch.object(core, 'load_ledger', side_effect=AssertionError('full ledger')), \
                mock.patch.object(core, 'save_ledger', side_effect=AssertionError('full replacement')):
            connect.return_value.__enter__.return_value = connection
            core.save_race_record(row)
        sql, args = connection.execute.call_args.args
        self.assertNotIn('DELETE', sql)
        self.assertEqual(json.loads(args[0]), [{'id': row['id'], 'record': row}])
        connection.commit.assert_not_called()

    def test_race_writes_cannot_escape_the_model_and_ledger_transaction(self):
        with mock.patch.object(core, '_db_enabled', return_value=True), \
                mock.patch.object(core, 'in_state_transaction', return_value=False), \
                mock.patch.object(core, '_db_connect') as connect:
            with self.assertRaises(RuntimeError):
                core.save_race_record({'id': 'test'})
            connect.assert_not_called()

    def test_annual_totals_accept_a_single_pass_stream_and_reject_duplicate_days(self):
        dates = history.year_window('20261024')
        def days():
            for date in dates:
                yield {'date': date, 'format_version': 1, 'counts': [[1234, 2, 1, 0, 1, 0]]}
        result = history.aggregate_days(days(), '20261024')
        self.assertEqual(result['totals'][('1234', 2)], [365, 0, 365, 0])
        with self.assertRaises(ValueError):
            history.aggregate_days(iter([{'date': dates[0], 'format_version': 1, 'counts': []}] * 365), '20261024')

    def test_missing_predictions_after_official_result_leave_an_explicit_reason(self):
        # Persisted states cover unrecorded races, without making up past bets.
        core_mock = mock.Mock()
        runner = automation.AutoRunner(core_mock, persist=lambda state: None)
        with mock.patch.object(automation, 'configured', return_value=True), \
                mock.patch.object(automation, 'write_events') as write:
            runner.event('20261024', '11', 5, 'prediction_missing', '保存された予想がありません。')
            runner.event('20261024', '11', 5, 'prediction_missing', '保存された予想がありません。')
            runner.flush_events('20261024', '11')
        write.assert_called_once()
        self.assertEqual(write.call_args.args[0][0]['state'], 'prediction_missing')


if __name__ == '__main__':
    unittest.main()

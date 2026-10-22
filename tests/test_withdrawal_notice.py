import sys
from pathlib import Path
from datetime import datetime, timedelta
from unittest import TestCase, mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import automation
import threads_publisher as tp
from test_threads_publisher import MemoryStore


class WithdrawalTests(TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 23, 10, 0, tzinfo=tp.JST)
        self.deadline = self.now + timedelta(minutes=10)
        self.config = tp.Config('20261023', 1, 'test', stadium='05', post_format='compact_text')
        self.before = {'withdrawn_boats': [4],
                       'exhibition': {i: 6.7 for i in (1, 2, 3, 5, 6)},
                       'exhibition_st': {i: .1 for i in (1, 2, 3, 5, 6)}}

    def candidate(self):
        return tp.withdrawal_candidate(self.config, '20261023', '05', 1,
                                       self.deadline, self.before, self.now)

    def test_five_boats_with_explicit_withdrawal_get_notice_once(self):
        entry = self.candidate()
        self.assertIn('4号艇の欠場により5艇立て', entry['text'])
        self.assertNotIn('本命', entry['text'])
        store = MemoryStore()
        store.enqueue([entry]); store.enqueue([entry])
        self.assertEqual(len(store.read()['posts']), 1)
        self.assertIsNotNone(store.claim(self.config, self.now))
        self.assertIsNone(store.claim(self.config, self.now))

    def test_missing_exhibition_alone_never_means_withdrawal(self):
        self.before['withdrawn_boats'] = []
        self.assertIsNone(self.candidate())

    def test_wait_for_exhibition_and_reject_wrong_scope_or_late_notice(self):
        del self.before['exhibition_st'][6]
        self.assertIsNone(self.candidate())
        self.before['exhibition_st'][6] = .1
        self.now = self.deadline
        self.assertIsNone(self.candidate())
        self.now -= timedelta(minutes=10)
        self.config = tp.Config('20261023', 1, 'test', stadium='18')
        self.assertIsNone(self.candidate())

    def test_official_withdrawal_cell_is_parsed(self):
        before = app.parse_before('<table><tr><td>４</td><td></td><td>選手</td><td>欠場</td></tr></table>')
        self.assertEqual(before['withdrawn_boats'], [4])
        self.assertEqual(before['exhibition'], {})

    def test_automation_never_predicts_when_withdrawn(self):
        core = mock.Mock()
        core.parse_before.return_value = self.before
        runner = automation.AutoRunner(core, clock=lambda: self.now, persist=lambda _: None)
        runner.deadlines = lambda *_: {1: self.deadline}
        runner.official_results = lambda *_: {}
        runner.settle = mock.Mock()
        with mock.patch.object(tp, 'enqueue_withdrawal_notice') as enqueue:
            runner.process_venue('20261023', {'stadium': '05'}, [])
        core.predict_race.assert_not_called()
        core.save_automatic_prediction.assert_not_called()
        enqueue.assert_called_once()

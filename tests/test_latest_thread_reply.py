import unittest
from datetime import datetime, timedelta, timezone
from latest_thread_reply import choose_reply


class LatestReplyTests(unittest.TestCase):
    now = datetime.fromisoformat('2026-11-07T12:00:00+09:00')
    lead = timedelta(seconds=90)

    def row(self, race, minutes_ago, remaining=10):
        return (f'auto:20261107:04:{race:02d}',
                (self.now - timedelta(minutes=minutes_ago)).astimezone(timezone.utc).isoformat(),
                (self.now + timedelta(minutes=remaining)).isoformat(), f'本文{race}', None)

    def test_latest_publication_not_race_number_or_insertion_order(self):
        new = self.row(3, 1)
        old = self.row(8, 4)
        self.assertEqual(choose_reply([new, old], self.now, self.lead)[0], '本文3')

    def test_closed_latest_does_not_fall_back_to_older_open_post(self):
        text, deadline = choose_reply([self.row(2, 2, 1), self.row(1, 5)], self.now, self.lead)
        self.assertIn('配信終了', text)
        self.assertIsNone(deadline)

    def test_not_yet_published_and_wrong_day_are_excluded(self):
        yesterday = list(self.row(1, 8))
        yesterday[0] = 'auto:20261106:04:01'
        self.assertIn('まだ公開', choose_reply([yesterday, self.row(2, -1)], self.now, self.lead)[0])

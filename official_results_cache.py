"""Share immutable official daily results without caching incomplete downloads."""
from collections import OrderedDict
import copy
import threading
import time

import requests


class DailyResultsCache:
    def __init__(self, loader, limit=16, retry_seconds=60, clock=time.monotonic):
        self.loader = loader
        self.limit = limit
        self.retry_seconds = retry_seconds
        self.clock = clock
        self._lock = threading.Lock()
        self._records = OrderedDict()
        self._loading = {}
        self._failures = {}

    def clear(self):
        with self._lock:
            self._records.clear()
            self._failures.clear()

    def get(self, date, timeout=15, priority=0):
        deadline = self.clock() + timeout
        with self._lock:
            if date in self._records:
                self._records.move_to_end(date)
                return copy.deepcopy(self._records[date])
            failure = self._failures.get(date)
            if failure and failure[0] > self.clock():
                raise failure[1]
            pending = self._loading.get(date)
            owner = pending is None
            if owner:
                pending = {'event': threading.Event(), 'records': None, 'error': None}
                self._loading[date] = pending

        if not owner:
            if not pending['event'].wait(max(0, deadline - self.clock())):
                raise requests.Timeout('過去の公式結果の共有取得を待っています。')
            if pending['error'] is not None:
                raise pending['error']
            return copy.deepcopy(pending['records'])

        try:
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise requests.Timeout('過去の公式結果の取得待ちが時間切れになりました。')
            records = self.loader(date, timeout=remaining, priority=priority)
            with self._lock:
                self._records[date] = copy.deepcopy(records)
                self._records.move_to_end(date)
                while len(self._records) > self.limit:
                    self._records.popitem(last=False)
                self._failures.pop(date, None)
                pending['records'] = self._records[date]
            return copy.deepcopy(records)
        except Exception as error:
            with self._lock:
                pending['error'] = error
                self._failures[date] = (self.clock() + self.retry_seconds, error)
                # Bound failure metadata as well as successful downloads.
                while len(self._failures) > self.limit:
                    self._failures.pop(next(iter(self._failures)))
            raise
        finally:
            with self._lock:
                self._loading.pop(date, None)
                pending['event'].set()

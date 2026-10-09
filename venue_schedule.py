"""Share confirmed venue schedules between the UI and automatic processing."""
from collections import OrderedDict
import copy
import threading
import time

import requests


class VenueScheduleCache:
    def __init__(self, loader, ttl=900, stale_ttl=3600, maxsize=8, wait_timeout=15,
                 clock=time.monotonic):
        self.loader = loader
        self.ttl = ttl
        self.stale_ttl = stale_ttl
        self.maxsize = maxsize
        self.wait_timeout = wait_timeout
        self.clock = clock
        self._condition = threading.Condition()
        self._cache = OrderedDict()
        self._pending = set()

    def _snapshot(self, date, cached=True):
        saved_at, venues = self._cache[date]
        age = max(0, self.clock() - saved_at)
        self._cache.move_to_end(date)
        return {'venues': copy.deepcopy(venues), 'cached': cached,
                'stale': age >= self.ttl, 'age_seconds': int(age)}

    def get(self, date):
        deadline = time.monotonic() + self.wait_timeout
        with self._condition:
            while True:
                cached = self._cache.get(date)
                age = self.clock() - cached[0] if cached else float('inf')
                if age < self.ttl:
                    return self._snapshot(date)
                if date not in self._pending:
                    self._pending.add(date)
                    break
                # A confirmed schedule can still be shown while it is refreshed.
                if age < self.stale_ttl:
                    return self._snapshot(date)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise requests.Timeout('開催場の取得中です。時間をおいて再試行してください。')
                self._condition.wait(remaining)
        try:
            venues = self.loader(date)
            with self._condition:
                self._cache[date] = (self.clock(), copy.deepcopy(venues))
                while len(self._cache) > self.maxsize:
                    self._cache.popitem(last=False)
                return self._snapshot(date, cached=False)
        except Exception:
            with self._condition:
                cached = self._cache.get(date)
                if cached and self.clock() - cached[0] < self.stale_ttl:
                    return self._snapshot(date)
            raise
        finally:
            with self._condition:
                self._pending.remove(date)
                self._condition.notify_all()

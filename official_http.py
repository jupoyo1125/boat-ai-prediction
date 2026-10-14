"""Keep official HTTP requests bounded and serve waiting callers in order."""
from collections import deque
from contextlib import contextmanager
import threading
import time

import requests


class OfficialRequestBusy(requests.Timeout):
    """The shared network cap is busy; no external connection was opened."""


class OfficialRequestSlots:
    def __init__(self, limit=3):
        self.limit = limit
        self._condition = threading.Condition()
        self._waiting = deque()
        self._active = 0

    @contextmanager
    def slot(self, timeout=15, priority=0):
        ticket = (priority, object())
        deadline = time.monotonic() + timeout
        with self._condition:
            self._waiting.append(ticket)
            self._condition.notify_all()
            try:
                while self._active >= self.limit or min(self._waiting, key=lambda item: item[0]) is not ticket:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise OfficialRequestBusy('公式サイトへの通信が混み合っています。再試行してください。')
                    self._condition.wait(remaining)
            except BaseException:
                self._waiting.remove(ticket)
                self._condition.notify_all()
                raise
            self._waiting.remove(ticket)
            self._active += 1
            self._condition.notify_all()
        try:
            yield
        finally:
            with self._condition:
                self._active -= 1
                self._condition.notify_all()

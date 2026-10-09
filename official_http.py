"""Keep official HTTP requests bounded and serve waiting callers in order."""
from collections import deque
from contextlib import contextmanager
import threading
import time

import requests


class OfficialRequestSlots:
    def __init__(self, limit=3):
        self.limit = limit
        self._condition = threading.Condition()
        self._waiting = deque()
        self._active = 0

    @contextmanager
    def slot(self, timeout=15):
        ticket = object()
        deadline = time.monotonic() + timeout
        with self._condition:
            self._waiting.append(ticket)
            try:
                while self._active >= self.limit or self._waiting[0] is not ticket:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise requests.Timeout('公式サイトへの通信が混み合っています。再試行してください。')
                    self._condition.wait(remaining)
            except BaseException:
                self._waiting.remove(ticket)
                self._condition.notify_all()
                raise
            self._waiting.popleft()
            self._active += 1
            self._condition.notify_all()
        try:
            yield
        finally:
            with self._condition:
                self._active -= 1
                self._condition.notify_all()

"""Client-side rate limiting so the bot stays inside the per-account budget.

The API counts GET/HEAD as reads and everything else as writes, per account, per
minute. A sliding 60-second window mirrors that closely. When the server still
answers 429, :meth:`SlidingWindowLimiter.pause` blocks every caller until the
``Retry-After`` time, because the budget is shared by all of the account's keys.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable, Deque


class SlidingWindowLimiter:
    def __init__(
        self,
        limit: int,
        window: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        self.limit = limit
        self.window = window
        self._clock = clock
        self._sleep = sleep
        self._stamps: Deque[float] = deque()
        self._paused_until = 0.0
        self._lock = threading.Lock()

    def _wait_time(self, now: float) -> float:
        while self._stamps and now - self._stamps[0] >= self.window:
            self._stamps.popleft()
        wait = max(0.0, self._paused_until - now)
        if len(self._stamps) >= self.limit:
            wait = max(wait, self._stamps[0] + self.window - now)
        return wait

    def acquire(self) -> float:
        """Block until a request may be sent. Returns the seconds spent waiting."""
        waited = 0.0
        while True:
            with self._lock:
                now = self._clock()
                wait = self._wait_time(now)
                if wait <= 0:
                    self._stamps.append(now)
                    return waited
            self._sleep(wait)
            waited += wait

    def pause(self, seconds: float) -> None:
        """Hold every caller for ``seconds`` (used after the server returns 429)."""
        with self._lock:
            self._paused_until = max(self._paused_until, self._clock() + max(0.0, seconds))

    @property
    def used(self) -> int:
        """Requests sent inside the current window."""
        with self._lock:
            self._wait_time(self._clock())
            return len(self._stamps)

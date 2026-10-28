"""Injectable clock for repeatable timing tests and replay."""
import asyncio
import heapq
import itertools
import math
import time


class RealClock:
    def now(self):
        return time.time()

    def monotonic(self):
        return time.monotonic()

    async def sleep(self, seconds):
        await asyncio.sleep(seconds)


class ManualClock:
    def __init__(self, start=0.0):
        self.time = start
        self._sleepers = []
        self._sequence = itertools.count()

    def now(self):
        return self.time

    def monotonic(self):
        return self.time

    def next_deadline(self):
        self._sleepers = [row for row in self._sleepers if not row[2].done()]
        heapq.heapify(self._sleepers)
        return self._sleepers[0][0] if self._sleepers else None

    async def sleep(self, seconds):
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        future = asyncio.get_running_loop().create_future()
        heapq.heappush(self._sleepers, (self.time + seconds, next(self._sequence), future))
        try:
            await future
        finally:
            self._sleepers = [row for row in self._sleepers if row[2] is not future]
            heapq.heapify(self._sleepers)

    def advance(self, seconds):
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("manual time cannot go backwards")
        self.time += seconds
        while self._sleepers and self._sleepers[0][0] <= self.time:
            _, _, future = heapq.heappop(self._sleepers)
            if not future.done():
                future.set_result(None)

"""Injectable clock for repeatable timing tests and replay."""
import asyncio
import heapq
import itertools
import time


class RealClock:
    def now(self):
        return time.time()

    async def sleep(self, seconds):
        await asyncio.sleep(seconds)


class ManualClock:
    def __init__(self, start=0.0):
        self.time = start
        self._sleepers = []
        self._sequence = itertools.count()

    def now(self):
        return self.time

    async def sleep(self, seconds):
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        future = asyncio.get_running_loop().create_future()
        heapq.heappush(self._sleepers, (self.time + seconds, next(self._sequence), future))
        await future

    def advance(self, seconds):
        if seconds < 0:
            raise ValueError("manual time cannot go backwards")
        self.time += seconds
        while self._sleepers and self._sleepers[0][0] <= self.time:
            _, _, future = heapq.heappop(self._sleepers)
            if not future.done():
                future.set_result(None)

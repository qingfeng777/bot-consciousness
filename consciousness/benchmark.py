"""Reproducible real-clock stop-request benchmark with a persistent journal.

This measures the rule-to-driver request path, not physical stopping or models.
"""
import argparse
import asyncio
import json
import math
import hashlib
import os
from pathlib import Path
import platform
import resource
import sqlite3
import tempfile
import time

from . import __version__
from .domain import Signal, positive_number
from .driver import SimulatedDriver
from .journal import Journal
from .runtime import Runtime


def percentile(values, q):
    return sorted(values)[max(0, math.ceil(len(values) * q) - 1)]


class MeasuredDriver(SimulatedDriver):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.started = asyncio.Event()
        self.latencies = []
        self.ingress_latencies = []
        self.runtime = None
        self.ingress_at = None

    async def execute(self, action):
        self.duration = 30.0 if action.capability == "say" else 0.0
        self.started.set()
        return await super().execute(action)

    async def stop(self, action):
        now = time.monotonic()
        self.latencies.append(1000 * (now - self.runtime._interrupt_received_at))
        self.ingress_latencies.append(1000 * (now - self.ingress_at))
        return await super().stop(action)


async def benchmark(trials=100, rate=20.0):
    if type(trials) is not int or not 1 <= trials <= 10000:
        raise ValueError("trials must be between 1 and 10000")
    positive_number(rate, "rate")
    with tempfile.TemporaryDirectory(prefix="consciousness-benchmark-") as directory:
        journal = Journal(str(Path(directory) / "journal.sqlite3"))
        driver = MeasuredDriver(path=str(Path(directory) / "simulator.sqlite3"))
        runtime = Runtime(journal, driver)
        driver.runtime = runtime
        begin = time.monotonic()
        arrivals = []
        try:
            await runtime.start()
            for trial in range(trials):
                for offset, text in ((0, "background"), (1, "火警")):
                    target = begin + (2 * trial + offset) / rate
                    await asyncio.sleep(max(0, target - time.monotonic()))
                    if offset == 0:
                        driver.started.clear()
                    else:
                        await asyncio.wait_for(driver.started.wait(), 2)
                        driver.ingress_at = time.monotonic()
                    arrivals.append(time.monotonic())
                    await runtime.submit(Signal(text))
                await runtime.drain()
            if len(driver.latencies) != trials:
                raise RuntimeError("not every emergency produced a measured stop request")
            stats = {"count": trials, "p50_ms": percentile(driver.latencies, 0.5),
                     "p95_ms": percentile(driver.latencies, 0.95),
                     "p99_ms": percentile(driver.latencies, 0.99), "max_ms": max(driver.latencies)}
            digest = hashlib.sha256()
            for source in sorted(Path(__file__).parent.glob("*.py")):
                digest.update(source.name.encode())
                digest.update(source.read_bytes())
            return {"version": __version__, "simulation": True, "python": platform.python_version(),
                    "platform": platform.platform(), "machine": platform.machine(),
                    "logical_cpus": os.cpu_count(), "source_sha256": digest.hexdigest(),
                    "sqlite": sqlite3.sqlite_version, "storage": "temporary SQLite WAL, synchronous FULL",
                    "trials": trials, "inputs": len(arrivals), "target_inputs_per_second": rate,
                    "observed_inputs_per_second": (len(arrivals) - 1) / (arrivals[-1] - arrivals[0]),
                    "stop_request_latency": stats,
                    "ingress_to_stop_p95_ms": percentile(driver.ingress_latencies, 0.95),
                    "max_external_concurrency": driver.max_active,
                    "elapsed_seconds": time.monotonic() - begin,
                    "process_maxrss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                    "acceptance": {"stop_request_p95_at_most_100ms": stats["p95_ms"] <= 100,
                                   "external_concurrency_at_most_one": driver.max_active <= 1},
                    "limitations": ["simulated stop only", "alternating background/emergency text",
                                    "not a 72-hour soak test", "not a hardware or model benchmark"]}
        finally:
            await runtime.close()
            driver.close()
            journal.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=int, default=100)
    parser.add_argument("--rate", type=float, default=20)
    parser.add_argument("--output")
    args = parser.parse_args()
    report = asyncio.run(benchmark(args.trials, args.rate))
    result = json.dumps(report, indent=2, ensure_ascii=False)
    if args.output:
        Path(args.output).write_text(result + "\n")
    print(result)
    return 0 if all(report["acceptance"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Actual abrupt process termination between device effect and journal result."""
import asyncio
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from consciousness.driver import SimulatedDriver
from consciousness.journal import Journal
from consciousness.runtime import Runtime


CRASH_PROGRAM = '''
import asyncio, os, sys
from consciousness.attention import Attention
from consciousness.domain import Signal
from consciousness.driver import SimulatedDriver
from consciousness.journal import Journal
journal = Journal(sys.argv[1])
driver = SimulatedDriver(path=sys.argv[2], duration=0)
signal = Signal("crash-window", event_id="durable-input")
journal.accept(signal, Attention().select(signal), 64)
action = journal.claim("crashed-process")
journal.phase(action.id, "sending")
asyncio.run(driver.execute(action))
os._exit(73)
'''


class ProcessRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_abrupt_exit_releases_lock_and_does_not_repeat_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            jp = str(Path(directory) / "journal.sqlite3")
            dp = str(Path(directory) / "driver.sqlite3")
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-c", CRASH_PROGRAM, jp, dp,
                cwd=Path(__file__).resolve().parents[1],
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
            self.assertEqual(process.returncode, 73, stderr.decode())
            journal = Journal(jp)
            driver = SimulatedDriver(path=dp, duration=0)
            runtime = Runtime(journal, driver)
            try:
                await runtime.start()
                summary = await runtime.drain()
                self.assertEqual(summary["counts"]["completed"], 1)
                self.assertEqual(driver.execution_order, [])
            finally:
                await runtime.close()
                driver.close()
                journal.close()

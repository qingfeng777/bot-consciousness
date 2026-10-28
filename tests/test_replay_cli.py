import asyncio
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from consciousness.domain import InvalidInput
from consciousness.replay import load_scenario, replay


ROOT = Path(__file__).resolve().parents[1]


class ReplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_batch_replay_is_reproducible(self):
        signals = load_scenario(ROOT / "examples/basic.jsonl")
        first = await replay(signals)
        second = await replay(signals)
        self.assertEqual(first, second)
        self.assertEqual([row["input_id"] for row in first["outcomes"]],
                         ["emergency-1", "named-1", "greeting-1"])
        self.assertEqual(first["max_active"], 1)

    async def test_replay_rejects_unknown_fields_before_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "bad.jsonl"
            file.write_text('{"id":"x","text":"hi","shell":"ignored?"}\n')
            with self.assertRaises(InvalidInput):
                load_scenario(file)


class CliTests(unittest.TestCase):
    def invoke(self, *args):
        return subprocess.run([sys.executable, "-m", "consciousness", *args],
                              cwd=ROOT, capture_output=True, text=True, timeout=10)

    def test_persistent_cli_run_resume_and_inspect(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.invoke("run", "hello", "--data-dir", directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["counts"]["completed"], 1)
            resumed = self.invoke("resume", "--data-dir", directory)
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            self.assertEqual(json.loads(resumed.stdout)["counts"]["completed"], 1)
            inspected = self.invoke("inspect", "--data-dir", directory)
            self.assertEqual(inspected.returncode, 0, inspected.stderr)
            events = [json.loads(line) for line in inspected.stdout.splitlines()]
            self.assertEqual(sum(row.get("kind") == "action.sending" for row in events), 1)

    def test_cli_rejects_invalid_input(self):
        result = self.invoke("run", " ")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stderr)["error"], "INVALID_INPUT")

    def test_cli_replay(self):
        result = self.invoke("replay", "examples/basic.jsonl")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["counts"]["completed"], 3)

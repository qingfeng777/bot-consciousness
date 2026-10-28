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

    def test_timed_cli_and_backup_restore(self):
        timed = self.invoke("replay", "examples/preemption.jsonl", "--timed", "--stop-delay", "0.1")
        self.assertEqual(timed.returncode, 0, timed.stderr)
        self.assertEqual(json.loads(timed.stdout)["counts"]["cancelled"], 1)
        with tempfile.TemporaryDirectory() as directory:
            data = str(Path(directory) / "data")
            backup = str(Path(directory) / "backup")
            result = self.invoke("run", "hello", "--data-dir", data, "--robot-id", "robot-test")
            self.assertEqual(result.returncode, 0, result.stderr)
            result = self.invoke("backup", "--data-dir", data, backup)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((Path(backup) / "manifest.json").exists())
            self.assertFalse((Path(backup) / "INCOMPLETE").exists())
            duplicate = self.invoke("backup", "--data-dir", data, backup)
            self.assertEqual(duplicate.returncode, 2)
            resumed = self.invoke("resume", "--data-dir", backup)
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            summary = json.loads(resumed.stdout)
            self.assertEqual(summary["counts"]["completed"], 1)
            self.assertEqual(summary["storage"]["robot_id"], "robot-test")
            (Path(backup) / "INCOMPLETE").write_text("incomplete")
            blocked = self.invoke("resume", "--data-dir", backup)
            self.assertEqual(blocked.returncode, 2)

    def test_inspection_does_not_create_missing_data_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing"
            result = self.invoke("inspect", "--data-dir", str(missing))
            self.assertEqual(result.returncode, 2)
            self.assertFalse(missing.exists())

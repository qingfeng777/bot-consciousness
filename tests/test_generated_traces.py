"""Seeded state-machine exploration; not a claim of formal verification."""
import random
import unittest

from consciousness.attention import Attention
from consciousness.domain import Backpressure, Signal, TERMINAL
from consciousness.journal import Journal


class GeneratedTraceTests(unittest.TestCase):
    def test_1000_traces_with_10000_operations(self):
        attention = Attention()
        for seed in range(1000):
            with self.subTest(seed=seed):
                rng = random.Random(seed)
                journal = Journal()
                signals = []
                terminal = {}
                try:
                    for step in range(10):
                        operation = rng.randrange(6)
                        if operation <= 1 or not signals:
                            signal = Signal(rng.choice(["hello", "小智", "火警"]), event_id=f"{seed}:{step}")
                            try:
                                journal.accept(signal, attention.select(signal), 3)
                                signals.append(signal)
                            except Backpressure:
                                pass
                        elif operation == 2:
                            signal = rng.choice(signals)
                            _, accepted = journal.accept(signal, attention.select(signal), 3)
                            self.assertFalse(accepted)
                        elif operation == 3:
                            journal.claim("epoch")
                        else:
                            active = journal.unresolved()
                            if active:
                                action, _ = active[0]
                                state = journal.task(action.task_id)["status"]
                                if operation == 4 and state == "running":
                                    journal.transition(action.task_id, "waiting_stop", "generated_interrupt")
                                elif operation == 4 and state == "waiting_stop":
                                    journal.transition(action.task_id, "outcome_unknown", "generated_timeout")
                                else:
                                    journal.finish(action, rng.choice(sorted(TERMINAL)), "sim:generated_evidence")
                        tasks = journal.tasks()
                        self.assertLessEqual(sum(t["status"] in {"running", "waiting_stop", "outcome_unknown"}
                                                 for t in tasks), 1)
                        self.assertLessEqual(sum(t["status"] == "queued" for t in tasks), 3)
                        self.assertEqual(len({t["event_id"] for t in tasks}), len(tasks))
                        for task in tasks:
                            if task["id"] in terminal:
                                self.assertEqual(task["status"], terminal[task["id"]])
                            if task["status"] in TERMINAL:
                                terminal[task["id"]] = task["status"]
                        active = journal.unresolved()
                        if active:
                            self.assertIsNone(journal.claim("competing-epoch"))
                finally:
                    journal.close()

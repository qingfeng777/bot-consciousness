import unittest

from consciousness.attention import Attention
from consciousness.domain import ContextOverflow, InvalidInput, Signal
from consciousness.memory import Item, WorkingMemory


class MemoryAttentionTests(unittest.TestCase):
    def test_pinned_constraints_survive_eviction(self):
        memory = WorkingMemory(max_items=2)
        memory.add(Item("constraint", "constraint", "one external action", True))
        for n in range(100):
            memory.add(Item(str(n), "observation", str(n)))
        snapshot = memory.snapshot()
        self.assertEqual([i.source_id for i in snapshot.items], ["constraint", "99"])
        memory.add(Item("100", "observation", "later"))
        self.assertEqual(snapshot.items[-1].source_id, "99")

    def test_hard_overflow_is_atomic(self):
        memory = WorkingMemory(max_items=1)
        memory.add(Item("constraint", "constraint", "first", True))
        original = memory.snapshot()
        with self.assertRaises(ContextOverflow):
            memory.add(Item("constraint2", "constraint", "second", True))
        self.assertEqual(memory.snapshot(), original)

    def test_byte_budget_and_oversized_ordinary_input(self):
        memory = WorkingMemory(max_bytes=150)
        memory.add(Item("a", "observation", "大" * 1000))
        self.assertEqual(memory.snapshot().items, ())
        with self.assertRaises(ContextOverflow):
            memory.add(Item("b", "constraint", "大" * 1000, True))

    def test_exact_emergency_and_named_attention(self):
        attention = Attention()
        self.assertEqual(attention.select(Signal("火警")).priority, 0)
        self.assertEqual(attention.select(Signal("这里没有火警")).priority, 2)
        self.assertGreater(attention.select(Signal("小智你好")).salience,
                           attention.select(Signal("hello")).salience)

    def test_input_contract_and_self_feedback_rejection(self):
        for signal in [Signal(" "), Signal("x", schema_version="2.0"),
                       Signal("x", source="output"), Signal("大" * 6000),
                       Signal("x", event_id="")]:
            with self.assertRaises(InvalidInput):
                Attention().select(signal)

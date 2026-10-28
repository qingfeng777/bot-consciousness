"""Bounded immutable snapshots. Capacity overflow never truncates hard constraints."""
from dataclasses import dataclass
from .domain import ContextOverflow, canonical


@dataclass(frozen=True)
class Item:
    source_id: str
    kind: str
    content: str
    pinned: bool = False


@dataclass(frozen=True)
class Snapshot:
    revision: int
    items: tuple
    byte_count: int


class WorkingMemory:
    def __init__(self, max_items=32, max_bytes=32768):
        if max_items < 1 or max_bytes < 1:
            raise ValueError("memory budgets must be positive")
        self.max_items, self.max_bytes = max_items, max_bytes
        self._items = []
        self.revision = 0

    @staticmethod
    def _size(items):
        return sum(len(canonical(item.__dict__).encode("utf-8")) for item in items)

    def add(self, item: Item):
        items = [i for i in self._items if i.source_id != item.source_id] + [item]
        pinned = [i for i in items if i.pinned]
        if len(pinned) > self.max_items or self._size(pinned) > self.max_bytes:
            raise ContextOverflow("hard constraints exceed working memory budget")
        while len(items) > self.max_items or self._size(items) > self.max_bytes:
            items.remove(next(i for i in items if not i.pinned))
        if items != self._items:
            self._items = items
            self.revision += 1

    def snapshot(self):
        return Snapshot(self.revision, tuple(self._items), self._size(self._items))

"""Transactional versioned module state for bounded local simulations.

State and its audit event share the runtime journal transaction and backup.
This JSON storage baseline deliberately favors inspectability over scale.
"""
from contextlib import contextmanager
from copy import deepcopy
import json
import math

from .domain import InvalidInput, RevisionConflict, canonical


def identifier(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise InvalidInput("expected a nonempty identifier of at most 256 characters")
    return value


def number(value, minimum=0, maximum=float("inf")):
    if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise InvalidInput("number outside finite permitted range")
    return value


class StateStore:
    def __init__(self, journal):
        self.journal = journal
        with journal.transaction():
            journal._db.execute("CREATE TABLE IF NOT EXISTS module_state ("
                                "name TEXT PRIMARY KEY, revision INTEGER NOT NULL, "
                                "schema_version INTEGER NOT NULL, body TEXT NOT NULL)")

    def read(self, name):
        row = self.journal._db.execute("SELECT * FROM module_state WHERE name=?", (name,)).fetchone()
        if row is None:
            return {}, 0
        if row["schema_version"] != 1:
            raise InvalidInput("unsupported module state schema")
        return json.loads(row["body"]), row["revision"]

    @contextmanager
    def edit(self, name, cause, expected=None):
        identifier(name)
        identifier(cause)
        with self.journal.transaction():
            state, revision = self.read(name)
            if expected is not None and expected != revision:
                raise RevisionConflict("module revision changed")
            before = deepcopy(state)
            yield state
            if state != before:
                body = canonical(state)
                if len(body.encode()) > 8 * 1024 * 1024:
                    raise InvalidInput("module state exceeds 8 MiB simulation budget")
                self.journal._db.execute("INSERT INTO module_state VALUES (?,?,1,?) "
                                        "ON CONFLICT(name) DO UPDATE SET revision=excluded.revision,body=excluded.body",
                                        (name, revision + 1, body))
                self.journal._event("module.updated", {"module": name, "revision": revision + 1},
                                    "cognition", cause_id=cause)

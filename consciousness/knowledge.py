"""M05/M18: sourced temporal claims, explicit disputes and withdrawal closure."""
from copy import deepcopy
import math
import sqlite3

from .domain import DuplicateConflict, InvalidInput
from .state import identifier, number


class Knowledge:
    def __init__(self, store):
        self.store = store

    def evidence(self, eid, source, text, *, group=None):
        for value in (eid, source):
            identifier(value)
        if not isinstance(text, str) or not text or len(text) > 16384:
            raise InvalidInput("invalid evidence text")
        record = dict(id=eid, source=source, text=text, group=identifier(group or eid), active=True)
        with self.store.edit("knowledge", eid) as state:
            records = state.setdefault("evidence", {})
            if eid in records:
                if {**records[eid], "active": True} != record:
                    raise DuplicateConflict("evidence ID reused")
                return deepcopy(records[eid])
            records[eid] = record
        return record

    @staticmethod
    def _refresh(state):
        claims = state.setdefault("claims", {})
        # JSON persistence sorts keys; propagation must not depend on key order.
        for claim in claims.values():
            supported = all(state["evidence"][eid]["active"] for eid in claim["evidence"])
            claim["status"] = ("withdrawn" if claim.get("withdrawn") else
                               "active" if supported else "needs_verification")
        conflicts = state.setdefault("conflicts", {})
        # Only invalidate in this fixed point, so cycles of evidence processing
        # cannot oscillate. A fresh refresh can restore newly supported facts.
        for _ in range(len(claims) + 1):
            for case in conflicts.values():
                decision = case.get("decision")
                valid = decision and all(state["evidence"][e]["active"] for e in decision["evidence"])
                valid = valid and claims[decision["winner"]]["status"] == "active"
                case["status"] = "resolved" if valid else "reopened" if decision else "detected"
            changed = False
            for claim in claims.values():
                invalid = any(claims[p]["status"] != "active" for p in claim["parents"])
                for parent in claim["parents"]:
                    for case in conflicts.values():
                        if parent not in case["claims"] or not all(claims[c]["status"] == "active" for c in case["claims"]):
                            continue
                        related = [claim] + [claims[c] for c in case["claims"]]
                        overlap = max(c["start"] for c in related) < min(
                            c["end"] if c["end"] is not None else float("inf") for c in related)
                        invalid |= overlap and (case["status"] != "resolved" or case["decision"]["winner"] != parent)
                if claim["status"] == "active" and invalid:
                    claim["status"] = "needs_verification"
                    changed = True
            if not changed:
                break

    def claim(self, cid, entity, attribute, value, evidence, *, start=0, end=None,
              unit="", context="global", parents=()):
        for item in (cid, entity, attribute, context):
            identifier(item)
        number(start)
        if end is not None and number(end) <= start:
            raise InvalidInput("valid interval must be nonempty")
        if not isinstance(value, (str, int, float, bool)) or isinstance(value, float) and not math.isfinite(value):
            raise InvalidInput("claim value must be a finite scalar")
        if unit in {"cm", "m"}:
            number(value)
            value = value / 100 if unit == "cm" else value
            unit = "m"
        elif unit not in {"", "%", "C"}:
            raise InvalidInput("unsupported unit")
        record = dict(id=cid, entity=entity, attribute=attribute, value=value, unit=unit,
                      context=context, start=start, end=end, evidence=sorted(set(evidence)),
                      parents=sorted(set(parents)), status="active", withdrawn=False)
        with self.store.edit("knowledge", cid) as state:
            claims = state.setdefault("claims", {})
            if cid in claims:
                original = {k: v for k, v in claims[cid].items() if k not in {"status", "withdrawn"}}
                if original != {k: v for k, v in record.items() if k not in {"status", "withdrawn"}}:
                    raise DuplicateConflict("claim ID reused")
                return deepcopy(claims[cid])
            if not record["evidence"] or any(not state.get("evidence", {}).get(e, {}).get("active") for e in record["evidence"]):
                raise InvalidInput("claim requires active registered evidence")
            if any(p not in claims or claims[p]["status"] != "active" for p in record["parents"]):
                raise InvalidInput("derived claim requires existing active premises")
            if any(start < claims[p]["start"] or claims[p]["end"] is not None and
                   (end is None or end > claims[p]["end"]) for p in record["parents"]):
                raise InvalidInput("derived validity cannot exceed a premise interval")
            cases = state.setdefault("conflicts", {})
            for old in claims.values():
                same = all(old[k] == record[k] for k in ("entity", "attribute", "unit", "context"))
                overlap = max(start, old["start"]) < min(end if end is not None else float("inf"),
                                                        old["end"] if old["end"] is not None else float("inf"))
                if same and overlap and old["value"] != value and not old["withdrawn"]:
                    key = str(len(cases) + 1)
                    cases[key] = dict(id=key, claims=[old["id"], cid], status="detected", history=[])
            claims[cid] = record
            self._refresh(state)
        return deepcopy(record)

    def resolve(self, case_id, winner, evidence, reason):
        identifier(reason)
        with self.store.edit("knowledge", "resolve:" + case_id) as state:
            case = state["conflicts"][case_id]
            if winner not in case["claims"] or not evidence:
                raise InvalidInput("resolution requires a member claim and evidence")
            if any(not state["evidence"].get(e, {}).get("active") for e in evidence):
                raise InvalidInput("resolution evidence unavailable")
            if state["claims"][winner]["status"] not in {"active", "superseded"}:
                raise InvalidInput("winner is not supported")
            decision = dict(winner=winner, evidence=sorted(set(evidence)), reason=reason)
            case["decision"] = decision
            case["history"].append(deepcopy(decision))
            self._refresh(state)

    def withdraw(self, evidence_id):
        with self.store.edit("knowledge", "withdraw:" + evidence_id) as state:
            state["evidence"][evidence_id]["active"] = False
            self._refresh(state)

    def retract(self, claim_id):
        with self.store.edit("knowledge", "retract:" + claim_id) as state:
            state["claims"][claim_id]["withdrawn"] = True
            self._refresh(state)

    def search(self, *, entity=None, attribute=None, at=0, context="global", query="", limit=10, claim_ids=None):
        number(at)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise InvalidInput("retrieval limit must be 1..100")
        state, revision = self.store.read("knowledge")
        result = []
        for claim in state.get("claims", {}).values():
            if claim_ids is not None and claim["id"] not in claim_ids:
                continue
            if claim["status"] != "active" or claim["context"] != context:
                continue
            if entity is not None and claim["entity"] != entity or attribute is not None and claim["attribute"] != attribute:
                continue
            if not claim["start"] <= at or claim["end"] is not None and at >= claim["end"]:
                continue
            if query.casefold() not in str(claim["value"]).casefold():
                continue
            relevant = [c for c in state.get("conflicts", {}).values()
                        if claim["id"] in c["claims"] and all(
                            state["claims"][cid]["status"] == "active" and state["claims"][cid]["start"] <= at
                            and (state["claims"][cid]["end"] is None or at < state["claims"][cid]["end"])
                            for cid in c["claims"])]
            if any(c["status"] == "resolved" and c["decision"]["winner"] != claim["id"] for c in relevant):
                continue
            cases = [c["id"] for c in relevant if c["status"] != "resolved"]
            result.append({**claim, "revision": revision, "conflicts": cases,
                           "certain": not cases, "stale_index": False, "evidence_missing": False})
        return sorted(result, key=lambda item: item["id"])[:limit]

    def conflicts(self):
        return list(self.store.read("knowledge")[0].get("conflicts", {}).values())

    def rebuild_index(self):
        """Optional SQLite FTS5 index; source claims remain authoritative."""
        state, revision = self.store.read("knowledge")
        claims = state.get("claims", {})
        if len(claims) > 1000:
            raise InvalidInput("local full-text index baseline supports at most 1000 claims")
        try:
            with self.store.edit("retrieval_index", "rebuild") as index:
                db = self.store.journal._db
                db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(memory_id UNINDEXED, body)")
                db.execute("DELETE FROM memory_fts")
                for claim in claims.values():
                    if claim["status"] == "active":
                        text = " ".join(str(claim[k]) for k in ("entity", "attribute", "value"))
                        db.execute("INSERT INTO memory_fts VALUES (?,?)", (claim["id"], text))
                index.update(knowledge_revision=revision, claims=len(claims))
        except sqlite3.OperationalError as exc:
            raise InvalidInput("FTS5 unavailable or index rebuild failed; structured search remains available") from exc
        return revision

    def ranked(self, query, *, at=0, entity=None, attribute=None, context="global", limit=10):
        if not isinstance(query, str) or not query.strip() or len(query) > 1024:
            raise InvalidInput("query must contain 1..1024 characters")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise InvalidInput("retrieval limit must be 1..100")
        state, revision = self.store.read("knowledge")
        indexed, _ = self.store.read("retrieval_index")
        stale = indexed.get("knowledge_revision") != revision
        if stale:
            return [{**h, "stale_index": True, "retrieval": "substring_fallback", "score": None}
                    for h in self.search(entity=entity, attribute=attribute, at=at, context=context, query=query, limit=limit)]
        expression = " AND ".join('"' + part.replace('"', '""') + '"' for part in query.split())
        try:
            rows = self.store.journal._db.execute(
                "SELECT memory_id,bm25(memory_fts) AS score FROM memory_fts WHERE memory_fts MATCH ? "
                "ORDER BY score,memory_id LIMIT 1000", (expression,)).fetchall()
        except sqlite3.OperationalError:
            return [{**h, "stale_index": True, "retrieval": "substring_fallback", "score": None}
                    for h in self.search(entity=entity, attribute=attribute, at=at, context=context, query=query, limit=limit)]
        # Apply authoritative temporal/conflict filtering before returning hits.
        result = []
        for row in rows:
            claim = state.get("claims", {}).get(row["memory_id"])
            if claim is None or entity is not None and claim["entity"] != entity or attribute is not None and claim["attribute"] != attribute:
                continue
            valid = self.search(at=at, context=context, limit=1, claim_ids={row["memory_id"]})
            hit = next((h for h in valid if h["id"] == row["memory_id"]), None)
            if hit:
                result.append({**hit, "retrieval": "fts5_bm25", "score": row["score"]})
                if len(result) >= limit:
                    break
        return result

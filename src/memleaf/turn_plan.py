"""Immutable, checksummed write plans and identity for bounded forward recovery.

The journal is written before knowledge/history changes. It is not an all-Vault
transaction: interrupted operations are resumed under the Vault lock. Checksums
catch damaged journals, not hostile edits made by an actor with Vault access.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Iterable, Mapping

from .validation import ModelOutputError, parse_strict_json

SCHEMA_VERSION = 1
MAX_PLAN_BYTES = 8 * 1024 * 1024


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _error(message: str) -> ModelOutputError:
    return ModelOutputError(message, validation_reason="schema_violation", validation_detail="invalid_evidence")


def content_digest(summary: Mapping[str, Any]) -> str:
    """Compare committed semantic state, excluding generated time and provenance."""
    fields = {key: summary.get(key) for key in ("body", "type", "scopes", "status", "completed_at", "due_date")}
    fields["scopes"] = sorted(fields["scopes"] or ["global"])
    if fields["type"] == "todo":
        fields["status"] = fields["status"] or "active"
    else:
        fields["status"] = None
    # A completion timestamp can be assigned by the writer. Operation identity
    # retains the complete frozen payload separately from this replay comparison.
    if fields["status"] == "completed":
        fields["completed_at"] = None
    return _digest(fields)


def dedup_digest(summary: Mapping[str, Any]) -> str:
    """Exact state plus title: different named tasks with one body stay distinct.

    Never strip negation, punctuation inside identifiers, dates, or numbers.
    Provenance and search metadata do not justify another active memory.
    """
    return _digest([str(summary.get("title", "")).strip(), content_digest(summary)])


def revision_digest(memory: Any) -> str:
    value = memory.to_dict() if hasattr(memory, "to_dict") else dict(memory)
    # Retrieval hit counters are not an authored revision and may change while
    # a model call is in progress. All other persisted fields are protected.
    return _digest({k: v for k, v in value.items() if k not in {"hit_count", "last_hit_at"}})


def turn_identity_key(source: str, session_id: str, turn_key: str) -> str:
    return "turn-" + _digest([source, session_id, turn_key])


def turn_plan_key(turn: Any) -> str:
    return turn_identity_key(turn.source, turn.session_id, turn.turn_key)


def input_digest(turn: Any) -> str:
    # Explicit remember may recreate an event with a fresh timestamp on retry.
    # Its content, role, provenance and identity must remain identical. The plan
    # already freezes any dates resolved from the original evidence timestamp.
    events = [{"event_key": e.event_key, "role": e.role, "content": e.content,
               "tool_evidence": list(getattr(e, "tool_evidence", ()))} for e in turn.events]
    return _digest([turn.source, turn.session_id, turn.turn_key, events])


@dataclass(frozen=True)
class CandidatePlan:
    operation_id: str
    payload: str

    def to_dict(self) -> dict[str, Any]:
        return json.loads(self.payload)


@dataclass(frozen=True)
class TurnPlan:
    candidates: tuple[CandidatePlan, ...]

    @classmethod
    def from_requests(cls, requests: Iterable[Mapping[str, Any]]) -> "TurnPlan":
        result: list[CandidatePlan] = []
        targets: set[tuple[str, str, str, str]] = set()
        for request in requests:
            # Automatic duplicate references never change the target. Explicit
            # remember duplicates have separate metadata-merge authorization.
            if request.get("duplicate_memory_id") and not request.get("explicit_remember"):
                continue
            summary = request["summary"]
            turn = request["turn"]
            correction = request.get("scope_correction", {})
            retirement = bool(correction.get("survivor_memory_id"))
            update = summary.get("update_memory_id")
            target = request.get("duplicate_memory_id") or update or request["memory_id"]
            evidence = tuple(request.get("evidence_unit_ids", ()))
            if not request.get("explicit_remember") and not evidence:
                raise _error("automatic write has no admitted evidence")
            identity = (turn.source, turn.session_id, turn.turn_key, target)
            mutation_target = (*identity[:3], correction["target_memory_id"] if retirement else target)
            if mutation_target in targets:
                raise ModelOutputError("multiple same-turn writes to one target", validation_detail="duplicate_update_target")
            targets.add(mutation_target)
            kind = "scope_retirement" if retirement else "metadata_merge" if request.get("duplicate_memory_id") else "memory_write"
            # No model-generated candidate ID is used as an idempotency key.
            op_id = "op-" + _digest([*identity, input_digest(turn), kind, summary, correction])
            value = {"schema_version": SCHEMA_VERSION, "operation_id": op_id,
                     "source": turn.source, "session_id": turn.session_id, "turn_key": turn.turn_key,
                     "candidate_id": request["candidate_id"], "memory_id": target,
                     "disposition": "UPDATE" if update or retirement or request.get("duplicate_memory_id") else "CREATE",
                     "kind": kind, "evidence_unit_ids": list(evidence),
                     "digest": content_digest(summary)}
            if retirement:
                value["scope_correction"] = dict(correction)
            result.append(CandidatePlan(op_id, _json(value)))
        return cls(tuple(result))


@dataclass(frozen=True)
class FrozenTurn:
    """JSON serialization prevents mutable dictionaries escaping a frozen plan."""
    payload: str
    checksum: str

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": SCHEMA_VERSION, "payload": self.payload, "checksum": self.checksum}

    @classmethod
    def build(cls, turn: Any, requests: Iterable[Mapping[str, Any]], *,
              scopes: Iterable[str] = (), candidates: Iterable[Mapping[str, Any]] = (),
              evidence: Iterable[Mapping[str, Any]] = (), deferred: Iterable[Mapping[str, Any]] = ()) -> "FrozenTurn":
        records = []
        for request in requests:
            if request.get("turn") != turn:
                raise _error("write plan contains another turn")
            records.append({k: v for k, v in request.items() if k != "turn"})
        value = {"schema_version": SCHEMA_VERSION, "turn_id": turn_plan_key(turn),
                 "input_digest": input_digest(turn), "requests": records, "scopes": list(scopes),
                 "candidate_dispositions": list(candidates), "evidence_dispositions": list(evidence),
                 "deferred_candidates": list(deferred)}
        payload = _json(value)
        if len(payload.encode()) > MAX_PLAN_BYTES:
            raise _error("write plan exceeds storage budget")
        return cls(payload, hashlib.sha256(payload.encode()).hexdigest())

    @classmethod
    def restore(cls, stored: Any, turn: Any) -> dict[str, Any]:
        if (not isinstance(stored, Mapping) or stored.get("schema_version") != SCHEMA_VERSION
            or not isinstance(stored.get("payload"), str) or not isinstance(stored.get("checksum"), str)):
            raise _error("invalid stored write plan")
        payload = stored["payload"]
        if len(payload.encode()) > MAX_PLAN_BYTES or hashlib.sha256(payload.encode()).hexdigest() != stored["checksum"]:
            raise _error("stored write plan checksum mismatch")
        value = parse_strict_json(payload)
        if (not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION
            or value.get("turn_id") != turn_plan_key(turn) or value.get("input_digest") != input_digest(turn)):
            raise _error("stored write plan does not match current evidence")
        for key in ("requests", "scopes", "candidate_dispositions", "evidence_dispositions", "deferred_candidates"):
            if not isinstance(value.get(key), list):
                raise _error("invalid stored plan field")
        if any(not isinstance(s, str) or not s for s in value["scopes"]):
            raise _error("invalid stored plan scopes")
        for request in value["requests"]:
            if not isinstance(request, dict) or "turn" in request or not isinstance(request.get("summary"), dict):
                raise _error("invalid stored write request")
            for key in ("candidate_id", "memory_id"):
                ident = request.get(key)
                if (not isinstance(ident, str) or not ident or any(c in ident for c in "\x00\r\n")
                    or (key == "memory_id" and (any(c in ident for c in "/\\") or ident in {".", ".."}))):
                    raise _error("invalid stored write identity")
            request["turn"] = turn
        return value


def cancel_frozen_targets(stored: Mapping[str, Any], memory_ids: set[str]) -> tuple[dict[str, Any], set[str]]:
    """Revoke affected pending writes, preserving independently planned siblings.

    Called only under the Vault lock, before an explicit forget deletes files.
    Revocation removes frozen plaintext as well as operation intents. It is tied
    to this input, not a permanent ban on remembering the subject again.
    """
    if not isinstance(stored, Mapping) or stored.get("schema_version") != SCHEMA_VERSION:
        raise _error("invalid stored write plan during forget")
    payload, checksum = stored.get("payload"), stored.get("checksum")
    if (not isinstance(payload, str) or len(payload.encode()) > MAX_PLAN_BYTES
        or hashlib.sha256(payload.encode()).hexdigest() != checksum):
        raise _error("stored write plan checksum mismatch during forget")
    value = parse_strict_json(payload)
    if not isinstance(value, dict) or not isinstance(value.get("requests"), list):
        raise _error("invalid pending requests during forget")
    kept, removed = [], set()
    for request in value["requests"]:
        if not isinstance(request, dict) or not isinstance(request.get("summary"), dict):
            raise _error("invalid pending request during forget")
        correction = request.get("scope_correction") or {}
        references = {request.get("memory_id"), request.get("duplicate_memory_id"),
            request["summary"].get("update_memory_id"), correction.get("target_memory_id"),
            correction.get("survivor_memory_id")}
        if references.intersection(memory_ids):
            removed.add(request["candidate_id"])
        else:
            kept.append(request)
    if not removed:
        return dict(stored), set()
    value["requests"] = kept
    value["candidate_dispositions"] = [row for row in value.get("candidate_dispositions", [])
        if row.get("candidate_id") not in removed]
    value["candidate_dispositions"].extend(
        {"candidate_id": cid, "disposition": "NO_CHANGE", "reason": "explicit_forget"}
        for cid in sorted(removed))
    value["deferred_candidates"] = [row for row in value.get("deferred_candidates", [])
        if row.get("candidate_id") not in removed]
    for row in value.get("evidence_dispositions", []):
        ids = row.get("candidate_ids")
        if isinstance(ids, list) and removed.intersection(ids):
            remaining = [cid for cid in ids if cid not in removed]
            if remaining:
                row["candidate_ids"] = remaining
            else:
                row.pop("candidate_ids", None)
                row["decision"] = "NO_CHANGE"
                row["reason"] = "explicit_forget"
    result = _json(value)
    return FrozenTurn(result, hashlib.sha256(result.encode()).hexdigest()).to_dict(), removed

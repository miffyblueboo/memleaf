"""Immutable write intent and content identity for auditable forward recovery."""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import json
from typing import Any, Iterable, Mapping
from .validation import ModelOutputError


def content_digest(summary: Mapping[str, Any]) -> str:
    fields = {key: summary.get(key) for key in
              ("body", "type", "scopes", "status", "completed_at", "due_date")}
    # Writer supplies the default active status for todos.
    if fields["type"] == "todo" and fields["status"] is None:
        fields["status"] = "active"
    if fields["type"] != "todo":
        fields["status"] = None
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class CandidatePlan:
    operation_id: str
    source: str
    session_id: str
    turn_key: str
    candidate_id: str
    memory_id: str
    disposition: str
    evidence_unit_ids: tuple[str, ...]
    digest: str

    def to_dict(self) -> dict[str, Any]:
        return {**{key: getattr(self, key) for key in
                   ("operation_id", "source", "session_id", "turn_key", "candidate_id",
                    "memory_id", "disposition", "digest")},
                "evidence_unit_ids": list(self.evidence_unit_ids)}


@dataclass(frozen=True)
class TurnPlan:
    candidates: tuple[CandidatePlan, ...]

    @classmethod
    def from_requests(cls, requests: Iterable[Mapping[str, Any]]) -> 'TurnPlan':
        result = []
        targets = set()
        for request in requests:
            if request.get("duplicate_memory_id") or request.get("scope_correction", {}).get("survivor_memory_id"):
                continue
            summary = request["summary"]
            turn = request["turn"]
            update = summary.get("update_memory_id")
            target = update or request["memory_id"]
            evidence = tuple(request.get("evidence_unit_ids", ()))
            if not request.get("explicit_remember") and not evidence:
                raise ModelOutputError("automatic write has no admitted evidence", validation_detail="invalid_evidence")
            identity = (turn.source, turn.session_id, turn.turn_key, target)
            if identity in targets:
                raise ModelOutputError("multiple same-turn writes to one target", validation_detail="duplicate_update_target")
            targets.add(identity)
            digest = content_digest(summary)
            material = json.dumps([*identity, request["candidate_id"], digest], ensure_ascii=False, separators=(",", ":"))
            result.append(CandidatePlan("op-" + hashlib.sha256(material.encode()).hexdigest(),
                turn.source, turn.session_id, turn.turn_key, request["candidate_id"], target,
                "UPDATE" if update else "CREATE", evidence, digest))
        return cls(tuple(result))

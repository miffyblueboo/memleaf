"""Candidate outcomes and the local not-yet-committed overlay."""
from __future__ import annotations
from typing import Any, Iterable, Mapping, Optional
from .inbox import InboxTurn
from .process_common import _Snapshot, _UNSET


class TurnAudit:
    def __init__(self):
        self._planned_related = []
        self._deferred_by_turn = {}
        self._dispositions_by_turn = {}
        self._evidence_by_turn = {}

    def _record_disposition(
        self,
        turn_ref: tuple[str, str, str],
        candidate: Mapping[str, Any],
        disposition: str,
        *,
        reason: str | None = None,
        memory_id: str | None = None,
    ) -> None:
        """Record a compact, candidate-level processing outcome.

        The ledger deliberately keeps only the stable candidate id and the
        outcome metadata.  Candidate text belongs in the inbox/model
        diagnostics, not in the durable processing index.
        """

        candidate_id = candidate.get("candidate_id")
        if not isinstance(candidate_id, str) or not candidate_id:
            return
        if disposition not in {"CREATE", "UPDATE", "NO_CHANGE", "DEFERRED"}:
            return
        value: dict[str, Any] = {
            "candidate_id": candidate_id,
            "disposition": disposition,
        }
        if candidate.get("evidence_unit_ids"):
            value["evidence_unit_ids"] = list(candidate["evidence_unit_ids"])
        if isinstance(reason, str) and reason:
            value["reason"] = reason
        if isinstance(memory_id, str) and memory_id:
            value["memory_id"] = memory_id
        values = self._dispositions_by_turn.setdefault(turn_ref, [])
        candidate_key = candidate_id.casefold()
        for index, previous in enumerate(values):
            if (
                isinstance(previous, Mapping)
                and isinstance(previous.get("candidate_id"), str)
                and previous["candidate_id"].casefold() == candidate_key
            ):
                values[index] = value
                return
        values.append(value)


    def _record_request_disposition(
        self,
        request: Mapping[str, Any],
        disposition: str,
        *,
        reason: str | None = None,
        memory_id: str | None = None,
    ) -> None:
        turn = request.get("turn")
        if not isinstance(turn, InboxTurn):
            return
        for member in request.get("contributing_candidates") or [request]:
            self._record_disposition(
                (turn.source, turn.session_id, turn.turn_key), member, disposition,
                reason=reason, memory_id=memory_id,
            )


    def _candidate_dispositions(
        self,
        snapshots: Iterable[_Snapshot] = (),
    ) -> list[dict[str, Any]]:
        """Return compact outcomes in the same order as processed turns."""

        result: list[dict[str, Any]] = []
        for snapshot in snapshots:
            turn = snapshot.turn
            turn_ref = (turn.source, turn.session_id, turn.turn_key)
            result.extend(
                dict(item)
                for item in self._dispositions_by_turn.get(turn_ref, [])
                if isinstance(item, Mapping)
            )
        return result


    def _defer_candidate(
        self,
        turn_ref: tuple[str, str, str],
        candidate: Mapping[str, Any],
        reason: str,
        *,
        scopes: Optional[Iterable[str]] = None,
        scope_source: Any = _UNSET,
    ) -> None:
        self._record_disposition(
            turn_ref,
            candidate,
            "DEFERRED",
            reason=reason,
            memory_id=(
                candidate.get("update_memory_id")
                if isinstance(candidate.get("update_memory_id"), str)
                else candidate.get("duplicate_memory_id")
                if isinstance(candidate.get("duplicate_memory_id"), str)
                else None
            ),
        )
        self._deferred_by_turn[turn_ref].append(
            {
                "candidate_id": str(candidate["candidate_id"]),
                "scopes": list(scopes if scopes is not None else candidate["scopes"]),
                "scope_source": (
                    candidate.get("scope_source")
                    if scope_source is _UNSET
                    else scope_source
                ),
                "reason": reason,
            }
        )

"""Shared host lifecycle runtime.

Host adapters translate their native lifecycle events into this module.  Memory
semantics stay in :class:`memleaf.service.Memleaf`; this runtime only owns the
cross-host turn lifecycle: visible capture, retrieval-turn binding, bounded
search observation, completion gating, and process triggering.

Hermes reaches the same runtime through the MCP boundary, while hook-based
hosts such as Codex can call it in-process.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .locking import atomic_write_json, read_json
from .retrieval_gate import (
    MAX_GATE_RETRIES,
    RetrievalGateError,
    begin_turn,
    bind_turn_alias,
    consume_continuation,
    continuation_marker,
    find_pending_continuation,
    find_turn,
    mark_degraded,
    observe_search,
    request_gate_retry,
    validate_turn,
)
from .service import Memleaf


_GATE_RETRY_REASON = (
    "Before producing the final answer, call the memleaf search tool once for this "
    "turn. Use the current conversation and the Scope Map to choose the query; "
    "a no-match result is acceptable."
)
_GATE_ERROR_REASON = (
    "The memleaf search did not complete. Retry the memleaf search once before "
    "answering; do not treat this error as no match."
)


@dataclass(frozen=True)
class TurnOpenResult:
    retrieval_id: str
    continuation: bool
    injection_delivered: bool


@dataclass(frozen=True)
class ToolPreparation:
    allowed: bool
    retrieval_id: str | None
    arguments: dict[str, Any] | None
    reason: str | None = None


@dataclass(frozen=True)
class TurnCompletion:
    retry_required: bool = False
    retry_reason: str | None = None
    degraded: bool = False
    captured: bool = False
    process_failed: bool = False
    process_deferred: bool = False


class HostRuntime:
    """Host-neutral lifecycle operations over one Memleaf service."""

    def __init__(self, service: Memleaf, host: str) -> None:
        if not isinstance(host, str) or not host.strip():
            raise ValueError("host is required")
        self.service = service
        self.vault = service.vault
        self.host = host.strip()

    def capture(self, **arguments: Any) -> Any:
        """Forward one explicit visible capture through the shared runtime."""

        return self.service.capture(**arguments)

    def capture_visible(
        self,
        *,
        session_id: str,
        turn_id: str,
        role: str,
        content: str,
    ) -> tuple[bool, bool]:
        result = self.service.capture(
            self.host,
            session_id,
            turn_id,
            role,
            content,
            record=True,
            visible=True,
        )
        stored = getattr(result, "stored", False) is True
        duplicate = getattr(result, "duplicate", False) is True
        return stored or duplicate, stored

    def process(self, **arguments: Any) -> Any:
        """Run the existing Core process path without changing extraction rules."""

        return self.service.process(**arguments)

    def scope_catalog(self, **arguments: Any) -> Any:
        return self.service.scope_catalog(**arguments)

    def open_retrieval_turn(self, session_id: str, turn_id: str) -> str:
        retrieval_id = find_turn(self.vault, self.host, session_id, turn_id)
        if retrieval_id is not None:
            return retrieval_id
        return begin_turn(self.vault, self.host, session_id, turn_id)

    def open_turn(
        self,
        *,
        session_id: str,
        turn_id: str,
        user_content: str,
        allow_continuation: bool = True,
    ) -> TurnOpenResult:
        """Capture a visible user turn and bind one managed retrieval turn.

        Internal continuation prompts created by a completion retry are bound to
        the original retrieval turn and are never captured as business text.
        """

        if allow_continuation:
            continuation_id = find_pending_continuation(
                self.vault,
                self.host,
                session_id,
                user_content,
            )
            if continuation_id is not None:
                bind_turn_alias(self.vault, continuation_id, turn_id)
                consume_continuation(self.vault, continuation_id)
                return TurnOpenResult(
                    retrieval_id=continuation_id,
                    continuation=True,
                    injection_delivered=True,
                )

        retrieval_id = self.open_retrieval_turn(session_id, turn_id)
        self.capture_visible(
            session_id=session_id,
            turn_id=turn_id,
            role="user",
            content=user_content,
        )
        return TurnOpenResult(
            retrieval_id=retrieval_id,
            continuation=False,
            injection_delivered=self.injection_delivered(session_id, turn_id),
        )

    def injection_delivered(self, session_id: str, turn_id: str) -> bool:
        entry = self._host_session(session_id)
        values = entry.get("injected_turn_ids")
        return isinstance(values, list) and turn_id in values

    def mark_injection_delivered(self, session_id: str, turn_id: str) -> bool:
        path = self.vault.host_ingest_path
        if path.is_symlink() or (path.exists() and not path.is_file()):
            return False
        try:
            with self.vault.lock():
                state = self._read_ingest_state()
                hosts = state.setdefault("hosts", {})
                host_bucket = hosts.setdefault(self.host, {})
                entry = self._normalize_host_entry(host_bucket.get(session_id))
                if turn_id in entry["injected_turn_ids"]:
                    return False
                entry["injected_turn_ids"] = (
                    entry["injected_turn_ids"] + [turn_id]
                )[-256:]
                host_bucket[session_id] = entry
                self._mirror_legacy_codex(state, session_id, entry)
                atomic_write_json(path, state, mode=0o600)
                return True
        except Exception:
            return False

    def prepare_memory_tool(
        self,
        *,
        session_id: str,
        turn_id: str,
        arguments: Mapping[str, Any] | None,
    ) -> ToolPreparation:
        retrieval_id = self._retrieval_id(session_id, turn_id)
        if retrieval_id is None:
            return ToolPreparation(
                allowed=False,
                retrieval_id=None,
                arguments=None,
                reason=(
                    "The memleaf retrieval turn is unavailable or expired. "
                    "Start a new user turn before using this memory tool."
                ),
            )
        if not isinstance(arguments, Mapping):
            return ToolPreparation(
                allowed=False,
                retrieval_id=retrieval_id,
                arguments=None,
                reason=(
                    "The memleaf tool input is unavailable; the memory request "
                    "cannot be authorized for this turn."
                ),
            )
        updated = dict(arguments)
        updated["retrieval_id"] = retrieval_id
        return ToolPreparation(
            allowed=True,
            retrieval_id=retrieval_id,
            arguments=updated,
        )

    def observe_search(
        self,
        *,
        session_id: str,
        turn_id: str,
        status: str,
        call_id: str,
        supplied_retrieval_id: Any,
    ) -> bool:
        retrieval_id = self._retrieval_id(session_id, turn_id)
        if (
            retrieval_id is None
            or supplied_retrieval_id != retrieval_id
            or status not in {"found", "no_match", "error"}
            or not isinstance(call_id, str)
            or not call_id
        ):
            return False
        try:
            observe_search(self.vault, retrieval_id, status, call_id)
        except RetrievalGateError:
            return False
        return True

    def complete_turn(
        self,
        *,
        session_id: str,
        turn_id: str,
        assistant_content: str | None,
        auto_process: bool = True,
    ) -> TurnCompletion:
        """Finish one managed host turn using the existing Core semantics."""

        retrieval_id = self._retrieval_id(session_id, turn_id)
        degraded = retrieval_id is None
        capture_turn_id = turn_id

        if retrieval_id is not None:
            try:
                gate_state = validate_turn(self.vault, retrieval_id)
            except RetrievalGateError:
                gate_state = None
                degraded = True
            else:
                original_turn_id = gate_state.get("turn_id")
                if isinstance(original_turn_id, str) and original_turn_id:
                    capture_turn_id = original_turn_id
                if gate_state.get("status") == "DEGRADED":
                    degraded = True

            if gate_state is not None and gate_state.get("status") in {"NOT_SEARCHED", "ERROR"}:
                retries = int(gate_state.get("gate_retries", 0) or 0)
                if retries < MAX_GATE_RETRIES:
                    try:
                        request_gate_retry(self.vault, retrieval_id)
                    except RetrievalGateError:
                        pass
                    else:
                        reason = (
                            _GATE_ERROR_REASON
                            if gate_state.get("status") == "ERROR"
                            else _GATE_RETRY_REASON
                        )
                        marker = continuation_marker(self.vault, retrieval_id)
                        if marker:
                            reason = f"{reason} [memleaf continuation {marker}]"
                        return TurnCompletion(
                            retry_required=True,
                            retry_reason=reason,
                            degraded=degraded,
                        )
                try:
                    mark_degraded(self.vault, retrieval_id)
                except RetrievalGateError:
                    pass
                degraded = True

        captured = False
        if isinstance(assistant_content, str) and assistant_content.strip():
            ok, stored = self.capture_visible(
                session_id=session_id,
                turn_id=capture_turn_id,
                role="assistant",
                content=assistant_content,
            )
            if ok:
                captured = stored

        pending = self._process_pending(session_id)
        if captured:
            pending = True
        if not auto_process or not pending:
            if captured:
                self._set_process_pending(session_id, True)
            return TurnCompletion(degraded=degraded, captured=captured)

        try:
            processed = self.process(source=self.host, session_id=session_id)
        except Exception:
            self._set_process_pending(session_id, True)
            return TurnCompletion(
                degraded=degraded,
                captured=captured,
                process_failed=True,
            )

        deferred = False
        if isinstance(processed, Mapping):
            deferred = any(
                isinstance(processed.get(key), int)
                and not isinstance(processed.get(key), bool)
                and processed.get(key, 0) > 0
                for key in ("deferred_candidates", "deferred_inbox_turns")
            )
        self._set_process_pending(session_id, False)
        return TurnCompletion(
            degraded=degraded,
            captured=captured,
            process_deferred=deferred,
        )

    def _retrieval_id(self, session_id: str, turn_id: str) -> str | None:
        try:
            return find_turn(self.vault, self.host, session_id, turn_id)
        except RetrievalGateError:
            return None

    def _process_pending(self, session_id: str) -> bool:
        return self._host_session(session_id).get("process_pending") is True

    def _set_process_pending(self, session_id: str, value: bool) -> None:
        path = self.vault.host_ingest_path
        if path.is_symlink() or (path.exists() and not path.is_file()):
            return
        try:
            with self.vault.lock():
                state = self._read_ingest_state()
                hosts = state.setdefault("hosts", {})
                host_bucket = hosts.setdefault(self.host, {})
                entry = self._normalize_host_entry(host_bucket.get(session_id))
                entry["process_pending"] = bool(value)
                host_bucket[session_id] = entry
                self._mirror_legacy_codex(state, session_id, entry)
                atomic_write_json(path, state, mode=0o600)
        except Exception:
            return

    def _host_session(self, session_id: str) -> dict[str, Any]:
        state = self._read_ingest_state()
        hosts = state.get("hosts")
        host_bucket = hosts.get(self.host) if isinstance(hosts, Mapping) else None
        value = host_bucket.get(session_id) if isinstance(host_bucket, Mapping) else None
        if value is None and self.host == "codex":
            legacy = state.get("codex")
            value = legacy.get(session_id) if isinstance(legacy, Mapping) else None
        return self._normalize_host_entry(value)

    def _read_ingest_state(self) -> dict[str, Any]:
        path = self.vault.host_ingest_path
        if path.is_symlink() or not path.exists():
            return {"version": 2, "hosts": {}, "transcripts": {}}
        try:
            value = read_json(path)
        except (OSError, UnicodeError, TypeError, ValueError):
            return {"version": 2, "hosts": {}, "transcripts": {}}
        if not isinstance(value, dict):
            return {"version": 2, "hosts": {}, "transcripts": {}}
        state = dict(value)
        hosts = state.get("hosts")
        if not isinstance(hosts, dict):
            hosts = {}
        legacy_codex = state.get("codex")
        if isinstance(legacy_codex, Mapping):
            codex_bucket = dict(hosts.get("codex")) if isinstance(hosts.get("codex"), Mapping) else {}
            for session_id, entry in legacy_codex.items():
                if session_id not in codex_bucket:
                    codex_bucket[session_id] = entry
            hosts["codex"] = codex_bucket
        state["hosts"] = hosts
        if not isinstance(state.get("transcripts"), dict):
            state["transcripts"] = {}
        state["version"] = max(2, int(state.get("version", 1) or 1))
        return state

    @staticmethod
    def _normalize_host_entry(value: Any) -> dict[str, Any]:
        if isinstance(value, Mapping):
            pending = value.get("process_pending") is True
            injected = value.get("injected_turn_ids")
            if not isinstance(injected, list):
                injected = []
            injected = [item for item in injected if isinstance(item, str) and item][-256:]
        else:
            pending = value is True
            injected = []
        return {
            "process_pending": pending,
            "injected_turn_ids": injected,
        }

    def _mirror_legacy_codex(
        self,
        state: dict[str, Any],
        session_id: str,
        entry: Mapping[str, Any],
    ) -> None:
        if self.host != "codex":
            return
        legacy = state.get("codex")
        if not isinstance(legacy, dict):
            legacy = {}
        legacy[session_id] = dict(entry)
        state["codex"] = legacy


__all__ = [
    "HostRuntime",
    "ToolPreparation",
    "TurnCompletion",
    "TurnOpenResult",
]

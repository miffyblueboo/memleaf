"""Stage-B2 processing: complete inbox turns, remember, updates, and cleanup."""

from __future__ import annotations

import errno
import hashlib
import inspect
import json
import os
import re
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

from .capture import _safe_turn_id
from .config import save_config
from .index import EVENT_V2_BLOCK, event_key, extract_event_keys, turn_key
from .inbox import InboxEvent, InboxTurn, parse_inbox
from .llm import (
    MODEL_ERROR_CODES,
    MODEL_VALIDATION_REASONS,
    CallableBackend,
    ModelError,
    ModelUnavailable,
    ModelRouter,
)
from .locking import atomic_write_json, atomic_write_text, read_json
from .memory_writer import MemoryWriter
from .models import Memory, utc_now
from .native_index import NativeIndexer
from .prompts import (
    DUPLICATE_TARGET_CORRECTION,
    GATE_TYPE_CORRECTION,
    JSON_CORRECTION,
    MIXED_PROJECT_SCOPES_CORRECTION,
    RELATIVE_TIME_CORRECTION,
    GATE_SYSTEM,
    SCOPE_GROUNDING_CORRECTION,
    SUMMARY_SCOPE_CORRECTION,
    SUMMARY_TARGET_CORRECTION,
    SUMMARY_TYPE_CORRECTION,
    SUMMARIZE_SYSTEM,
    TARGET_RELEVANCE_CORRECTION,
    UPDATE_TARGET_TYPE_CORRECTION,
    gate_prompt,
    summarize_prompt,
)
from .redaction import redact_text
from .retrieval import candidate_matches_query, filter_by_scope, normalize_term
from .scope_state import (
    ScopeError,
    normalize_scopes,
    register_scope_nodes,
)
from .scope_maintenance import ScopeMaintainer, ScopeMaintenanceError, scope_registry_projection
from .validation import (
    MODEL_VALIDATION_DETAILS,
    ModelOutputError,
    NO_CHANGE_DECISION,
    _model_scope_grounding_evidence,
    parse_gate_output,
    parse_strict_json,
    parse_summarize_output,
    normalize_relative_calendar_text,
    is_aggregate_operational_text,
)
from .vault import safe_component


_PROCESSING_LEASE_SECONDS = 3600
_LEGACY_PROCESSING_GRACE_SECONDS = 600
_PROCESSING_STATUS = "processing"
_IDLE_STATUS = "idle"
_FAILED_STATUS = "failed"
_DIAGNOSTIC_MAX_BYTES = 256 * 1024
_DIAGNOSTIC_FILENAME = "model-diagnostics.jsonl"
_DIAGNOSTIC_GATE_REQUIRED = frozenset(("candidates",))
_DIAGNOSTIC_GATE_ALLOWED = frozenset(("candidates",))
_DIAGNOSTIC_CANDIDATE_REQUIRED = frozenset(
    ("candidate_id", "memory", "evidence_event_ids", "duplicate", "worth", "type", "scopes", "scope_source")
)
_DIAGNOSTIC_CANDIDATE_ALLOWED = _DIAGNOSTIC_CANDIDATE_REQUIRED | frozenset(
    ("reason", "duplicate_memory_id", "update_memory_id")
)
_DIAGNOSTIC_SUMMARY_REQUIRED = frozenset(("title", "body", "tags", "type", "scopes", "sources"))
_DIAGNOSTIC_SUMMARY_ALLOWED = frozenset(
    (
        "memory_id",
        "update_memory_id",
        "title",
        "body",
        "tags",
        "aliases",
        "keywords",
        "type",
        "scopes",
        "scope_source",
        "sources",
        "evidence_event_ids",
        "status",
        "completed_at",
        "shadow_native_ids",
        "scope_operations",
    )
)
# Automatic model calls need more context than the host's Scope Map, but they
# still receive a bounded slice.  These limits are intentionally local to the
# processing prompts; they do not change the host's directory-only budget.
_RELATED_MAX_ITEMS = 6
_RELATED_MAX_BODY_CHARS = 1600
_RELATED_MAX_CHARS = 6000
_SCOPE_DIRECTORY_MAX_ITEMS = 8
_SCOPE_DIRECTORY_MAX_TITLE_CHARS = 180
_SCOPE_DIRECTORY_MAX_CHARS = 2400
_MAX_SESSION_LINEAGE_DEPTH = 32
_UNSET = object()


class ProcessingError(RuntimeError):
    """A process operation could not be committed safely."""


def _failure_metadata(
    error: BaseException,
) -> tuple[str, Optional[str], Optional[str], Optional[str], Optional[int]]:
    code = getattr(error, "code", None)
    if not isinstance(code, str) or code not in MODEL_ERROR_CODES:
        if isinstance(error, ModelOutputError):
            code = "model_invalid_response"
        elif isinstance(error, ModelUnavailable):
            code = "model_unavailable"
        else:
            code = "model_failed"
    stage = getattr(error, "stage", None)
    if stage not in {"gate", "summarize"}:
        stage = None
    validation_reason = getattr(error, "validation_reason", None)
    if not isinstance(validation_reason, str) or validation_reason not in MODEL_VALIDATION_REASONS:
        validation_reason = "schema_violation" if isinstance(error, ModelOutputError) else None
        if code == "model_invalid_response" and validation_reason is None:
            validation_reason = "response_shape"
    validation_detail = getattr(error, "validation_detail", None)
    if not isinstance(validation_detail, str) or validation_detail not in MODEL_VALIDATION_DETAILS:
        validation_detail = "other_schema_violation" if isinstance(error, ModelOutputError) else None
    attempt_count = getattr(error, "attempt_count", None)
    if isinstance(attempt_count, bool) or not isinstance(attempt_count, int) or attempt_count not in (1, 2, 3):
        attempt_count = None
    return code, stage, validation_reason, validation_detail, attempt_count


def _json_top_level_type(value: Any) -> str:
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, bool):
        return "boolean"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return "number"
    return "unknown"


def _model_output_statistics(raw: Any, purpose: str) -> dict[str, Any]:
    """Return structural-only model output statistics; never retain model text."""

    stats: dict[str, Any] = {
        "output_chars": len(raw) if isinstance(raw, str) else 0,
        "output_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest() if isinstance(raw, str) else "",
        "top_level_type": "non_text" if not isinstance(raw, str) else "invalid",
        "candidate_count": 0,
        "missing_fields_count": 0,
        "unknown_fields_count": 0,
    }
    if not isinstance(raw, str):
        return stats
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return stats
    stats["top_level_type"] = _json_top_level_type(parsed)
    if not isinstance(parsed, Mapping):
        return stats
    if purpose == "gate":
        required = _DIAGNOSTIC_GATE_REQUIRED
        allowed = _DIAGNOSTIC_GATE_ALLOWED
    elif purpose == "summarize":
        required = _DIAGNOSTIC_SUMMARY_REQUIRED
        allowed = _DIAGNOSTIC_SUMMARY_ALLOWED
    else:
        return stats
    stats["missing_fields_count"] = len(required - set(parsed))
    stats["unknown_fields_count"] = len(set(parsed) - allowed)
    if purpose != "gate":
        return stats
    candidates = parsed.get("candidates")
    if not isinstance(candidates, list):
        return stats
    stats["candidate_count"] = len(candidates)
    missing_count = stats["missing_fields_count"]
    unknown_count = stats["unknown_fields_count"]
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        missing_count += len(_DIAGNOSTIC_CANDIDATE_REQUIRED - set(candidate))
        unknown_count += len(set(candidate) - _DIAGNOSTIC_CANDIDATE_ALLOWED)
    stats["missing_fields_count"] = missing_count
    stats["unknown_fields_count"] = unknown_count
    return stats


class _Snapshot:
    def __init__(self, turn: InboxTurn, token: str, state_key: str):
        self.turn = turn
        self.token = token
        self.state_key = state_key


def _empty_processed() -> dict[str, Any]:
    return {"version": 1, "event_keys": [], "events": {}, "sessions": {}}


def _read_processed(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.exists():
        return _empty_processed()
    try:
        value = read_json(path)
    except (OSError, UnicodeError, TypeError, ValueError):
        return _empty_processed()
    if not isinstance(value, dict):
        return _empty_processed()
    result = dict(value)
    result.setdefault("version", 1)
    result.setdefault("event_keys", [])
    result.setdefault("events", {})
    result.setdefault("sessions", {})
    if not isinstance(result.get("sessions"), dict):
        result["sessions"] = {}
    return result


def _now_value(clock: Any = None) -> str:
    value: Any = None
    try:
        if clock is None:
            value = None
        elif hasattr(clock, "now") and callable(clock.now):
            value = clock.now()
        elif callable(clock):
            value = clock()
        else:
            value = clock
    except Exception as error:
        raise ProcessingError("clock failed") from error
    if value is None:
        return utc_now()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    if isinstance(value, str) and value.strip():
        return value
    raise ProcessingError("clock must return an ISO timestamp")


def _parse_time(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _session_key(source: str, session_id: str) -> str:
    return f"{source}/{session_id}"


def _as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return default


def _safe_scope_background(state: Mapping[str, Any], explicit: Any = None) -> Any:
    value = explicit
    if value is None:
        value = state.get("scopes", state.get("scope_background", state.get("scope")))
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    return []


def _event_payload(turn: InboxTurn) -> list[dict[str, Any]]:
    return [
        {
            "event_key": event.event_key,
            "role": event.role,
            "turn_id": event.turn_id or "",
            "timestamp": event.timestamp,
            "content": event.content,
        }
        for event in turn.events
    ]


def _summary_evidence_keys(summary: Mapping[str, Any], candidate: Mapping[str, Any]) -> list[str]:
    """Collect only current-turn evidence IDs that the summary explicitly names."""

    values: list[str] = []

    def add(value: Any) -> None:
        if not isinstance(value, str) or not value:
            return
        key = value.casefold()
        if key not in values:
            values.append(key)

    evidence = summary.get("evidence_event_ids")
    if isinstance(evidence, list):
        for value in evidence:
            add(value)
    sources = summary.get("sources")
    if isinstance(sources, list):
        for source in sources:
            if not isinstance(source, Mapping):
                continue
            add(source.get("event_key"))
            source_evidence = source.get("evidence_event_ids")
            if isinstance(source_evidence, list):
                for value in source_evidence:
                    add(value)
    if values:
        return values
    candidate_evidence = candidate.get("evidence_event_ids")
    if isinstance(candidate_evidence, list):
        for value in candidate_evidence:
            add(value)
    return values


def _summary_date_anchor(
    turn: InboxTurn,
    summary: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> Optional[datetime]:
    """Return one unambiguous current-evidence timestamp for date rewriting."""

    event_timestamps = {
        event.event_key.casefold(): _parse_time(event.timestamp)
        for event in turn.events
        if isinstance(event.event_key, str)
    }
    evidence_keys = _summary_evidence_keys(summary, candidate)
    if not evidence_keys:
        return None
    timestamps: list[datetime] = []
    for evidence_key in evidence_keys:
        timestamp = event_timestamps.get(evidence_key)
        if timestamp is None:
            # An omitted, malformed, or foreign evidence timestamp is not a
            # safe anchor.  The strict parser will reject unresolved dates and
            # the caller can defer this candidate without advancing silently.
            return None
        timestamps.append(timestamp)
    calendar_dates = {timestamp.date() for timestamp in timestamps}
    if len(calendar_dates) != 1:
        # A summary can cite multiple events from different UTC dates, but a
        # single unlabelled relative phrase cannot be assigned safely to one
        # of them.
        return None
    return timestamps[0]


def _normalize_summary_dates(
    raw: Any,
    turn: InboxTurn,
    candidate: Mapping[str, Any],
) -> Any:
    """Normalize summary dates before strict validation when evidence permits."""

    if not isinstance(raw, str):
        return raw
    try:
        parsed = parse_strict_json(raw)
    except ModelOutputError:
        # Let the regular summary parser preserve its invalid-JSON metadata.
        return raw
    if not isinstance(parsed, Mapping):
        return raw
    anchor = _summary_date_anchor(turn, parsed, candidate)
    if anchor is None:
        return raw
    normalized = dict(parsed)
    changed = False
    for field in ("title", "body"):
        value = normalized.get(field)
        if not isinstance(value, str):
            continue
        rewritten = normalize_relative_calendar_text(value, anchor)
        if rewritten is not None:
            normalized[field] = rewritten
            changed = changed or rewritten != value
    if not changed:
        return raw
    return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))


def _native_result(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        values = [value]
    elif isinstance(value, (list, tuple)):
        values = list(value)
    else:
        return []
    result: list[dict[str, Any]] = []
    allowed_fields = {
        "memory_id",
        "native_id",
        "native",
        "source",
        "native_source_id",
        "agent",
        "native_agent",
        "locator",
        "share",
        "title",
        "body",
        "tags",
        "type",
        "scopes",
        "aliases",
        "keywords",
        "status",
        "completed_at",
    }
    for item in values:
        if isinstance(item, Memory):
            value = item.to_dict()
            result.append({key: value[key] for key in allowed_fields if key in value})
        elif isinstance(item, Mapping):
            result.append({key: item[key] for key in allowed_fields if key in item})
        else:
            result.append({"body": str(item)})
    return result


def _merge_related(values: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen_bodies: set[str] = set()
    for item in values:
        if not isinstance(item, Mapping):
            continue
        body = item.get("body")
        if not isinstance(body, str):
            continue
        normalized = normalize_term(body)
        if normalized and normalized in seen_bodies:
            continue
        if normalized:
            seen_bodies.add(normalized)
        result.append(dict(item))
    return result


def _invoke_native(reader: Any, query: str, scope: Any) -> list[dict[str, Any]]:
    if reader is None:
        return []
    callback = reader.search if hasattr(reader, "search") and callable(reader.search) else reader
    if not callable(callback):
        raise TypeError("native_memory_reader must be callable or expose search()")
    try:
        signature = inspect.signature(callback)
    except (TypeError, ValueError):
        value = callback(query)
    else:
        try:
            signature.bind(query, scope=scope)
        except TypeError:
            try:
                signature.bind(query)
            except TypeError as error:
                raise TypeError("native_memory_reader must accept query") from error
            value = callback(query)
        else:
            value = callback(query, scope=scope)
    return _native_result(value)


class Processor:
    def __init__(self, service: Any):
        self.service = service
        self.writer = MemoryWriter(service)
        # Candidate-level decisions are collected before the commit phase.
        # This small overlay lets a later pending turn in the same
        # process call see a memory planned by an earlier turn without
        # exposing an uncommitted body outside this processor.
        self._planned_related: list[dict[str, Any]] = []
        self._deferred_by_turn: dict[tuple[str, str, str], list[dict[str, Any]]] = {}

    def _resolve_backend(self, model: Any = None, router: Any = None) -> Any:
        backend = router if router is not None else model
        if backend is None:
            backend = getattr(self.service, "router", None)
        if backend is None:
            backend = ModelRouter.from_config(self.service.vault.config())
            self.service.router = backend
        if callable(backend) and not hasattr(backend, "complete"):
            backend = CallableBackend(backend)
        if not hasattr(backend, "complete"):
            raise ModelUnavailable("no model backend is configured")
        return backend

    def _auto_compact(self, *, model: Any = None, router: Any = None) -> dict[str, Any]:
        from .compaction import Compactor

        return Compactor(self.service).auto(model=model, router=router)

    def _complete(self, backend: Any, prompt: str, *, system: str, purpose: str) -> str:
        try:
            value = backend.complete(prompt, system=system, purpose=purpose, temperature=0.0)
        except ModelError as error:
            error.with_stage(purpose)
            raise
        except Exception as error:
            raise ModelError("model backend failed", stage=purpose) from error
        if not isinstance(value, str):
            raise ModelError(
                "model backend returned non-text output",
                code="model_invalid_response",
                stage=purpose,
                validation_reason="response_shape",
            )
        return value

    @staticmethod
    def _set_stage_diagnostics(error: BaseException, *, purpose: str, attempt_count: int) -> None:
        if isinstance(error, ModelError):
            error.with_stage(purpose)
            if error.code == "model_invalid_response" and (
                not isinstance(getattr(error, "validation_reason", None), str)
                or error.validation_reason not in MODEL_VALIDATION_REASONS
            ):
                error.validation_reason = "response_shape"
        elif isinstance(error, ModelOutputError):
            error.stage = purpose
            if (
                not isinstance(getattr(error, "validation_reason", None), str)
                or error.validation_reason not in MODEL_VALIDATION_REASONS
            ):
                error.validation_reason = "schema_violation"
        error.attempt_count = attempt_count

    @staticmethod
    def _retryable_json_error(error: BaseException) -> bool:
        return isinstance(error, ModelOutputError) or (
            isinstance(error, ModelError) and error.code == "model_invalid_response"
        )

    @staticmethod
    def _allows_next_json_attempt(error: BaseException, attempt_count: int) -> bool:
        # Invalid extraction output is safe to retry with the bounded
        # correction prompt.  Schema/shape violations are no less likely to
        # be transient than an empty response; allowing the same final
        # attempt prevents a single malformed JSON object from failing an
        # otherwise recoverable automatic process.  The caller still stops
        # after attempt three and preserves the final diagnostics.
        return Processor._retryable_json_error(error) and attempt_count < 3

    @staticmethod
    def _safe_correction_hint(error: BaseException) -> Optional[str]:
        detail = getattr(error, "validation_detail", None)
        if isinstance(detail, str) and detail in MODEL_VALIDATION_DETAILS:
            return detail
        reason = getattr(error, "validation_reason", None)
        if isinstance(error, ModelError) and error.code == "model_invalid_response":
            if isinstance(reason, str) and reason in MODEL_VALIDATION_REASONS:
                return reason
        return None

    @staticmethod
    def _correction_instruction(error: BaseException) -> Optional[str]:
        hint = Processor._safe_correction_hint(error)
        stage = getattr(error, "stage", None)
        if hint == "duplicate_update_target":
            return DUPLICATE_TARGET_CORRECTION
        if hint == "mixed_project_scopes":
            return MIXED_PROJECT_SCOPES_CORRECTION
        if stage == "gate" and hint == "update_target_type_mismatch":
            return UPDATE_TARGET_TYPE_CORRECTION
        if stage == "gate" and hint == "invalid_type":
            return GATE_TYPE_CORRECTION
        if stage == "gate" and hint == "scope_not_grounded":
            return SCOPE_GROUNDING_CORRECTION
        if stage == "gate" and hint == "target_not_relevant":
            return TARGET_RELEVANCE_CORRECTION
        if stage == "summarize" and hint == "scope_drift":
            return SUMMARY_SCOPE_CORRECTION
        if hint == "relative_time":
            return RELATIVE_TIME_CORRECTION
        if stage == "summarize" and hint == "invalid_update_target":
            return SUMMARY_TARGET_CORRECTION
        if stage == "summarize" and hint == "invalid_type":
            return SUMMARY_TYPE_CORRECTION
        if hint is not None:
            return f"Previous output violated: {hint}."
        return None

    def _diagnostic_enabled(self) -> bool:
        try:
            config = self.service.vault.config()
            llm = config.get("llm") if isinstance(config, Mapping) else None
            return isinstance(llm, Mapping) and type(llm.get("diagnostic_logging", False)) is bool and llm.get(
                "diagnostic_logging", False
            )
        except Exception:
            return False

    def _write_model_diagnostic(
        self,
        *,
        purpose: str,
        attempt_count: int,
        context: Mapping[str, Any] | None,
        raw: Any,
        error: BaseException | None,
    ) -> None:
        """Best-effort bounded JSONL diagnostics; never changes model outcome."""

        if not self._diagnostic_enabled():
            return
        context = context if isinstance(context, Mapping) else {}
        source = context.get("source", "")
        session_id = context.get("session_id", "")
        turn_index = context.get("turn_index")
        if not isinstance(source, str):
            source = ""
        if not isinstance(session_id, str):
            session_id = ""
        if isinstance(turn_index, bool) or not isinstance(turn_index, int):
            turn_index = None
        if isinstance(attempt_count, bool) or not isinstance(attempt_count, int) or attempt_count not in (1, 2, 3):
            attempt_count = None
        failure_code = ""
        validation_reason = ""
        validation_detail = ""
        if error is not None:
            failure_code, _failure_stage, reason, detail, _attempt = _failure_metadata(error)
            validation_reason = reason or ""
            validation_detail = detail or ""
        entry = {
            "timestamp": utc_now(),
            "source": source,
            "session_id": session_id,
            "turn_index": turn_index,
            "stage": purpose,
            "attempt_count": attempt_count,
            "failure_code": failure_code,
            "validation_reason": validation_reason,
            "validation_detail": validation_detail,
            **_model_output_statistics(raw, purpose),
        }
        response_diagnostics = getattr(error, "response_diagnostics", None) if error is not None else None
        if isinstance(response_diagnostics, Mapping):
            allowed_diagnostics = {
                "finish_reason",
                "completion_tokens",
                "content_present",
                "content_chars",
                "reasoning_present",
                "reasoning_chars",
            }
            for key in allowed_diagnostics:
                value = response_diagnostics.get(key)
                if key == "finish_reason":
                    if isinstance(value, str) and value in {
                        "stop",
                        "length",
                        "tool_calls",
                        "function_call",
                        "content_filter",
                        "insufficient_system_resource",
                        "unknown",
                    }:
                        entry[key] = value
                elif key == "completion_tokens":
                    if value is None or (
                        isinstance(value, int)
                        and not isinstance(value, bool)
                        and 0 <= value <= 1_000_000
                    ):
                        entry[key] = value
                elif key in {"content_present", "reasoning_present"}:
                    if isinstance(value, bool):
                        entry[key] = value
                elif isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 1_000_000:
                    entry[key] = value
        try:
            payload = (
                json.dumps(entry, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
                + "\n"
            ).encode("utf-8")
            if len(payload) > _DIAGNOSTIC_MAX_BYTES:
                return
            path = self.service.vault.logs_path / _DIAGNOSTIC_FILENAME
            rotated = path.with_name(f"{path.name}.1")
            with self.service.vault.lock():
                logs_path = self.service.vault.logs_path
                if logs_path.exists() and (logs_path.is_symlink() or not logs_path.is_dir()):
                    raise OSError("unsafe diagnostics directory")
                logs_path.mkdir(parents=True, exist_ok=True)
                os.chmod(logs_path, 0o700)
                if path.is_symlink():
                    raise OSError("unsafe diagnostics file")
                if rotated.is_symlink():
                    raise OSError("unsafe diagnostics rotation file")
                current_size = path.stat().st_size if path.exists() else 0
                if current_size + len(payload) > _DIAGNOSTIC_MAX_BYTES:
                    if rotated.exists():
                        rotated.unlink()
                    if path.exists():
                        os.replace(path, rotated)
                    current_size = 0
                with path.open("ab") as stream:
                    os.chmod(path, 0o600)
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
        except Exception:
            return

    def _complete_json_stage(
        self,
        backend: Any,
        prompt: str,
        *,
        system: str,
        purpose: str,
        parser: Callable[[str], Any],
        diagnostic_context: Mapping[str, Any] | None = None,
    ) -> Any:
        correction_prompt = prompt + "\n\n" + JSON_CORRECTION
        for attempt_count in (1, 2, 3):
            raw: Any = None
            try:
                raw = self._complete(
                    backend,
                    prompt if attempt_count == 1 else correction_prompt,
                    system=system,
                    purpose=purpose,
                )
                parsed = parser(raw)
            except (ModelError, ModelOutputError) as error:
                self._set_stage_diagnostics(error, purpose=purpose, attempt_count=attempt_count)
                try:
                    self._write_model_diagnostic(
                        purpose=purpose,
                        attempt_count=attempt_count,
                        context=diagnostic_context,
                        raw=raw,
                        error=error,
                    )
                except Exception:
                    pass
                if self._allows_next_json_attempt(error, attempt_count):
                    correction_prompt = prompt + "\n\n" + JSON_CORRECTION
                    instruction = self._correction_instruction(error)
                    if instruction is not None:
                        correction_prompt += f"\n{instruction}"
                    continue
                raise
            try:
                self._write_model_diagnostic(
                    purpose=purpose,
                    attempt_count=attempt_count,
                    context=diagnostic_context,
                    raw=raw,
                    error=None,
                )
            except Exception:
                pass
            return parsed
        raise AssertionError("unreachable JSON stage retry")

    def _write_processed_unlocked(self, processed: Mapping[str, Any]) -> None:
        atomic_write_json(self.service.vault.processed_index_path, dict(processed))

    def _cleanup_hours(self) -> int:
        try:
            config = self.service.vault.config()
        except (OSError, UnicodeError, ValueError, TypeError) as error:
            raise ProcessingError("cannot read process.inbox_cleanup_hours") from error
        process = config.get("process") if isinstance(config, Mapping) else None
        hours = process.get("inbox_cleanup_hours") if isinstance(process, Mapping) else None
        if type(hours) is not int or hours < 0:
            raise ProcessingError("invalid process.inbox_cleanup_hours")
        return hours

    @staticmethod
    def _owner_pid_status(owner_pid: Any) -> Optional[bool]:
        """Return whether a POSIX processing owner is alive.

        ``None`` means that the marker does not contain a usable PID or that
        the platform cannot answer the question.  In that case callers use
        the short legacy grace period rather than assuming ownership is gone.
        """

        if os.name != "posix" or isinstance(owner_pid, bool) or not isinstance(owner_pid, int):
            return None
        if owner_pid <= 0:
            return False
        try:
            os.kill(owner_pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            # The process exists but is owned by another user.
            return True
        except OSError as error:
            if error.errno == errno.ESRCH:
                return False
            if error.errno == errno.EPERM:
                return True
            return None
        return True

    @classmethod
    def _processing_marker_live(cls, marker: Any, now: str) -> bool:
        if not isinstance(marker, Mapping) or marker.get("status") != _PROCESSING_STATUS:
            return False
        started = _parse_time(marker.get("started_at"))
        current = _parse_time(now)
        if started is None or current is None:
            return False
        owner_status = cls._owner_pid_status(marker.get("owner_pid"))
        if owner_status is False:
            # A killed MCP worker cannot keep a processing claim alive.  This
            # is what makes a timed-out provider call immediately retryable.
            return False
        lease = _PROCESSING_LEASE_SECONDS if owner_status is True else _LEGACY_PROCESSING_GRACE_SECONDS
        return (current - started).total_seconds() <= lease

    def _session_path_without_create(self, state_key: str) -> Optional[Path]:
        try:
            source, session_id = state_key.split("/", 1)
            safe_component(source, "source")
            safe_component(session_id, "session id")
        except (ValueError, AttributeError):
            return None
        path = self.service.vault.inbox_path / source / f"{session_id}.md"
        return path

    def _remove_turn_blocks(self, path: Path, entry: Mapping[str, Any]) -> bool:
        if not path.exists():
            return False
        if path.is_symlink():
            raise ProcessingError("unsafe inbox session path")
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise ProcessingError("cannot read inbox session") from error
        target_keys = {
            item.casefold()
            for item in entry.get("event_keys", [])
            if isinstance(item, str)
        }
        target_turn_key = entry.get("turn_key")
        target_index = entry.get("turn_index")
        removed_keys: set[str] = set()

        def replace(match: re.Match[str]) -> str:
            try:
                metadata = json.loads(match.group("meta"))
            except (TypeError, ValueError):
                return match.group(0)
            if not isinstance(metadata, Mapping):
                return match.group(0)
            key = metadata.get("event_key")
            matches = isinstance(key, str) and key.casefold() in target_keys
            matches = matches or (
                isinstance(target_turn_key, str)
                and metadata.get("turn_key") == target_turn_key
                and metadata.get("turn_index") == target_index
            )
            if matches:
                if isinstance(key, str):
                    removed_keys.add(key.casefold())
                return ""
            return match.group(0)

        updated = EVENT_V2_BLOCK.sub(replace, text)
        keys_for_legacy = removed_keys or target_keys
        for key in keys_for_legacy:
            updated = re.sub(
                rf"(?m)^<!--\s*memleaf:event-key:v1:{re.escape(key)}\s*-->[ \t]*(?:\r?\n|$)",
                "",
                updated,
            )
        if updated == text:
            return False
        if not extract_event_keys(updated):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        else:
            atomic_write_text(path, updated)
        return True

    def _cleanup_due_unlocked(self, processed: dict[str, Any], now: str, cleanup_hours: int) -> int:
        # ``cleanup_hours`` is read and validated by the caller even though
        # eligibility timestamps are written at successful commit time.  It
        # is accepted here to keep the lock-held cleanup boundary explicit.
        del cleanup_hours
        now_time = _parse_time(now)
        if now_time is None:
            return 0
        sessions = processed.get("sessions")
        if not isinstance(sessions, dict):
            return 0
        due_entries: list[tuple[str, int, dict[str, Any]]] = []
        for state_key, state_value in sessions.items():
            if not isinstance(state_key, str) or not isinstance(state_value, dict):
                continue
            if self._processing_marker_live(state_value.get("processing"), now):
                continue
            entries = state_value.get("processed_turns")
            if not isinstance(entries, list):
                continue
            path = self._session_path_without_create(state_key)
            for entry_index, raw_entry in enumerate(entries):
                if not isinstance(raw_entry, dict) or raw_entry.get("cleanup_done_at"):
                    continue
                due = _parse_time(raw_entry.get("eligible_cleanup_at"))
                if due is None or due > now_time:
                    continue
                if path is not None:
                    self._remove_turn_blocks(path, raw_entry)
                due_entries.append((state_key, entry_index, raw_entry))
        if not due_entries:
            return 0

        updated = deepcopy(processed)
        updated_sessions = updated.setdefault("sessions", {})
        for state_key, entry_index, _ in due_entries:
            state = updated_sessions.get(state_key)
            if not isinstance(state, dict):
                raise ProcessingError("cleanup session disappeared")
            entries = state.get("processed_turns")
            if not isinstance(entries, list) or entry_index >= len(entries):
                raise ProcessingError("cleanup entry disappeared")
            entry = entries[entry_index]
            if not isinstance(entry, dict):
                raise ProcessingError("cleanup entry is invalid")
            entry["cleanup_done_at"] = now

        # Inbox blocks are intentionally removed before the ledger is marked,
        # but the ledger is only committed after the derived index succeeds.
        # If any later write fails, the entry remains eligible and a retry can
        # safely observe the already-absent target block.
        try:
            self._write_processed_unlocked(updated)
            self.service._rebuild_index_unlocked()
        except Exception:
            try:
                self._write_processed_unlocked(processed)
            except Exception:
                pass
            raise
        processed.clear()
        processed.update(updated)
        return len(due_entries)

    def _turns_by_session(self) -> dict[str, list[InboxTurn]]:
        grouped: dict[str, list[InboxTurn]] = {}
        for turn in parse_inbox(self.service.vault):
            if not isinstance(turn.source, str) or not isinstance(turn.session_id, str):
                continue
            grouped.setdefault(_session_key(turn.source, turn.session_id), []).append(turn)
        for values in grouped.values():
            values.sort(key=lambda turn: (turn.turn_index or 0, turn.turn_key or ""))
        return grouped

    def _snapshot(
        self,
        *,
        source: str | None,
        session_id: str | None,
        now: str,
        cleanup_hours: int,
        scope: Any = None,
    ) -> tuple[list[_Snapshot], int]:
        with self.service.vault.lock():
            self.service._recover_compaction_unlocked()
            processed = _read_processed(self.service.vault.processed_index_path)
            cleaned = self._cleanup_due_unlocked(processed, now, cleanup_hours)
            grouped = self._turns_by_session()
            sessions = processed.setdefault("sessions", {})
            snapshots: list[_Snapshot] = []
            for state_key, turns in grouped.items():
                if source is not None and not state_key.startswith(f"{source}/"):
                    continue
                if session_id is not None and state_key != _session_key(source or state_key.split("/", 1)[0], session_id):
                    continue
                state = sessions.get(state_key)
                if not isinstance(state, dict):
                    state = {}
                processing = state.get("processing")
                if self._processing_marker_live(processing, now):
                    continue
                processed_entries = state.get("processed_turns", [])
                if not isinstance(processed_entries, list):
                    processed_entries = []
                processed_keys = {
                    entry.get("turn_key")
                    for entry in processed_entries
                    if isinstance(entry, Mapping) and isinstance(entry.get("turn_key"), str)
                }
                processed_indices = {
                    _as_int(entry.get("turn_index"), -1)
                    for entry in processed_entries
                    if isinstance(entry, Mapping)
                }
                watermark = max(
                    _as_int(state.get("watermark"), 0),
                    _as_int(state.get("processed_watermark"), 0),
                    *(index for index in processed_indices if index > 0),
                )
                by_index = {
                    turn.turn_index: turn
                    for turn in turns
                    if isinstance(turn.turn_index, int) and turn.turn_index > 0
                }
                next_index = watermark + 1
                selected: list[InboxTurn] = []
                # A deferred turn is already reflected in the watermark so
                # that later turns are not blocked.  An explicit scope on a
                # subsequent process call is the small, deterministic retry
                # signal; select those turns before ordinary pending work.
                can_retry_deferred = scope is not None and scope not in ("", [])
                if can_retry_deferred:
                    deferred = [
                        entry
                        for entry in processed_entries
                        if isinstance(entry, Mapping)
                        and isinstance(entry.get("deferred_candidates"), list)
                        and entry.get("deferred_candidates")
                    ]
                    deferred.sort(key=lambda entry: _as_int(entry.get("turn_index"), 0))
                    for entry in deferred:
                        turn_key_value = entry.get("turn_key")
                        turn = next(
                            (item for item in turns if item.turn_key == turn_key_value and item.complete),
                            None,
                        )
                        if turn is not None:
                            selected.append(turn)
                if not selected:
                    while next_index in by_index:
                        turn = by_index[next_index]
                        if not turn.complete:
                            break
                        if turn.turn_key in processed_keys or next_index in processed_indices:
                            next_index += 1
                            continue
                        selected.append(turn)
                        next_index += 1
                if not selected:
                    continue
                token = uuid.uuid4().hex
                state["processing"] = {
                    "status": _PROCESSING_STATUS,
                    "token": token,
                    "owner_pid": os.getpid(),
                    "turn_keys": [turn.turn_key for turn in selected],
                    "turn_indices": [turn.turn_index for turn in selected],
                    "started_at": now,
                }
                sessions[state_key] = state
                snapshots.extend(_Snapshot(turn, token, state_key) for turn in selected)
            if snapshots:
                self._write_processed_unlocked(processed)
            return snapshots, cleaned

    def _mark_failed(self, snapshots: list[_Snapshot], error: BaseException) -> None:
        try:
            now = _now_value(getattr(self.service, "clock", None))
            failure_code, failure_stage, validation_reason, validation_detail, attempt_count = _failure_metadata(error)
            with self.service.vault.lock():
                processed = _read_processed(self.service.vault.processed_index_path)
                sessions = processed.setdefault("sessions", {})
                for snapshot in snapshots:
                    state = sessions.get(snapshot.state_key)
                    if not isinstance(state, dict):
                        continue
                    marker = state.get("processing")
                    if not isinstance(marker, Mapping) or marker.get("token") != snapshot.token:
                        continue
                    failed_marker = {
                        "status": _FAILED_STATUS,
                        "token": snapshot.token,
                        "turn_keys": list(marker.get("turn_keys", [])),
                        "turn_indices": list(marker.get("turn_indices", [])),
                        "failed_at": now,
                    }
                    failed_marker["failure_code"] = failure_code
                    if failure_stage is not None:
                        failed_marker["failure_stage"] = failure_stage
                    if validation_reason is not None:
                        failed_marker["validation_reason"] = validation_reason
                    if validation_detail is not None:
                        failed_marker["validation_detail"] = validation_detail
                    if attempt_count is not None:
                        failed_marker["attempt_count"] = attempt_count
                    state["processing"] = failed_marker
                self._write_processed_unlocked(processed)
        except Exception:
            # Preserve the original safe error; a future call can recover an
            # orphaned processing marker by replacing it.
            return

    def _conversation_title(self, turn: InboxTurn) -> str:
        path = self._session_path_without_create(_session_key(turn.source, turn.session_id))
        if path is not None and path.exists():
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    if line.startswith("# Session "):
                        return line[2:].strip()
            except (OSError, UnicodeError):
                pass
        return f"{turn.source}/{turn.session_id}"

    def _scope_registry_projection(self) -> list[dict[str, Any]]:
        with self.service.vault.lock():
            try:
                config = self.service.vault.config()
                return scope_registry_projection(config)
            except (OSError, UnicodeError, ValueError, TypeError, ScopeMaintenanceError) as error:
                raise ProcessingError("invalid scope registry") from error

    @staticmethod
    def _overlay_related(
        related: Iterable[Mapping[str, Any]],
        overlay: Iterable[Mapping[str, Any]] = (),
        *,
        query: str = "",
        scope: Any = None,
    ) -> list[dict[str, Any]]:
        """Overlay same-ID planned memories while retaining relevant results."""

        values = [dict(item) for item in related if isinstance(item, Mapping)]
        by_id: dict[str, dict[str, Any]] = {}
        without_id: list[dict[str, Any]] = []
        for item in values:
            memory_id = item.get("memory_id")
            if isinstance(memory_id, str) and item.get("native") is not True:
                by_id[memory_id.casefold()] = item
            else:
                without_id.append(item)
        for item in overlay:
            if not isinstance(item, Mapping):
                continue
            value = dict(item)
            memory_id = value.get("memory_id")
            if isinstance(memory_id, str) and value.get("native") is not True:
                item_scopes = value.get("scopes")
                if isinstance(scope, str):
                    requested_scopes = {scope.casefold()}
                elif isinstance(scope, (list, tuple, set)):
                    requested_scopes = {
                        item.casefold()
                        for item in scope
                        if isinstance(item, str)
                    }
                else:
                    requested_scopes = set()
                if requested_scopes and isinstance(item_scopes, list):
                    available_scopes = {
                        item.casefold()
                        for item in item_scopes
                        if isinstance(item, str)
                    }
                    if "global" in requested_scopes:
                        if "global" not in available_scopes:
                            continue
                    elif not (
                        available_scopes.intersection(requested_scopes)
                        or "global" in available_scopes
                    ):
                        continue
                normalized_query = normalize_term(query)
                haystack = normalize_term(
                    " ".join(
                        str(value.get(field, ""))
                        for field in ("title", "body")
                    )
                )
                if normalized_query and haystack:
                    query_fragments: list[str] = []
                    for fragment in re.findall(
                        r"[\u4e00-\u9fff]{2,}|[a-z0-9]+",
                        normalized_query,
                        re.UNICODE,
                    ):
                        if re.fullmatch(r"[\u4e00-\u9fff]+", fragment):
                            query_fragments.extend(
                                fragment[index : index + 2]
                                for index in range(len(fragment) - 1)
                            )
                        else:
                            query_fragments.append(fragment)
                    if normalized_query not in haystack and query_fragments and not any(
                        fragment in haystack for fragment in query_fragments
                    ):
                        continue
                by_id[memory_id.casefold()] = value
            else:
                without_id.append(value)
        return _merge_related(list(by_id.values()) + without_id)

    def _related_query(
        self,
        turn: InboxTurn,
        state: Mapping[str, Any],
        query: str,
        explicit_scope: Any = None,
        *,
        overlay: Iterable[Mapping[str, Any]] = (),
        strict_relevance: bool = False,
        priority_memory_ids: Iterable[str] = (),
        priority_only: bool = False,
        scope_records: Optional[list[Any]] = None,
    ) -> tuple[
        list[dict[str, Any]],
        Any,
        list[dict[str, str]],
        Optional[tuple[list[Any], bool]],
    ]:
        visible = query.strip() if isinstance(query, str) else ""
        scope = _safe_scope_background(state, explicit_scope)
        local: list[dict[str, Any]] = []
        indexed_native: list[dict[str, Any]] = []
        scope_fallback: Optional[tuple[list[Any], bool]] = None
        with self.service.vault.lock():
            priority_wanted = [
                value.casefold()
                for value in priority_memory_ids
                if isinstance(value, str) and value
            ]
            priority_records: list[Any] = []
            if priority_wanted:
                available = scope_records
                if available is None:
                    available, _ = self._scope_records_unlocked(scope)
                by_id = {
                    record.memory.memory_id.casefold(): record
                    for record in available
                }
                priority_records = [
                    by_id[value] for value in priority_wanted if value in by_id
                ]
            if priority_only:
                # A candidate already selected an active target from the
                # directory.  Reading that target is sufficient; a second
                # full-text search cannot change the candidate and only adds
                # cost (and unrelated context).
                records = priority_records
            else:
                records = self.service._search_unlocked(
                    visible,
                    scope=scope if scope else None,
                    include_history=False,
                    todo_status="all",
                    limit=None,
                    # Processing needs the same candidate relevance boundary as
                    # the public directory search.  The legacy indexed-first
                    # lookup can return no record for an elliptical follow-up
                    # such as “this project's tasks”, even when the session scope
                    # identifies the project and its active memory is the only
                    # plausible maintenance target.
                    strict_candidates=True,
                ) if visible else []
                if visible and strict_relevance and self._has_specific_scope(scope):
                    records = [
                        record
                        for record in records
                        if candidate_matches_query(record.memory, visible)
                    ]
                if visible and not records and not priority_records and self._has_specific_scope(scope):
                    scoped_records, ambiguous = self._scope_records_unlocked(scope)
                    scope_fallback = (scoped_records, ambiguous)
                    records = [] if ambiguous else scoped_records
                if priority_records:
                    priority_ids = {
                        record.memory.memory_id.casefold()
                        for record in priority_records
                    }
                    records = priority_records + [
                        record
                        for record in records
                        if record.memory.memory_id.casefold() not in priority_ids
                    ]
            local = _native_result([record.memory for record in records])
            if visible:
                if not priority_only:
                    indexed_native = NativeIndexer(self.service.vault).search_unlocked(
                        visible,
                        target_agent=turn.source,
                        for_context=False,
                        limit=None,
                    )
        native = (
            _invoke_native(getattr(self.service, "native_memory_reader", None), visible, scope)
            if visible and not priority_only
            else []
        )
        related = self._overlay_related(
            _merge_related(local + indexed_native + native),
            overlay,
            query=visible,
            scope=scope,
        )
        related = self._bound_related(
            related,
            priority_memory_ids=priority_memory_ids,
        )
        native_refs = [
            {
                "source_id": item["native_source_id"],
                "native_id": item["native_id"],
            }
            for item in related
            if item.get("native") is True
            and isinstance(item.get("native_source_id"), str)
            and isinstance(item.get("native_id"), str)
        ]
        return related, scope, native_refs, scope_fallback

    @staticmethod
    def _has_specific_scope(scope: Any) -> bool:
        values = [scope] if isinstance(scope, str) else scope if isinstance(scope, (list, tuple, set)) else []
        return any(isinstance(value, str) and value not in {"", "global", "unscoped"} for value in values)

    @staticmethod
    def _single_specific_scope(scope: Any) -> bool:
        values = [scope] if isinstance(scope, str) else list(scope) if isinstance(scope, (list, tuple, set)) else []
        return len(values) == 1 and isinstance(values[0], str) and values[0] not in {"", "global", "unscoped"}

    @staticmethod
    def _related_payload_size(value: Mapping[str, Any]) -> int:
        try:
            return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
        except (TypeError, ValueError, OverflowError):
            return -1

    @classmethod
    def _bound_related(
        cls,
        related: Iterable[Mapping[str, Any]],
        *,
        priority_memory_ids: Iterable[str] = (),
    ) -> list[dict[str, Any]]:
        """Keep model related-memory context within the processing budget.

        Update/duplicate targets are placed first, but every body is still
        bounded.  The serialized payload limit also covers metadata, so a
        large tag/alias list cannot bypass the body budget.
        """

        priority = {
            value.casefold()
            for value in priority_memory_ids
            if isinstance(value, str) and value
        }
        values: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for item in related:
            if not isinstance(item, Mapping):
                continue
            value = dict(item)
            memory_id = value.get("memory_id")
            if isinstance(memory_id, str):
                key = memory_id.casefold()
                if key in seen_ids:
                    continue
                seen_ids.add(key)
            values.append(value)
        values.sort(
            key=lambda value: (
                isinstance(value.get("memory_id"), str)
                and value["memory_id"].casefold() in priority,
            ),
            reverse=True,
        )
        selected: list[dict[str, Any]] = []
        used = 2  # The surrounding JSON array brackets.
        for value in values:
            if len(selected) >= _RELATED_MAX_ITEMS:
                break
            body = value.get("body")
            if isinstance(body, str) and len(body) > _RELATED_MAX_BODY_CHARS:
                value["body"] = body[: _RELATED_MAX_BODY_CHARS - 1].rstrip() + "…"
            size = cls._related_payload_size(value)
            if size < 0:
                continue
            additional = size + (1 if selected else 0)
            if used + additional > _RELATED_MAX_CHARS:
                # A priority target still gets a minimal, bounded view when
                # oversized metadata leaves no room for its normal payload.
                memory_id = value.get("memory_id")
                if not (
                    isinstance(memory_id, str)
                    and memory_id.casefold() in priority
                ):
                    continue
                minimal = {
                    key: value[key]
                    for key in ("memory_id", "title", "body", "type", "scopes")
                    if key in value
                }
                size = cls._related_payload_size(minimal)
                if size < 0 or used + size + (1 if selected else 0) > _RELATED_MAX_CHARS:
                    continue
                value = minimal
                additional = size + (1 if selected else 0)
            selected.append(value)
            used += additional
        return selected

    def _scope_records_unlocked(self, scope: Any) -> tuple[list[Any], bool]:
        """Read and rank active records once for a scoped fallback."""

        active_records = self.service._read_memories_unlocked("knowledge")
        scoped = filter_by_scope(
            [record.memory for record in active_records],
            scope,
            self.service.vault.config(),
        )
        ranks = {memory.memory_id.casefold(): rank for memory, rank in scoped}
        records = [
            record
            for record in active_records
            if record.memory.memory_id.casefold() in ranks
        ]
        records.sort(
            key=lambda record: (
                ranks[record.memory.memory_id.casefold()],
                record.memory.updated,
                record.memory.memory_id,
            ),
            reverse=True,
        )
        return records, len(records) > 1

    @classmethod
    def _scope_directory_entry(cls, memory: Memory) -> tuple[dict[str, Any], bool]:
        title = memory.title
        title_truncated = len(title) > _SCOPE_DIRECTORY_MAX_TITLE_CHARS
        if title_truncated:
            title = title[: _SCOPE_DIRECTORY_MAX_TITLE_CHARS - 1].rstrip() + "…"
        return (
            {
                "memory_id": memory.memory_id,
                "title": title,
                "type": memory.type,
                "scopes": list(memory.scopes),
            },
            title_truncated,
        )

    @classmethod
    def _scope_directory(
        cls,
        records: Iterable[Any],
    ) -> tuple[list[dict[str, Any]], bool]:
        """Return a bounded metadata-only directory from scoped records."""

        records = list(records)
        complete = len(records) <= _SCOPE_DIRECTORY_MAX_ITEMS
        directory: list[dict[str, Any]] = []
        used = 2
        for record in records[:_SCOPE_DIRECTORY_MAX_ITEMS]:
            entry, title_truncated = cls._scope_directory_entry(record.memory)
            complete = complete and not title_truncated
            try:
                size = len(json.dumps(entry, ensure_ascii=False, separators=(",", ":")))
            except (TypeError, ValueError, OverflowError):
                complete = False
                continue
            additional = size + (1 if directory else 0)
            if used + additional > _SCOPE_DIRECTORY_MAX_CHARS:
                complete = False
                break
            directory.append(entry)
            used += additional
        if len(records) > _SCOPE_DIRECTORY_MAX_ITEMS:
            complete = False
        return directory, complete

    def _related(
        self,
        turn: InboxTurn,
        state: Mapping[str, Any],
        explicit_scope: Any = None,
        *,
        overlay: Iterable[Mapping[str, Any]] = (),
    ) -> tuple[
        list[dict[str, Any]],
        Any,
        list[dict[str, str]],
        Optional[tuple[list[Any], bool]],
    ]:
        visible = " ".join(event.content for event in turn.events if isinstance(event.content, str)).strip()
        return self._related_query(
            turn,
            state,
            visible,
            explicit_scope,
            overlay=overlay,
            strict_relevance=True,
        )

    def _target_is_relevant_to_candidate(
        self,
        turn: InboxTurn,
        candidate: Mapping[str, Any],
        *,
        scope_directory: Optional[list[dict[str, Any]]] = None,
        scope_directory_complete: bool = True,
    ) -> bool:
        """Check a model-selected target against the candidate topic only.

        The ordinary related-memory prompt may include several independent
        items from one mailbox turn.  Target selection must not inherit that
        aggregate relevance: only the candidate's own extracted topic may
        make an active target eligible.  A complete scope directory remains a
        bounded escape hatch for an explicitly indirect user/session scope;
        it never turns the target into an unconditional priority hit for a
        direct model-attributed candidate.
        """

        target = next(
            (
                candidate.get(field)
                for field in ("duplicate_memory_id", "update_memory_id")
                if isinstance(candidate.get(field), str) and candidate.get(field)
            ),
            None,
        )
        memory = candidate.get("memory")
        candidate_scopes = candidate.get("scopes")
        if (
            not isinstance(target, str)
            or not isinstance(memory, str)
            or not isinstance(candidate_scopes, list)
        ):
            return False

        target_key = target.casefold()
        candidate_scope_source = candidate.get("scope_source")

        # A complete bounded directory is the existing indirect resolution
        # path.  It contains only active IDs from the requested inherited
        # scope, so preserve its exact-ID selection semantics rather than
        # forcing a body relevance check that the directory intentionally
        # cannot provide.
        if scope_directory is not None:
            if not scope_directory_complete:
                return False
            selected = next(
                (
                    item
                    for item in scope_directory
                    if isinstance(item, Mapping)
                    and isinstance(item.get("memory_id"), str)
                    and item["memory_id"].casefold() == target_key
                ),
                None,
            )
            if selected is not None and selected.get("type") == candidate.get("type"):
                return True

        # Model-attributed, directly named scopes must match the active
        # target's stable topic title.  Matching the whole old body is too
        # permissive: two different project memories can share implementation
        # details such as milestones while serving different future uses.
        if candidate_scope_source == "model":
            scope_terms: list[str] = []
            try:
                scope_config = self.service.vault.config()
                configured_scopes = scope_config.get("scopes", {})
            except (OSError, UnicodeError, ValueError, TypeError):
                scope_config = {}
                configured_scopes = {}
            for candidate_scope in candidate_scopes:
                if not isinstance(candidate_scope, str) or not candidate_scope.startswith("project:"):
                    continue
                scope_terms.append(candidate_scope.partition(":")[2])
                metadata = (
                    configured_scopes.get(candidate_scope)
                    if isinstance(configured_scopes, Mapping)
                    else None
                )
                aliases = metadata.get("aliases") if isinstance(metadata, Mapping) else None
                if isinstance(aliases, list):
                    scope_terms.extend(
                        alias for alias in aliases if isinstance(alias, str) and alias
                    )

            # The regression is project-scope drift inside aggregate turns.
            # Global/domain/portfolio targets keep their established semantic
            # gate behavior because there is no project identity to remove
            # before comparing stable topics.
            if not scope_terms:
                return True

            visible_turn = " ".join(
                event.content for event in turn.events if isinstance(event.content, str)
            )
            if scope_terms and not any(
                normalize_term(term) in normalize_term(visible_turn) for term in scope_terms
            ):
                # The visible turn is elliptical (for example, "this
                # project"); the inherited scope and bounded related context
                # already resolved its target.
                return True

            target_memory = None
            with self.service.vault.lock():
                for record in self.service._read_memories_unlocked("knowledge"):
                    if record.memory.memory_id.casefold() == target_key:
                        target_memory = record.memory
                        break
            if target_memory is None:
                for item in self._planned_related:
                    if (
                        isinstance(item, Mapping)
                        and isinstance(item.get("memory_id"), str)
                        and item["memory_id"].casefold() == target_key
                    ):
                        try:
                            target_memory = Memory.from_mapping(item)
                        except (TypeError, ValueError):
                            target_memory = None
                        break
            if target_memory is None:
                return False
            if not filter_by_scope([target_memory], candidate_scopes, scope_config):
                return False

            def without_scope_terms(value: str) -> str:
                result = value
                for term in sorted(set(scope_terms), key=len, reverse=True):
                    result = re.sub(re.escape(term), "", result, flags=re.IGNORECASE)
                return result.strip()

            topic_title = without_scope_terms(target_memory.title)
            topic_query = without_scope_terms(memory)
            if not topic_title or not topic_query:
                return False
            topic_memory = Memory(
                memory_id=target_memory.memory_id,
                title=topic_query,
                body="",
                type=target_memory.type,
                scopes=target_memory.scopes,
            )
            return candidate_matches_query(topic_memory, topic_title)

        # User/session scopes are authoritative indirect context.  The base
        # parser has already limited the target to an active related memory.
        return candidate_scope_source in {"user", "session_context"}

    @staticmethod
    def _project_scope_keys(scopes: Any) -> set[str]:
        if not isinstance(scopes, list):
            return set()
        return {
            scope.casefold()
            for scope in scopes
            if isinstance(scope, str) and scope.startswith("project:")
        }

    @staticmethod
    def _is_project_plan_title(value: Any) -> bool:
        text = normalize_term(value) if isinstance(value, str) else ""
        return any(
            marker in text
            for marker in ("实施计划", "项目计划", "implementation plan", "project plan")
        )

    @staticmethod
    def _is_adjacent_plan_record(value: Any) -> bool:
        text = normalize_term(value) if isinstance(value, str) else ""
        return any(
            marker in text
            for marker in (
                "已发送", "发送", "邮件", "附件", "存档", "会议", "启动会", "纪要",
                "sent", "email", "mail", "attachment", "archive", "meeting", "minutes",
            )
        )

    def _infer_update_target(
        self,
        candidate: Mapping[str, Any],
        related: Iterable[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Reuse one unambiguously matching project plan/constraint memory.

        The gate remains authoritative when it names a target.  This narrow
        fallback handles a common model omission: a project-plan candidate is
        emitted as a new ``fact`` without ``update_memory_id`` even though the
        same scoped plan is already active.  The active target's type is the
        canonical type; it is copied before summary validation so the target
        type invariant remains enforced.
        """

        result = dict(candidate)
        if (
            not result.get("worth")
            or result.get("duplicate")
            or any(result.get(field) for field in ("duplicate_memory_id", "update_memory_id"))
            or not isinstance(result.get("memory"), str)
        ):
            return result
        if result.get("type") not in {"fact", "project"}:
            return result
        if not self._is_project_plan_title(result["memory"]):
            return result
        project_keys = self._project_scope_keys(result.get("scopes"))
        if len(project_keys) != 1:
            return result

        project_matches: list[Memory] = []
        fact_matches: list[Memory] = []
        seen: set[str] = set()
        for item in related:
            if not isinstance(item, Mapping) or item.get("native") is True:
                continue
            memory_id = item.get("memory_id")
            item_type = item.get("type")
            item_scopes = item.get("scopes")
            if (
                not isinstance(memory_id, str)
                or not isinstance(item_type, str)
                or item_type not in {"fact", "project"}
                or self._project_scope_keys(item_scopes) != project_keys
                or memory_id.casefold() in seen
                or not self._is_project_plan_title(item.get("title"))
                or self._is_adjacent_plan_record(item.get("title"))
            ):
                continue
            try:
                target = Memory.from_mapping(item)
            except (TypeError, ValueError):
                continue
            seen.add(memory_id.casefold())
            if target.type == "project":
                project_matches.append(target)
            else:
                fact_matches.append(target)

        # Prefer a project target over a same-topic fact.  If there are
        # multiple project targets, defer rather than creating a sibling by
        # guessing; the same conservative rule applies when only multiple
        # durable fact targets remain.
        if len(project_matches) > 1 or (not project_matches and len(fact_matches) > 1):
            result["_defer_reason"] = "ambiguous_update_target"
            return result
        if project_matches:
            target = project_matches[0]
        elif len(fact_matches) == 1:
            target = fact_matches[0]
        else:
            return result
        result["update_memory_id"] = target.memory_id
        # The update target's type is immutable.  Correct a model's fact-vs-
        # project label only after the same-use target was identified.
        result["type"] = target.type
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

    @staticmethod
    def _planned_memory(request: Mapping[str, Any]) -> Optional[dict[str, Any]]:
        """Project a not-yet-committed request into the next turn's lookup."""

        summary = request.get("summary")
        if not isinstance(summary, Mapping) or request.get("duplicate_memory_id") is not None:
            return None
        target_id = summary.get("update_memory_id")
        memory_id = target_id if isinstance(target_id, str) else request.get("memory_id")
        if not isinstance(memory_id, str) or not memory_id:
            return None
        body = summary.get("body")
        title = summary.get("title")
        scopes = summary.get("scopes")
        if not isinstance(body, str) or not isinstance(title, str) or not isinstance(scopes, list):
            return None
        value: dict[str, Any] = {
            "memory_id": memory_id,
            "title": title,
            "body": body,
            "tags": list(summary.get("tags", [])) if isinstance(summary.get("tags", []), list) else [],
            "type": summary.get("type", "other"),
            "scopes": list(scopes),
            "aliases": list(summary.get("aliases", [])) if isinstance(summary.get("aliases", []), list) else [],
            "keywords": list(summary.get("keywords", [])) if isinstance(summary.get("keywords", []), list) else [],
        }
        return value

    def _request(
        self,
        summary: Mapping[str, Any],
        turn: InboxTurn,
        *,
        candidate_id: str,
        conversation_title: str,
        event_key_value: Optional[str] = None,
        turn_id: str = "",
        explicit_remember: bool = False,
        native_refs: Iterable[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        evidence = summary.get("evidence_event_ids", [])
        if not isinstance(evidence, list) or not evidence:
            evidence = list(turn.event_keys)
        memory_id = MemoryWriter.deterministic_memory_id(
            source=turn.source,
            session_id=turn.session_id,
            turn_key=turn.turn_key or turn_key(turn_id or candidate_id),
            candidate_id=candidate_id,
            evidence_event_ids=evidence,
        )
        return {
            "summary": dict(summary),
            "turn": turn,
            "candidate_id": candidate_id,
            "memory_id": memory_id,
            "event_key": event_key_value or (turn.event_keys[0] if turn.event_keys else ""),
            "turn_id": turn_id,
            "conversation_title": conversation_title,
            "explicit_remember": explicit_remember,
            "native_refs": [dict(item) for item in native_refs if isinstance(item, Mapping)],
        }

    def _duplicate_request(
        self,
        candidate: Mapping[str, Any],
        turn: InboxTurn,
        *,
        conversation_title: str,
        native_refs: Iterable[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Turn a validated duplicate candidate into a metadata-only write."""

        duplicate_id = candidate.get("duplicate_memory_id")
        if not isinstance(duplicate_id, str) or not duplicate_id:
            raise ProcessingError("invalid duplicate memory target")
        summary = {
            "title": "",
            "body": "",
            "tags": [],
            "type": candidate.get("type"),
            "scopes": list(candidate.get("scopes", [])),
            "scope_source": candidate.get("scope_source"),
            "sources": [],
            "scope_operations": [],
        }
        return {
            "summary": summary,
            "turn": turn,
            "candidate_id": str(candidate["candidate_id"]),
            "memory_id": duplicate_id,
            "duplicate_memory_id": duplicate_id,
            "event_key": turn.event_keys[0] if turn.event_keys else "",
            "turn_id": "",
            "conversation_title": conversation_title,
            "explicit_remember": False,
            "native_refs": [dict(item) for item in native_refs if isinstance(item, Mapping)],
        }

    def _collect_turn_outputs(
        self,
        backend: Any,
        turn: InboxTurn,
        state: Mapping[str, Any],
        *,
        explicit: bool = False,
        explicit_candidate: Optional[Mapping[str, Any]] = None,
        scope: Any = None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        events = _event_payload(turn)
        turn_ref = (turn.source, turn.session_id, turn.turn_key)
        self._deferred_by_turn.setdefault(turn_ref, [])
        related, scope_background, native_refs, scope_fallback = self._related(
            turn,
            state,
            scope,
            overlay=self._planned_related,
        )
        # When a compressed turn has no lexical hit but inherits one concrete
        # scope containing several active memories, expose only a bounded
        # metadata directory to the gate.  Bodies stay out of this first
        # decision; the selected ID is re-read for summarize below.
        scope_directory: Optional[list[dict[str, Any]]] = None
        scope_directory_complete = True
        scope_ambiguous = False
        scoped_records: Optional[list[Any]] = None
        gate_related = related
        related_native_ids = [item["native_id"] for item in native_refs]
        related_memory_ids = [
            item["memory_id"]
            for item in related
            if item.get("native") is not True and isinstance(item.get("memory_id"), str)
        ]
        gate_related_memory_ids = list(related_memory_ids)
        gate_related_memory_types = {
            item["memory_id"].casefold(): item["type"]
            for item in gate_related
            if isinstance(item, Mapping)
            and isinstance(item.get("memory_id"), str)
            and isinstance(item.get("type"), str)
        }
        if scope_fallback is not None:
            scoped_records, scope_ambiguous = scope_fallback
            if scope_ambiguous and self._single_specific_scope(scope_background):
                scope_directory, scope_directory_complete = self._scope_directory(scoped_records)
                gate_related = []
                for entry in scope_directory:
                    memory_id = entry.get("memory_id")
                    if isinstance(memory_id, str) and memory_id.casefold() not in {
                        value.casefold() for value in gate_related_memory_ids
                    }:
                        gate_related_memory_ids.append(memory_id)
                    if (
                        isinstance(memory_id, str)
                        and isinstance(entry.get("type"), str)
                    ):
                        gate_related_memory_types[memory_id.casefold()] = entry["type"]
        scope_registry = self._scope_registry_projection()
        with self.service.vault.lock():
            validation_scope_registry = self.service.vault.config().get("scopes", {})
        title = self._conversation_title(turn)
        if explicit:
            candidate = dict(explicit_candidate or {})
            summary = self._complete_json_stage(
                backend,
                summarize_prompt(
                    candidate,
                    events,
                    explicit=True,
                    related_memories=related,
                    scope_background=scope_background,
                    scope_registry=scope_registry,
                ),
                system=SUMMARIZE_SYSTEM,
                purpose="summarize",
                parser=lambda raw: parse_summarize_output(
                    _normalize_summary_dates(raw, turn, candidate),
                    current_event_keys=turn.event_keys,
                    related_native_ids=related_native_ids,
                    related_memory_ids=related_memory_ids,
                    scope_registry=validation_scope_registry,
                    allow_no_change=False,
                ),
                diagnostic_context={
                    "source": turn.source,
                    "session_id": turn.session_id,
                    "turn_index": turn.turn_index,
                },
            )
            return (
                [
                    self._request(
                        summary,
                        turn,
                        candidate_id=str(candidate["candidate_id"]),
                        conversation_title=title,
                        explicit_remember=True,
                        native_refs=native_refs,
                    )
                ],
                list(summary["scopes"]),
            )

        gate_attempt_count = 0

        def parse_gate(raw: str) -> dict[str, Any]:
            nonlocal gate_attempt_count
            gate_attempt_count += 1
            parsed = parse_gate_output(
                raw,
                current_event_keys=turn.event_keys,
                related_memory_ids=gate_related_memory_ids,
                related_memory_types=gate_related_memory_types,
                scope_registry=validation_scope_registry,
                enforce_model_scope_grounding=gate_attempt_count < 3,
            )
            if gate_attempt_count >= 3:
                candidates = []
                for candidate in parsed["candidates"]:
                    candidate_scopes = [
                        item
                        for item in candidate.get("scopes", [])
                        if isinstance(item, str)
                    ]
                    if (
                        candidate.get("scope_source") == "model"
                        and candidate.get("worth") is True
                        and any(item.partition(":")[0] == "project" for item in candidate_scopes)
                    ):
                        selected_owners, matches = _model_scope_grounding_evidence(
                            str(candidate.get("memory", "")),
                            candidate_scopes,
                            validation_scope_registry,
                        )
                        candidate = dict(candidate)
                        if not selected_owners:
                            candidates.append(candidate)
                            continue
                        if len(matches) == 1:
                            grounded_scope = next(iter(matches.values()))
                            if grounded_scope.casefold() not in set(selected_owners.values()):
                                non_project_scopes = [
                                    item
                                    for item in candidate_scopes
                                    if item.partition(":")[0] != "project"
                                ]
                                if all(
                                    item.casefold() != grounded_scope.casefold()
                                    for item in non_project_scopes
                                ):
                                    non_project_scopes.append(grounded_scope)
                                candidate["scopes"] = non_project_scopes
                        else:
                            candidate["scopes"] = ["unscoped"]
                            candidate["scope_source"] = "insufficient_context"
                    candidates.append(candidate)
                parsed = dict(parsed)
                parsed["candidates"] = candidates

            invalid_targets: dict[str, set[str]] = {}
            for candidate in parsed["candidates"]:
                target_fields = {
                    field
                    for field in ("duplicate_memory_id", "update_memory_id")
                    if isinstance(candidate.get(field), str) and candidate.get(field)
                }
                if target_fields and not self._target_is_relevant_to_candidate(
                    turn,
                    candidate,
                    scope_directory=scope_directory,
                    scope_directory_complete=scope_directory_complete,
                ):
                    invalid_targets[candidate["candidate_id"].casefold()] = target_fields

            if invalid_targets and gate_attempt_count < 3:
                raise ModelOutputError(
                    "selected target is not relevant to the candidate topic",
                    validation_detail="target_not_relevant",
                )

            if invalid_targets:
                # A persistently non-converging model must not hold an entire
                # inbox turn hostage.  An unrelated update target can safely
                # become an independent CREATE candidate; an unrelated
                # duplicate has no independent fact to write and is dropped.
                candidates: list[dict[str, Any]] = []
                for candidate in parsed["candidates"]:
                    fields = invalid_targets.get(candidate["candidate_id"].casefold())
                    if not fields:
                        candidates.append(candidate)
                        continue
                    if "duplicate_memory_id" in fields:
                        continue
                    if "update_memory_id" in fields and candidate.get("worth"):
                        independent = dict(candidate)
                        independent.pop("update_memory_id", None)
                        candidates.append(independent)
                parsed = dict(parsed)
                parsed["candidates"] = candidates
            return parsed

        gate = self._complete_json_stage(
            backend,
            gate_prompt(
                events,
                related_memories=gate_related,
                scope_directory=scope_directory,
                scope_directory_complete=scope_directory_complete,
                scope_background=scope_background,
                scope_registry=scope_registry,
            ),
            system=GATE_SYSTEM,
            purpose="gate",
            parser=parse_gate,
            diagnostic_context={
                "source": turn.source,
                "session_id": turn.session_id,
                "turn_index": turn.turn_index,
            },
        )
        requests: list[dict[str, Any]] = []
        observed_scopes: list[str] = []
        for candidate in gate["candidates"]:
            # A combined mailbox/daily digest is not an atomic memory.  If a
            # concrete action was worth retaining, the gate must emit it as
            # its own candidate; the aggregate shell itself is NO_CHANGE.
            if candidate.get("worth") and is_aggregate_operational_text(candidate.get("memory")):
                continue
            candidate_scopes = list(candidate["scopes"])
            # An automatic candidate with no reliable project attribution is
            # retained as a retryable inbox turn, never silently promoted to
            # global knowledge.  The processed ledger records only a compact
            # marker; the complete candidate remains in the inbox.
            if candidate["worth"] and (
                candidate_scopes == ["unscoped"]
                or candidate.get("scope_source") == "insufficient_context"
            ):
                self._defer_candidate(turn_ref, candidate, "scope_required", scopes=candidate_scopes)
                continue

            has_target = any(
                isinstance(candidate.get(field), str) and candidate.get(field)
                for field in ("duplicate_memory_id", "update_memory_id")
            )
            if scope_directory is not None and not scope_directory_complete and (
                candidate["worth"] or has_target
            ):
                self._defer_candidate(
                    turn_ref,
                    candidate,
                    "scope_directory_incomplete",
                    scopes=candidate_scopes,
                )
                continue

            if candidate["worth"] and scope_ambiguous and not has_target:
                self._defer_candidate(
                    turn_ref,
                    candidate,
                    "related_ambiguous",
                    scopes=candidate_scopes,
                )
                continue

            candidate_related, candidate_scope_background, candidate_native_refs, _ = self._related_query(
                turn,
                state,
                str(candidate.get("memory", "")).strip(),
                candidate_scopes,
                overlay=self._planned_related,
                priority_memory_ids=[
                    candidate.get("duplicate_memory_id"),
                    candidate.get("update_memory_id"),
                ],
                priority_only=scope_directory is not None,
                scope_records=scoped_records if scope_directory is not None else None,
            )
            candidate = self._infer_update_target(candidate, candidate_related)
            defer_reason = candidate.pop("_defer_reason", None)
            if defer_reason:
                self._defer_candidate(turn_ref, candidate, defer_reason)
                continue
            candidate_native_ids = [item["native_id"] for item in candidate_native_refs]
            candidate_memory_ids = [
                item["memory_id"]
                for item in candidate_related
                if item.get("native") is not True and isinstance(item.get("memory_id"), str)
            ]
            candidate_id_set = {item.casefold() for item in candidate_memory_ids}
            for target_field in ("duplicate_memory_id", "update_memory_id"):
                target = candidate.get(target_field)
                if target is not None and (
                    not isinstance(target, str) or target.casefold() not in candidate_id_set
                ):
                    raise ModelOutputError(
                        f"{target_field} is not a related active memory for this candidate",
                        validation_detail=(
                            "invalid_duplicate_target"
                            if target_field == "duplicate_memory_id"
                            else "invalid_update_target"
                        ),
                    )

            if candidate["duplicate"] or not candidate["worth"]:
                duplicate_memory_id = candidate.get("duplicate_memory_id")
                if duplicate_memory_id is not None:
                    requests.append(
                        self._duplicate_request(
                            candidate,
                            turn,
                            conversation_title=title,
                            native_refs=candidate_native_refs,
                        )
                    )
                    # Automatic duplicate observations are metadata no-ops;
                    # only the already-active target's scopes are trustworthy
                    # session context, never a transient model-provided scope.
                    duplicate_scopes = next(
                        (
                            item.get("scopes")
                            for item in candidate_related
                            if isinstance(item, Mapping)
                            and isinstance(item.get("memory_id"), str)
                            and item["memory_id"].casefold() == duplicate_memory_id.casefold()
                            and isinstance(item.get("scopes"), list)
                        ),
                        [],
                    )
                    for observed_scope in duplicate_scopes:
                        if (
                            isinstance(observed_scope, str)
                            and observed_scope != "unscoped"
                            and observed_scope not in observed_scopes
                        ):
                            observed_scopes.append(observed_scope)
                continue

            gate_update_target = candidate.get("update_memory_id")
            gate_target_type = None
            if isinstance(gate_update_target, str):
                gate_target_key = gate_update_target.casefold()
                gate_target_type = next(
                    (
                        item.get("type")
                        for item in candidate_related
                        if isinstance(item, Mapping)
                        and isinstance(item.get("memory_id"), str)
                        and item["memory_id"].casefold() == gate_target_key
                        and isinstance(item.get("type"), str)
                    ),
                    None,
                )

            try:
                summary = self._complete_json_stage(
                    backend,
                    summarize_prompt(
                        candidate,
                        events,
                        related_memories=candidate_related,
                        scope_background=candidate_scope_background,
                        scope_registry=scope_registry,
                    ),
                    system=SUMMARIZE_SYSTEM,
                    purpose="summarize",
                    parser=lambda raw: parse_summarize_output(
                        _normalize_summary_dates(raw, turn, candidate),
                        current_event_keys=turn.event_keys,
                        related_native_ids=candidate_native_ids,
                        related_memory_ids=candidate_memory_ids,
                        scope_registry=validation_scope_registry,
                        expected_scopes=candidate["scopes"],
                        expected_scope_source=candidate["scope_source"],
                        allow_no_change=True,
                        expected_type=(
                            candidate.get("type")
                            if (
                                gate_update_target is not None
                                and gate_target_type == candidate.get("type")
                            )
                            else None
                        ),
                        expected_update_memory_id=gate_update_target,
                        expected_target_type=(
                            gate_target_type
                            if gate_target_type == candidate.get("type")
                            else None
                        ),
                    ),
                    diagnostic_context={
                        "source": turn.source,
                        "session_id": turn.session_id,
                        "turn_index": turn.turn_index,
                    },
                )
            except ModelOutputError as error:
                if getattr(error, "validation_detail", None) != "relative_time":
                    raise
                # The candidate's source turn remains in inbox for an
                # explicit retry.  Other candidates from this same turn may
                # still commit safely in the same transaction.
                self._defer_candidate(
                    turn_ref,
                    candidate,
                    "relative_time",
                    scopes=candidate["scopes"],
                )
                continue
            if summary.get("decision") == NO_CHANGE_DECISION:
                continue
            if gate_update_target is not None:
                summary_update_target = summary.get("update_memory_id")
                if summary_update_target is None:
                    summary = dict(summary)
                    summary["update_memory_id"] = gate_update_target
                elif (
                    not isinstance(summary_update_target, str)
                    or summary_update_target.casefold() != gate_update_target.casefold()
                ):
                    raise ModelOutputError(
                        "summary update target differs from gate target",
                        validation_detail="invalid_update_target",
                    )
                else:
                    summary = dict(summary)
                    summary["update_memory_id"] = gate_update_target
            if summary["scopes"] == ["unscoped"] or summary.get("scope_source") == "insufficient_context":
                self._defer_candidate(
                    turn_ref,
                    candidate,
                    "scope_required",
                    scopes=summary["scopes"],
                    scope_source=summary.get("scope_source"),
                )
                continue
            requests.append(
                self._request(
                    summary,
                    turn,
                    candidate_id=str(candidate["candidate_id"]),
                    conversation_title=title,
                    native_refs=candidate_native_refs,
                )
            )
            for observed_scope in summary["scopes"]:
                if (
                    isinstance(observed_scope, str)
                    and observed_scope != "unscoped"
                    and observed_scope not in observed_scopes
                ):
                    observed_scopes.append(observed_scope)
        return requests, observed_scopes

    def _state_for_snapshot_unlocked(self, snapshot: _Snapshot, processed: Mapping[str, Any]) -> Mapping[str, Any]:
        sessions = processed.get("sessions", {})
        state = sessions.get(snapshot.state_key) if isinstance(sessions, Mapping) else None
        if not isinstance(state, Mapping):
            return {}

        # Compression rotates the physical session before the next visible
        # turn.  Resolve only a missing child scope through the persisted
        # parent chain; an explicit child scope remains authoritative.
        child_scope = _safe_scope_background(state)
        if child_scope:
            return state
        current = state
        visited = {snapshot.state_key}
        for _ in range(_MAX_SESSION_LINEAGE_DEPTH):
            parent_session_id = current.get("lineage_parent_session_id")
            if not isinstance(parent_session_id, str) or not parent_session_id:
                break
            try:
                parent_session_id = safe_component(parent_session_id, "parent session id")
            except ValueError:
                break
            parent_key = _session_key(snapshot.turn.source, parent_session_id)
            if parent_key in visited:
                break
            visited.add(parent_key)
            parent_state = sessions.get(parent_key) if isinstance(sessions, Mapping) else None
            if not isinstance(parent_state, Mapping):
                break
            parent_scope = _safe_scope_background(parent_state)
            if parent_scope:
                inherited = dict(state)
                inherited["scopes"] = list(parent_scope) if isinstance(parent_scope, list) else parent_scope
                return inherited
            current = parent_state
        return state

    @staticmethod
    def _processed_memory_ids(processed: Mapping[str, Any], event_key_value: str) -> Optional[list[str]]:
        sessions = processed.get("sessions")
        if not isinstance(sessions, Mapping):
            return None
        for state in sessions.values():
            if not isinstance(state, Mapping):
                continue
            entries = state.get("processed_turns")
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, Mapping):
                    continue
                keys = entry.get("event_keys")
                if isinstance(keys, list) and any(
                    isinstance(key, str) and key.casefold() == event_key_value.casefold()
                    for key in keys
                ):
                    ids = entry.get("memory_ids", [])
                    return [item for item in ids if isinstance(item, str)] if isinstance(ids, list) else []
        return None

    def _deferred_counts(
        self,
        *,
        source: str | None = None,
        session_id: str | None = None,
    ) -> tuple[int, int]:
        """Return unresolved scope candidates in the requested session scope."""

        candidates = 0
        turns = 0
        with self.service.vault.lock():
            processed = _read_processed(self.service.vault.processed_index_path)
        sessions = processed.get("sessions")
        if not isinstance(sessions, Mapping):
            return 0, 0
        for state_key, state in sessions.items():
            if not isinstance(state_key, str):
                continue
            if source is not None and not state_key.startswith(f"{source}/"):
                continue
            if session_id is not None:
                state_source, separator, state_session = state_key.partition("/")
                if not separator or state_session != session_id or (
                    source is not None and state_source != source
                ):
                    continue
            if not isinstance(state, Mapping):
                continue
            entries = state.get("processed_turns")
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, Mapping):
                    continue
                deferred = entry.get("deferred_candidates")
                if isinstance(deferred, list) and deferred:
                    turns += 1
                    candidates += sum(1 for item in deferred if isinstance(item, Mapping))
        return candidates, turns

    def _commit_success(
        self,
        snapshots: list[_Snapshot],
        requests: list[Mapping[str, Any]],
        *,
        now: str,
        cleanup_hours: int,
        observed_scopes: Mapping[tuple[str, str, str], Iterable[str]] | None = None,
        deferred_candidates: Mapping[tuple[str, str, str], Iterable[Mapping[str, Any]]] | None = None,
    ) -> list[str]:
        if not snapshots:
            return []
        by_snapshot: dict[str, list[Mapping[str, Any]]] = {}
        for request in requests:
            turn = request["turn"]
            key = _session_key(turn.source, turn.session_id)
            by_snapshot.setdefault(key, []).append(request)
        with self.service.vault.lock():
            processed = _read_processed(self.service.vault.processed_index_path)
            sessions = processed.setdefault("sessions", {})
            for snapshot in snapshots:
                state = sessions.get(snapshot.state_key)
                marker = state.get("processing") if isinstance(state, Mapping) else None
                if not isinstance(marker, Mapping) or marker.get("token") != snapshot.token:
                    raise ProcessingError("processing ownership changed")
            written: list[Memory] = []
            all_requests = list(requests)
            claimed_native: dict[str, str] = {}
            for request in all_requests:
                summary = request.get("summary", {})
                shadow_ids = summary.get("shadow_native_ids", [])
                refs = request.get("native_refs", [])
                ref_ids = {
                    item.get("native_id").casefold()
                    for item in refs
                    if isinstance(item, Mapping) and isinstance(item.get("native_id"), str)
                }
                if not isinstance(shadow_ids, list) or any(
                    not isinstance(native_id, str) or native_id.casefold() not in ref_ids
                    for native_id in shadow_ids
                ):
                    raise ProcessingError("shadow native reference is not related to this turn")
                for native_id in shadow_ids:
                    key = native_id.casefold()
                    if key in claimed_native:
                        raise ProcessingError("native segment is shadowed more than once in one batch")
                    claimed_native[key] = request["memory_id"]

            scope_values: list[str] = []
            session_scope_updates: dict[str, list[str]] = {}
            scope_operations: list[Mapping[str, Any]] = []
            for snapshot in snapshots:
                scope_key = (
                    snapshot.turn.source,
                    snapshot.turn.session_id,
                    snapshot.turn.turn_key,
                )
                values = list((observed_scopes or {}).get(scope_key, []))
                if values:
                    session_scope_updates[snapshot.state_key] = values
                    for observed_scope in values:
                        if observed_scope not in scope_values:
                            scope_values.append(observed_scope)
            for request in all_requests:
                operations = request.get("summary", {}).get("scope_operations", [])
                if not isinstance(operations, list):
                    raise ProcessingError("invalid scope operations")
                scope_operations.extend(
                    operation for operation in operations if isinstance(operation, Mapping)
                )

            prepared_scopes = None
            if scope_operations:
                try:
                    prepared_scopes = ScopeMaintainer(self.service).prepare(
                        self.service.vault.config(),
                        operations=scope_operations,
                        observed_scopes=scope_values,
                        session_scopes=session_scope_updates,
                    )
                except (OSError, UnicodeError, ValueError, TypeError, ScopeMaintenanceError) as error:
                    raise ProcessingError("scope maintenance preflight failed") from error
                # Feed the canonical post-operation scopes to the writer too.
                # Otherwise a retry could briefly rewrite an already-migrated
                # active memory with the old source scope and create history.
                for request in all_requests:
                    summary = request.get("summary")
                    if not isinstance(summary, dict):
                        continue
                    values = summary.get("scopes")
                    if not isinstance(values, list):
                        continue
                    migrated: list[str] = []
                    for value in values:
                        current = value
                        seen: set[str] = set()
                        while current in prepared_scopes.migrations:
                            if current in seen:
                                raise ProcessingError("scope migration contains a cycle")
                            seen.add(current)
                            current = prepared_scopes.migrations[current]
                        if current not in migrated:
                            migrated.append(current)
                    summary["scopes"] = migrated
            if all_requests:
                written = self.writer.write_many_unlocked(all_requests, now=now)
            else:
                self.writer.last_metadata_merged = 0
                self.writer.last_noop_memory_ids = set()
            noop_memory_ids = self.writer.last_noop_memory_ids
            if prepared_scopes is not None:
                try:
                    ScopeMaintainer(self.service).apply_unlocked(
                        processed,
                        prepared_scopes,
                        session_scope_updates,
                    )
                except (OSError, UnicodeError, ValueError, TypeError, ScopeMaintenanceError) as error:
                    raise ProcessingError("scope maintenance commit failed") from error
            else:
                self.service._rebuild_index_unlocked()
                if scope_values:
                    try:
                        config = self.service.vault.config()
                        updated_config = register_scope_nodes(config, scope_values)
                    except ScopeError as error:
                        raise ProcessingError("invalid observed session scope") from error
                    if updated_config != config:
                        save_config(self.service.vault.config_path, updated_config)

            native_indexer = NativeIndexer(self.service.vault)
            for request, memory in zip(all_requests, written):
                shadow_ids = request["summary"].get("shadow_native_ids", [])
                if not shadow_ids:
                    continue
                # A prior attempt may have persisted the memory but failed
                # while applying its native shadow.  Re-apply that side
                # effect on retry even when the memory request itself was
                # recognized as already applied.
                shadow_keys = {value.casefold() for value in shadow_ids}
                refs = [
                    item
                    for item in request.get("native_refs", [])
                    if isinstance(item, Mapping)
                    and isinstance(item.get("native_id"), str)
                    and item["native_id"].casefold() in shadow_keys
                ]
                native_indexer.apply_shadow_unlocked(refs, memory.memory_id)
            processed = _read_processed(self.service.vault.processed_index_path)
            sessions = processed.setdefault("sessions", {})
            memory_ids_by_turn: dict[tuple[str, str], list[str]] = {}
            for request, memory in zip(all_requests, written):
                if request.get("memory_id") in noop_memory_ids:
                    continue
                turn = request["turn"]
                memory_ids_by_turn.setdefault((turn.source, turn.session_id, turn.turn_key), []).append(memory.memory_id)
            for snapshot in snapshots:
                state = sessions.get(snapshot.state_key)
                if not isinstance(state, dict):
                    raise ProcessingError("processing session disappeared")
                marker = state.get("processing")
                if not isinstance(marker, Mapping) or marker.get("token") != snapshot.token:
                    raise ProcessingError("processing ownership changed")
                entries = state.get("processed_turns")
                if not isinstance(entries, list):
                    entries = []
                existing_entry = next(
                    (
                        entry
                        for entry in entries
                        if isinstance(entry, Mapping) and entry.get("turn_key") == snapshot.turn.turn_key
                    ),
                    None,
                )
                ids = memory_ids_by_turn.get(
                    (snapshot.turn.source, snapshot.turn.session_id, snapshot.turn.turn_key), []
                )
                if isinstance(existing_entry, dict):
                    entry = existing_entry
                    entry["memory_ids"] = sorted(set(entry.get("memory_ids", []) + ids))
                else:
                    entry = {
                        "turn_key": snapshot.turn.turn_key,
                        "turn_index": snapshot.turn.turn_index,
                        "event_keys": list(snapshot.turn.event_keys),
                        "processed_at": now,
                        "eligible_cleanup_at": _add_hours(now, cleanup_hours),
                        "memory_ids": sorted(set(ids)),
                    }
                    entries.append(entry)
                scope_key = (
                    snapshot.turn.source,
                    snapshot.turn.session_id,
                    snapshot.turn.turn_key,
                )
                deferred_values = list((deferred_candidates or {}).get(scope_key, []))
                deferred_values = [
                    dict(item)
                    for item in deferred_values
                    if isinstance(item, Mapping)
                ]
                if deferred_values:
                    # Keep the complete source turn available for a later
                    # explicit-scope retry.  A missing cleanup timestamp is
                    # intentional: deleting the inbox would discard the
                    # unresolved candidate before it can be retried.
                    entry["deferred_candidates"] = deferred_values
                    entry["eligible_cleanup_at"] = None
                    entry.pop("cleanup_done_at", None)
                else:
                    entry.pop("deferred_candidates", None)
                    if not entry.get("cleanup_done_at"):
                        entry["eligible_cleanup_at"] = _add_hours(now, cleanup_hours)
                state["processed_turns"] = entries
                watermark = max(_as_int(state.get("watermark"), 0), _as_int(snapshot.turn.turn_index, 0))
                state["watermark"] = watermark
                state["processed_watermark"] = watermark
                if prepared_scopes is not None and snapshot.state_key in prepared_scopes.session_scopes:
                    current_scopes = list(prepared_scopes.session_scopes[snapshot.state_key])
                else:
                    current_scopes = list((observed_scopes or {}).get(scope_key, []))
                if current_scopes:
                    state["scopes"] = current_scopes
                sessions[snapshot.state_key] = state
            for state_key in {snapshot.state_key for snapshot in snapshots}:
                state = sessions.get(state_key)
                if isinstance(state, dict):
                    state["processing"] = {"status": _IDLE_STATUS, "last_processed_at": now}
                    sessions[state_key] = state
            self._write_processed_unlocked(processed)
            return [
                memory.memory_id
                for request, memory in zip(all_requests, written)
                if request.get("memory_id") not in noop_memory_ids
            ]

    def process(
        self,
        *,
        source: str | None = None,
        session_id: str | None = None,
        model: Any = None,
        router: Any = None,
        scope: Any = None,
    ) -> dict[str, Any]:
        if source is not None:
            source = safe_component(source, "source")
        if session_id is not None:
            session_id = safe_component(session_id, "session id")
        now = _now_value(getattr(self.service, "clock", None))
        cleanup_hours = self._cleanup_hours()
        snapshots, cleaned = self._snapshot(
            source=source,
            session_id=session_id,
            now=now,
            cleanup_hours=cleanup_hours,
            scope=scope,
        )
        if not snapshots:
            deferred_candidates, deferred_turns = self._deferred_counts(
                source=source,
                session_id=session_id,
            )
            return {
                "processed_turns": 0,
                "memories_written": 0,
                "memory_ids": [],
                "metadata_merged": 0,
                "cleaned_turns": cleaned,
                "deferred_candidates": deferred_candidates,
                "deferred_inbox_turns": deferred_turns,
                "compaction": self._auto_compact(model=model, router=router),
            }
        backend = self._resolve_backend(model=model, router=router)
        requests: list[dict[str, Any]] = []
        observed_scopes: dict[tuple[str, str, str], list[str]] = {}
        self._planned_related = []
        self._deferred_by_turn = {}
        try:
            for snapshot in snapshots:
                with self.service.vault.lock():
                    processed = _read_processed(self.service.vault.processed_index_path)
                    state = self._state_for_snapshot_unlocked(snapshot, processed)
                turn_requests, turn_scopes = self._collect_turn_outputs(
                    backend, snapshot.turn, state, scope=scope
                )
                requests.extend(turn_requests)
                for request in turn_requests:
                    planned = self._planned_memory(request)
                    if planned is None:
                        continue
                    planned_id = planned.get("memory_id")
                    if isinstance(planned_id, str):
                        self._planned_related = [
                            item
                            for item in self._planned_related
                            if item.get("memory_id", "").casefold() != planned_id.casefold()
                        ]
                    self._planned_related.append(planned)
                observed_scopes[(snapshot.turn.source, snapshot.turn.session_id, snapshot.turn.turn_key)] = turn_scopes
            ids = self._commit_success(
                snapshots,
                requests,
                now=_now_value(getattr(self.service, "clock", None)),
                cleanup_hours=cleanup_hours,
                observed_scopes=observed_scopes,
                deferred_candidates=self._deferred_by_turn,
            )
            compaction = self._auto_compact(model=backend)
            deferred_candidates, deferred_turns = self._deferred_counts(
                source=source,
                session_id=session_id,
            )
            return {
                "processed_turns": len(snapshots),
                "memories_written": len(ids),
                "memory_ids": ids,
                "metadata_merged": self.writer.last_metadata_merged,
                "cleaned_turns": cleaned,
                "deferred_candidates": deferred_candidates,
                "deferred_inbox_turns": deferred_turns,
                "compaction": compaction,
            }
        except Exception as error:
            self._mark_failed(snapshots, error)
            raise

    def _remember_turn(
        self,
        *,
        content: str,
        source: str,
        session_id: str,
        turn_id: str,
        event_key_value: str,
        scopes: Any,
        now: str,
        cleanup_hours: int,
    ) -> tuple[Optional[_Snapshot], Optional[Mapping[str, Any]], Optional[InboxTurn], int]:
        with self.service.vault.lock():
            self.service._recover_compaction_unlocked()
            processed = _read_processed(self.service.vault.processed_index_path)
            cleaned = self._cleanup_due_unlocked(processed, now, cleanup_hours)
            existing = self._processed_memory_ids(processed, event_key_value)
            if existing is not None:
                return None, {"memory_ids": existing}, None, cleaned
            sessions = processed.setdefault("sessions", {})
            state_key = _session_key(source, session_id)
            state = sessions.get(state_key)
            if not isinstance(state, dict):
                state = {}
            if self._processing_marker_live(state.get("processing"), now):
                raise ProcessingError("session is already being processed")
            turns = state.get("turns")
            if not isinstance(turns, dict):
                turns = {}
            stable_key = turn_key(turn_id)
            index = _as_int(turns.get(stable_key), 0)
            if index <= 0:
                index = max(_as_int(state.get("next_turn_index"), 1), 1)
                turns[stable_key] = index
                state["next_turn_index"] = index + 1
            state["turns"] = turns
            token = uuid.uuid4().hex
            state["processing"] = {
                "status": _PROCESSING_STATUS,
                "token": token,
                "owner_pid": os.getpid(),
                "turn_keys": [stable_key],
                "turn_indices": [index],
                "started_at": now,
            }
            sessions[state_key] = state
            processed["sessions"] = sessions
            self._write_processed_unlocked(processed)
        event = InboxEvent(
            source=source,
            session_id=session_id,
            turn_key=stable_key,
            turn_index=index,
            role="user",
            event_key=event_key_value,
            content=redact_text(content),
            turn_id=_safe_turn_id(turn_id),
            timestamp=now,
        )
        turn = InboxTurn(source, session_id, stable_key, index, (event,))
        candidate = {
            "candidate_id": f"remember-{event_key_value[:16]}",
            "memory": event.content,
            "evidence_event_ids": [event_key_value],
            "duplicate": False,
            "worth": True,
            "type": "other",
            "scopes": scopes if scopes is not None else ["global"],
            "scope_source": "user",
        }
        return _Snapshot(turn, token, state_key), candidate, turn, cleaned

    def remember(
        self,
        content: str | None = None,
        *,
        text: str | None = None,
        source: str = "memleaf",
        session_id: str = "remember",
        turn_id: str | None = None,
        event_id: str | None = None,
        scopes: Any = None,
        model: Any = None,
        router: Any = None,
    ) -> dict[str, Any]:
        value = content if content is not None else text
        if not isinstance(value, str) or not value.strip():
            raise ValueError("remember content is required")
        source = safe_component(source, "source")
        session_id = safe_component(session_id, "session id")
        normalized_scopes = None
        if scopes is not None:
            try:
                normalized_scopes = normalize_scopes(scopes, field="remember scopes")
            except ScopeError as error:
                raise ValueError("invalid remember scopes") from error
        raw_turn_id = turn_id or f"remember-{hashlib.sha256(value.encode('utf-8')).hexdigest()[:16]}"
        if not isinstance(raw_turn_id, str) or not raw_turn_id or "\x00" in raw_turn_id or "\n" in raw_turn_id or "\r" in raw_turn_id:
            raise ValueError("invalid turn id")
        raw_event_id = event_id or f"remember/{source}/{session_id}/{raw_turn_id}"
        if not isinstance(raw_event_id, str) or not raw_event_id or "\x00" in raw_event_id or "\n" in raw_event_id or "\r" in raw_event_id:
            raise ValueError("invalid event id")
        stable_event_key = event_key(raw_event_id)
        now = _now_value(getattr(self.service, "clock", None))
        cleanup_hours = self._cleanup_hours()
        snapshot, candidate, turn, cleaned = self._remember_turn(
            content=value,
            source=source,
            session_id=session_id,
            turn_id=raw_turn_id,
            event_key_value=stable_event_key,
            scopes=normalized_scopes,
            now=now,
            cleanup_hours=cleanup_hours,
        )
        if snapshot is None:
            ids = list(candidate.get("memory_ids", [])) if isinstance(candidate, Mapping) else []
            return {
                "processed_turns": 0,
                "memories_written": 0,
                "memory_ids": ids,
                "metadata_merged": 0,
                "cleaned_turns": cleaned,
                "deferred_candidates": 0,
                "deferred_inbox_turns": 0,
                "compaction": self._auto_compact(model=model, router=router),
            }
        backend = self._resolve_backend(model=model, router=router)
        self._planned_related = []
        self._deferred_by_turn = {}
        try:
            with self.service.vault.lock():
                processed = _read_processed(self.service.vault.processed_index_path)
                state = self._state_for_snapshot_unlocked(snapshot, processed)
            requests, turn_scopes = self._collect_turn_outputs(
                backend,
                turn,
                state,
                explicit=True,
                explicit_candidate=candidate,
                scope=normalized_scopes,
            )
            if normalized_scopes is not None:
                turn_scopes = list(normalized_scopes)
            ids = self._commit_success(
                [snapshot],
                requests,
                now=_now_value(getattr(self.service, "clock", None)),
                cleanup_hours=cleanup_hours,
                observed_scopes={
                    (snapshot.turn.source, snapshot.turn.session_id, snapshot.turn.turn_key): turn_scopes
                },
            )
            return {
                "processed_turns": 1,
                "memories_written": len(ids),
                "memory_ids": ids,
                "metadata_merged": self.writer.last_metadata_merged,
                "cleaned_turns": cleaned,
                "deferred_candidates": 0,
                "deferred_inbox_turns": 0,
                "compaction": self._auto_compact(model=backend),
            }
        except Exception as error:
            self._mark_failed([snapshot], error)
            raise


def _add_hours(value: str, hours: int) -> str:
    parsed = _parse_time(value)
    if parsed is None:
        return value
    return (parsed + timedelta(hours=hours)).isoformat(timespec="seconds").replace("+00:00", "Z")


__all__ = ["ProcessingError", "Processor"]

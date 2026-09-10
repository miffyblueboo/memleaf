"""Bounded Model Route execution and model-output diagnostics."""
from __future__ import annotations
import json
from decimal import Decimal, InvalidOperation
import os
import threading
import time
from typing import Any, Callable, Mapping, Optional
from .config import DEFAULT_MODEL_CONCURRENCY, MAX_MODEL_CONCURRENCY, MIN_MODEL_CONCURRENCY
from .llm import MODEL_VALIDATION_REASONS, CallableBackend, ModelError, ModelUnavailable, ModelRouter
from .models import utc_now
from .prompts import COVERAGE_ALREADY_COMPLETED_CORRECTION, COVERAGE_CANDIDATE_CORRECTION, COVERAGE_CORRECTION, COVERAGE_SHAPE_CORRECTION, DUPLICATE_TARGET_CORRECTION, EVIDENCE_EVENT_MAPPING_CORRECTION, EVIDENCE_SPAN_CORRECTION, GATE_STRUCTURE_REPAIR_SYSTEM, GATE_TYPE_CORRECTION, JSON_CORRECTION, MIXED_FUTURE_USE_CORRECTION, MIXED_PROJECT_SCOPES_CORRECTION, RELATIVE_TIME_CORRECTION, SCOPE_GROUNDING_CORRECTION, SUMMARY_SCOPE_CORRECTION, SUMMARY_TARGET_CORRECTION, SUMMARY_TYPE_CORRECTION, TARGET_RELEVANCE_CORRECTION, UPDATE_TARGET_TYPE_CORRECTION, gate_structure_repair_prompt
from .validation import MODEL_VALIDATION_DETAILS, ModelOutputError, parse_strict_json
from .process_common import _DIAGNOSTIC_FILENAME, _DIAGNOSTIC_MAX_BYTES, _failure_metadata, _model_output_statistics, _safe_evidence_check, _safe_evidence_diagnostics


_METRIC_STAGE_NAMES = frozenset({
    "gate",
    "summarize",
    "semantic_review",
    "coordination",
    "target_reconciliation",
    "single_pass",
})
_PROVIDER_METRIC_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "prompt_cache_hit_tokens",
    "prompt_cache_miss_tokens",
    "reasoning_tokens",
)
_METRIC_OPERATION_SUFFIXES = ("primary", "format_repair")
_MAX_METRIC_CALLS = 256
_THINKING_EFFECTIVE_MODES = frozenset({"provider_default", "unsupported", "disabled", "minimal", "low", "high", "max"})
_THINKING_CONTROLS = frozenset({
    "provider_default", "unsupported", "openai_reasoning_effort",
    "deepseek_thinking_effort", "anthropic_effort", "anthropic_adaptive_effort",
    "gemini_thinking_level", "gemini_thinking_budget",
})
_COVERAGE_ALLOWED_FIELDS = frozenset({"unit_id", "decision", "candidate_ids", "reason", "memory_id"})
_GATE_TOP_LEVEL_FIELDS = frozenset({"candidates", "coverage", "evidence_bindings"})
_GATE_STRUCTURE_REPAIR_MAX_BYTES = 64 * 1024
# Only program-defined diagnostic labels may be emitted; arbitrary model keys
# can themselves be credentials or business text. Counts still include all keys.
_COVERAGE_DIAGNOSTIC_FIELDS = frozenset({"explanation"})


def _metric_bucket() -> dict[str, Any]:
    return {
        "call_count": 0,
        "retry_count": 0,
        "failed_calls": 0,
        "invalid_output_count": 0,
        "request_duration_ms": 0,
        "input_chars": 0,
        "input_bytes": 0,
        "output_chars": 0,
        "output_bytes": 0,
        "max_in_flight": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "prompt_cache_hit_tokens": 0,
        "prompt_cache_miss_tokens": 0,
        "reasoning_tokens": 0,
        "cache_hit_calls": 0,
        "_first_started": None,
        "_last_finished": None,
    }


def _safe_json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, (int, float)):
        return "number"
    return "other"


def _safe_schema_field_name(value: Any) -> bool:
    return isinstance(value, str) and value in _COVERAGE_DIAGNOSTIC_FIELDS


def _coverage_shape_diagnostics(raw: Any) -> dict[str, Any]:
    """Describe one coverage-shape failure without retaining business values."""

    if not isinstance(raw, str):
        return {}
    try:
        value = parse_strict_json(raw)
    except (ModelOutputError, RecursionError):
        return {}
    if not isinstance(value, Mapping):
        return {}
    coverage = value.get("coverage")
    if not isinstance(coverage, list):
        return {
            "coverage_actual_type": _safe_json_type(coverage),
            "coverage_allowed_fields": sorted(_COVERAGE_ALLOWED_FIELDS),
        }
    for row_index, row in enumerate(coverage):
        if not isinstance(row, Mapping):
            return {
                "coverage_row_index": row_index,
                "coverage_actual_type": _safe_json_type(row),
                "coverage_allowed_fields": sorted(_COVERAGE_ALLOWED_FIELDS),
            }
        unexpected = sorted(
            key for key in set(row) - _COVERAGE_ALLOWED_FIELDS
            if _safe_schema_field_name(key)
        )
        unexpected_count = len(set(row) - _COVERAGE_ALLOWED_FIELDS)
        if unexpected_count:
            return {
                "coverage_row_index": row_index,
                "coverage_actual_type": "object",
                "coverage_unexpected_fields": unexpected[:8],
                "coverage_unexpected_field_count": unexpected_count,
                "coverage_allowed_fields": sorted(_COVERAGE_ALLOWED_FIELDS),
            }
    return {}


def _coverage_shape_repairable(error: BaseException, raw: Any) -> bool:
    """Allow targeted repair only when it can be proven to remove extra keys only."""

    if (
        not isinstance(error, ModelOutputError)
        or getattr(error, "validation_detail", None) != "invalid_evidence"
        or getattr(error, "evidence_check", None) != "coverage_shape"
        or not isinstance(raw, str)
        or len(raw.encode("utf-8")) > _GATE_STRUCTURE_REPAIR_MAX_BYTES
    ):
        return False
    diagnostic = _coverage_shape_diagnostics(raw)
    return (
        diagnostic.get("coverage_actual_type") == "object"
        and isinstance(diagnostic.get("coverage_unexpected_field_count"), int)
        and diagnostic["coverage_unexpected_field_count"] > 0
    )


def _coverage_shape_repair_error() -> ModelOutputError:
    return ModelOutputError(
        "gate coverage shape repair changed legal semantic fields",
        validation_detail="invalid_evidence",
        evidence_check="coverage_shape",
    )


def _same_json_value(left: Any, right: Any) -> bool:
    """Type-strict JSON equality: object order is irrelevant, array order is not."""

    pending = [(left, right)]
    while pending:
        left, right = pending.pop()
        if type(left) is not type(right):
            return False
        if isinstance(left, dict):
            if left.keys() != right.keys():
                return False
            pending.extend((value, right[key]) for key, value in left.items())
        elif isinstance(left, list):
            if len(left) != len(right):
                return False
            pending.extend(zip(left, right))
        elif left != right:
            return False
    return True


def _validate_coverage_shape_repair(previous_raw: str, repaired_raw: str) -> None:
    """Prove that a targeted repair only removed unknown coverage fields."""

    if any(
        not isinstance(raw, str)
        or len(raw.encode("utf-8")) > _GATE_STRUCTURE_REPAIR_MAX_BYTES
        for raw in (previous_raw, repaired_raw)
    ):
        raise _coverage_shape_repair_error()
    try:
        # Reuse the canonical duplicate-key/non-finite-constant rejection first.
        # Decimal in this bounded second pass avoids float-rounding collisions
        # when proving that *all* existing legal values are unchanged.
        parse_strict_json(previous_raw)
        parse_strict_json(repaired_raw)
        previous = json.loads(previous_raw, parse_float=Decimal)
        repaired = json.loads(repaired_raw, parse_float=Decimal)
    except (ModelOutputError, TypeError, ValueError, RecursionError, InvalidOperation) as error:
        raise _coverage_shape_repair_error() from error
    if not isinstance(previous, Mapping) or not isinstance(repaired, Mapping):
        raise _coverage_shape_repair_error()
    if set(previous) != _GATE_TOP_LEVEL_FIELDS or set(repaired) != _GATE_TOP_LEVEL_FIELDS:
        raise _coverage_shape_repair_error()
    if not _same_json_value(previous.get("candidates"), repaired.get("candidates")):
        raise _coverage_shape_repair_error()
    if not _same_json_value(previous.get("evidence_bindings"), repaired.get("evidence_bindings")):
        raise _coverage_shape_repair_error()
    previous_coverage = previous.get("coverage")
    repaired_coverage = repaired.get("coverage")
    if (
        not isinstance(previous_coverage, list)
        or not isinstance(repaired_coverage, list)
        or len(previous_coverage) != len(repaired_coverage)
    ):
        raise _coverage_shape_repair_error()
    for previous_row, repaired_row in zip(previous_coverage, repaired_coverage):
        if not isinstance(previous_row, Mapping) or not isinstance(repaired_row, Mapping):
            raise _coverage_shape_repair_error()
        previous_legal = {
            key: previous_row[key]
            for key in _COVERAGE_ALLOWED_FIELDS
            if key in previous_row
        }
        repaired_legal = {
            key: repaired_row[key]
            for key in _COVERAGE_ALLOWED_FIELDS
            if key in repaired_row
        }
        if not _same_json_value(previous_legal, repaired_legal):
            raise _coverage_shape_repair_error()
        if set(repaired_row) - _COVERAGE_ALLOWED_FIELDS:
            raise _coverage_shape_repair_error()


class ModelExecutor:
    def __init__(self, service: Any):
        self.service = service
        self._metrics_lock = threading.Lock()
        self._metrics = _metric_bucket()
        self._metric_stages: dict[str, dict[str, Any]] = {}
        self._metric_operations: dict[str, dict[str, Any]] = {}
        self._metric_calls: list[dict[str, Any]] = []
        self._next_metric_call_index = 0
        self._active_calls = 0

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

    @staticmethod
    def _safe_metric_stage(value: Any) -> str:
        return value if isinstance(value, str) and value in _METRIC_STAGE_NAMES else "other"

    def max_parallel_calls(self, backend: Any) -> int:
        """Return the configured finite concurrency only for an explicitly safe backend."""

        if getattr(backend, "parallel_safe", False) is not True:
            return 1
        try:
            config = self.service.vault.config()
            process = config.get("process") if isinstance(config, Mapping) else None
            value = (
                process.get("model_concurrency", DEFAULT_MODEL_CONCURRENCY)
                if isinstance(process, Mapping)
                else DEFAULT_MODEL_CONCURRENCY
            )
        except Exception:
            value = DEFAULT_MODEL_CONCURRENCY
        if isinstance(value, bool) or not isinstance(value, int):
            return DEFAULT_MODEL_CONCURRENCY
        return min(MAX_MODEL_CONCURRENCY, max(MIN_MODEL_CONCURRENCY, value))

    @staticmethod
    def _safe_metric_operation(stage: str, value: Any) -> str:
        if value == "gate_coverage_repair":
            return "gate_coverage_repair"
        if stage == "gate" and value == "gate_semantic_retry":
            return "gate_semantic_retry"
        if isinstance(value, str) and value in {
            f"{stage}_{suffix}" for suffix in _METRIC_OPERATION_SUFFIXES
        }:
            return value
        return f"{stage}_primary"

    @staticmethod
    def _safe_provider_metrics(value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            return {}
        result: dict[str, Any] = {}
        for key in _PROVIDER_METRIC_FIELDS:
            item = value.get(key)
            if isinstance(item, int) and not isinstance(item, bool) and 0 <= item <= 10_000_000:
                result[key] = item
        mode = value.get("thinking_mode")
        if mode in {"default", "disabled", "low", "high", "max"}:
            result["thinking_mode"] = mode
        effective = value.get("thinking_effective")
        if effective in _THINKING_EFFECTIVE_MODES:
            result["thinking_effective"] = effective
        control = value.get("thinking_control")
        if control in _THINKING_CONTROLS:
            result["thinking_control"] = control
        return result

    @staticmethod
    def _consume_provider_metrics(backend: Any) -> dict[str, Any]:
        consume = getattr(backend, "consume_call_metrics", None)
        if not callable(consume):
            return {}
        try:
            return ModelExecutor._safe_provider_metrics(consume())
        except Exception:
            return {}

    def _metric_begin(
        self,
        *,
        stage: str,
        operation: str,
        input_chars: int,
        input_bytes: int,
    ) -> tuple[float, int]:
        started = time.perf_counter()
        with self._metrics_lock:
            self._active_calls += 1
            self._next_metric_call_index += 1
            call_index = self._next_metric_call_index
            for bucket in (
                self._metrics,
                self._metric_stages.setdefault(stage, _metric_bucket()),
                self._metric_operations.setdefault(operation, _metric_bucket()),
            ):
                if bucket["_first_started"] is None:
                    bucket["_first_started"] = started
                bucket["call_count"] += 1
                bucket["input_chars"] += input_chars
                bucket["input_bytes"] += input_bytes
                bucket["max_in_flight"] = max(bucket["max_in_flight"], self._active_calls)
        return started, call_index

    def _metric_finish(
        self,
        *,
        stage: str,
        operation: str,
        call_index: int,
        started: float,
        input_chars: int,
        input_bytes: int,
        output: Any,
        failed: bool,
        retry: bool,
        provider_metrics: Mapping[str, Any] | None,
    ) -> None:
        finished = time.perf_counter()
        elapsed_ms = max(0, round((finished - started) * 1000))
        output_chars = len(output) if isinstance(output, str) else 0
        output_bytes = len(output.encode("utf-8")) if isinstance(output, str) else 0
        provider_metrics = self._safe_provider_metrics(provider_metrics)
        with self._metrics_lock:
            for bucket in (
                self._metrics,
                self._metric_stages.setdefault(stage, _metric_bucket()),
                self._metric_operations.setdefault(operation, _metric_bucket()),
            ):
                bucket["_last_finished"] = finished
                bucket["request_duration_ms"] += elapsed_ms
                bucket["output_chars"] += output_chars
                bucket["output_bytes"] += output_bytes
                if failed:
                    bucket["failed_calls"] += 1
                if retry:
                    bucket["retry_count"] += 1
                for field in _PROVIDER_METRIC_FIELDS:
                    bucket[field] += int(provider_metrics.get(field, 0))
                if int(provider_metrics.get("prompt_cache_hit_tokens", 0)) > 0:
                    bucket["cache_hit_calls"] += 1
            if len(self._metric_calls) < _MAX_METRIC_CALLS:
                call = {
                    "call_index": call_index,
                    "stage": stage,
                    "operation": operation,
                    "retry": bool(retry),
                    "failed": bool(failed),
                    "invalid_output": False,
                    "request_duration_ms": elapsed_ms,
                    "input_chars": input_chars,
                    "input_bytes": input_bytes,
                    "output_chars": output_chars,
                    "output_bytes": output_bytes,
                }
                call.update(provider_metrics)
                self._metric_calls.append(call)
            self._active_calls = max(0, self._active_calls - 1)

    def _record_invalid_output(self, context: dict[str, Any]) -> None:
        """Count each received response rejected by guard/parser once, not HTTP errors.

        The per-call context belongs to one synchronous invocation, so concurrent
        summary/review jobs never attribute a rejection to another call. Totals
        remain exact even after the bounded per-call detail list is full.
        """

        with self._metrics_lock:
            if not context.get("response_received") or context.get("invalid_output"):
                return
            context["invalid_output"] = True
            for bucket in (
                self._metrics,
                self._metric_stages[context["stage"]],
                self._metric_operations[context["operation"]],
            ):
                bucket["invalid_output_count"] += 1
            for call in self._metric_calls:
                if call["call_index"] == context["call_index"]:
                    call["invalid_output"] = True
                    break

    @staticmethod
    def _public_metric_bucket(bucket: Mapping[str, Any]) -> dict[str, int]:
        first = bucket.get("_first_started")
        last = bucket.get("_last_finished")
        wall_clock_ms = (
            max(0, round((last - first) * 1000))
            if isinstance(first, (int, float)) and isinstance(last, (int, float)) and last >= first
            else 0
        )
        result = {
            "call_count": int(bucket.get("call_count", 0)),
            "retry_count": int(bucket.get("retry_count", 0)),
            "failed_calls": int(bucket.get("failed_calls", 0)),
            "invalid_output_count": int(bucket.get("invalid_output_count", 0)),
            "request_duration_ms": int(bucket.get("request_duration_ms", 0)),
            "wall_clock_ms": wall_clock_ms,
            "input_chars": int(bucket.get("input_chars", 0)),
            "input_bytes": int(bucket.get("input_bytes", 0)),
            "output_chars": int(bucket.get("output_chars", 0)),
            "output_bytes": int(bucket.get("output_bytes", 0)),
            "max_in_flight": int(bucket.get("max_in_flight", 0)),
            "cache_hit_calls": int(bucket.get("cache_hit_calls", 0)),
        }
        for field in _PROVIDER_METRIC_FIELDS:
            result[field] = int(bucket.get(field, 0))
        return result

    def metrics(self) -> dict[str, Any]:
        """Return structural telemetry only; never prompts, responses or credentials."""

        with self._metrics_lock:
            total = self._public_metric_bucket(dict(self._metrics))
            stages = {
                key: self._public_metric_bucket(dict(value))
                for key, value in sorted(self._metric_stages.items())
            }
            operations = {
                key: self._public_metric_bucket(dict(value))
                for key, value in sorted(self._metric_operations.items())
            }
            calls = [
                dict(value)
                for value in sorted(self._metric_calls, key=lambda item: item["call_index"])
            ]
        return {"total": total, "stages": stages, "operations": operations, "calls": calls}

    def _complete(
        self,
        backend: Any,
        prompt: str,
        *,
        system: str,
        purpose: str,
        metric_stage: str | None = None,
        metric_operation: str | None = None,
        retry: bool = False,
        metric_context: dict[str, Any] | None = None,
    ) -> str:
        stage = self._safe_metric_stage(metric_stage or purpose)
        operation = self._safe_metric_operation(stage, metric_operation)
        input_chars = len(prompt) + len(system)
        input_bytes = len(prompt.encode("utf-8")) + len(system.encode("utf-8"))
        started, call_index = self._metric_begin(
            stage=stage,
            operation=operation,
            input_chars=input_chars,
            input_bytes=input_bytes,
        )
        if metric_context is not None:
            metric_context.update(
                stage=stage, operation=operation, call_index=call_index,
                response_received=False, invalid_output=False,
            )
        value: Any = None
        failed = False
        try:
            value = backend.complete(prompt, system=system, purpose=purpose, temperature=0.0)
            if metric_context is not None:
                # Any normal return means the backend produced a response, even
                # when the response object itself violates the text contract.
                metric_context["response_received"] = True
            if not isinstance(value, str):
                failed = True
                raise ModelError(
                    "model backend returned non-text output",
                    code="model_invalid_response",
                    stage=purpose,
                    validation_reason="response_shape",
                )
        except ModelError as error:
            failed = True
            error.with_stage(purpose)
            if metric_context is not None and error.code == "model_invalid_response":
                # Built-in backends use this code only after receiving a
                # provider/callback response that violates the response contract.
                metric_context["response_received"] = True
            raise
        except Exception as error:
            failed = True
            raise ModelError("model backend failed", stage=purpose) from error
        finally:
            provider_metrics = self._consume_provider_metrics(backend)
            self._metric_finish(
                stage=stage,
                operation=operation,
                call_index=call_index,
                started=started,
                input_chars=input_chars,
                input_bytes=input_bytes,
                output=value,
                failed=failed,
                retry=retry,
                provider_metrics=provider_metrics,
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
        # correction prompt. Schema/shape violations receive the same bounded
        # retry allowance. The caller still stops after attempt three, except
        # for the existing Gate invalid-span recovery below.
        return ModelExecutor._retryable_json_error(error) and attempt_count < 3

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
        hint = ModelExecutor._safe_correction_hint(error)
        stage = getattr(error, "stage", None)
        if hint == "duplicate_update_target":
            return DUPLICATE_TARGET_CORRECTION
        if hint == "mixed_project_scopes":
            return MIXED_PROJECT_SCOPES_CORRECTION
        if hint == "mixed_future_use":
            return MIXED_FUTURE_USE_CORRECTION
        if stage == "gate" and hint == "update_target_type_mismatch":
            return UPDATE_TARGET_TYPE_CORRECTION
        if stage == "gate" and hint == "invalid_type":
            return GATE_TYPE_CORRECTION
        if stage == "gate" and hint == "unknown_fields":
            return (
                "Previous output violated: unknown_fields. Repair the Gate JSON schema. "
                "Each candidate allows only candidate_id, memory, duplicate, worth, type, scopes, "
                "scope_source, evidence_event_ids, reason, duplicate_memory_id, and update_memory_id. "
                "evidence_bindings is a TOP-LEVEL sibling of candidates and coverage; it is never "
                "a candidate field. Keep the exact source quotes and IDs in top-level bindings. "
                "Do not turn supported candidate proposals into blanket NO_CHANGE/DEFERRED merely "
                "to avoid a schema error. Recheck each proposal against its supplied source evidence."
            )
        if stage == "gate" and hint == "scope_not_grounded":
            return SCOPE_GROUNDING_CORRECTION
        if stage == "gate" and hint == "target_not_relevant":
            return TARGET_RELEVANCE_CORRECTION
        if stage == "gate" and hint == "invalid_evidence":
            if getattr(error, "evidence_check", None) == "invalid_span":
                return EVIDENCE_SPAN_CORRECTION
            if getattr(error, "evidence_check", None) == "coverage_shape":
                return COVERAGE_SHAPE_CORRECTION
            if getattr(error, "evidence_check", None) == "coverage_candidate":
                return COVERAGE_CANDIDATE_CORRECTION
            if getattr(error, "evidence_check", None) in {
                "event_mismatch", "omitted_evidence_binding",
            }:
                return EVIDENCE_EVENT_MAPPING_CORRECTION
            if getattr(error, "evidence_check", None) == "coverage_terminal_witness":
                return COVERAGE_ALREADY_COMPLETED_CORRECTION
            context = ModelExecutor._evidence_correction_context(error)
            return COVERAGE_CORRECTION if context is None else COVERAGE_CORRECTION + "\n" + context
        if stage == "summarize" and hint == "scope_drift":
            return SUMMARY_SCOPE_CORRECTION
        if hint in {"relative_time", "due_date_not_grounded"}:
            return RELATIVE_TIME_CORRECTION
        if stage == "summarize" and hint == "invalid_update_target":
            return SUMMARY_TARGET_CORRECTION
        if stage == "summarize" and hint == "invalid_type":
            return SUMMARY_TYPE_CORRECTION
        if hint is not None:
            return f"Previous output violated: {hint}."
        return None

    @staticmethod
    def _evidence_correction_context(error: BaseException) -> Optional[str]:
        """Build a bounded Gate repair hint from validator-owned context only."""

        if getattr(error, "evidence_check", None) != "unknown_unit":
            return None
        diagnostics = _safe_evidence_diagnostics(error)
        path = diagnostics.get("evidence_path")
        expected = getattr(error, "evidence_expected_ids", ())
        if not isinstance(path, str) or not isinstance(expected, tuple) or not all(
            isinstance(item, str) and item for item in expected
        ):
            return None
        expected_json = json.dumps(list(expected), ensure_ascii=False, separators=(",", ":"))
        actual_parts = []
        for key, label in (("evidence_actual_type", "type"), ("evidence_actual_length", "length"),
                           ("evidence_actual_sha256", "sha256")):
            if key in diagnostics:
                actual_parts.append(f"{label}={diagnostics[key]}")
        descriptor = ", ".join(actual_parts) or "no safe value descriptor"
        legal = (
            f"the complete legal unit_id set is exactly {expected_json}. "
            if expected else "there are no legal unit_id values in this call. "
        )
        return (
            "Evidence reference diagnostic (structural only): evidence_check=unknown_unit; "
            "the invalid unit_id was at "
            f"{path}. Safe descriptor: {descriptor}. For this call, {legal}"
            "Copy one legal unit_id character-for-character from that set when one exists; "
            "do not use the invalid value, a placeholder, event_key, call ID, digest, or a guessed mapping."
        )

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
        if isinstance(attempt_count, bool) or not isinstance(attempt_count, int) or attempt_count not in (1, 2, 3, 4):
            attempt_count = None
        failure_code = ""
        validation_reason = ""
        validation_detail = ""
        evidence_check = None
        if error is not None:
            failure_code, _failure_stage, reason, detail, _attempt = _failure_metadata(error)
            validation_reason = reason or ""
            validation_detail = detail or ""
            evidence_check = _safe_evidence_check(error)
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
        if evidence_check is not None:
            entry["evidence_check"] = evidence_check
        entry.update(_safe_evidence_diagnostics(error) if error is not None else {})
        if evidence_check == "coverage_shape":
            entry.update(_coverage_shape_diagnostics(raw))
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
        metric_stage: str | None = None,
        max_attempts: int | None = None,
    ) -> Any:
        if max_attempts is None:
            effective_max_attempts = 4
        elif isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not 1 <= max_attempts <= 4:
            raise ValueError("max_attempts must be an integer from 1 to 4")
        else:
            effective_max_attempts = max_attempts
        correction_prompt = prompt + "\n\n" + JSON_CORRECTION
        correction_instructions: list[str] = []
        saw_invalid_span = False
        targeted_repair_pending = False
        targeted_repair_used = False
        targeted_previous_raw: Optional[str] = None
        targeted_prompt = ""
        for attempt_count in range(1, effective_max_attempts + 1):
            raw: Any = None
            metric_context: dict[str, Any] = {}
            using_targeted_repair = targeted_repair_pending
            try:
                operation_stage = self._safe_metric_stage(metric_stage or purpose)
                if attempt_count == 1:
                    call_prompt = prompt
                    call_system = system
                    metric_operation = f"{operation_stage}_primary"
                elif using_targeted_repair:
                    call_prompt = targeted_prompt
                    call_system = GATE_STRUCTURE_REPAIR_SYSTEM
                    metric_operation = f"{operation_stage}_format_repair"
                else:
                    call_prompt = correction_prompt
                    call_system = system
                    metric_operation = (
                        "gate_semantic_retry"
                        if operation_stage == "gate"
                        else f"{operation_stage}_format_repair"
                    )
                targeted_repair_pending = False
                raw = self._complete(
                    backend,
                    call_prompt,
                    system=call_system,
                    purpose=purpose,
                    metric_stage=metric_stage,
                    metric_operation=metric_operation,
                    retry=attempt_count > 1,
                    metric_context=metric_context,
                )
                if using_targeted_repair:
                    if not isinstance(targeted_previous_raw, str) or not isinstance(raw, str):
                        raise _coverage_shape_repair_error()
                    _validate_coverage_shape_repair(targeted_previous_raw, raw)
                parsed = parser(raw)
            except (ModelError, ModelOutputError) as error:
                if isinstance(error, ModelOutputError) or (
                    isinstance(error, ModelError) and error.code == "model_invalid_response"
                ):
                    self._record_invalid_output(metric_context)
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
                invalid_span = (
                    isinstance(error, ModelOutputError)
                    and purpose == "gate"
                    and getattr(error, "validation_detail", None) == "invalid_evidence"
                    and getattr(error, "evidence_check", None) == "invalid_span"
                )
                # A span error first exposed on Gate's third attempt gets one
                # bounded chance to copy an exact quote from the original
                # evidence. Earlier span errors already had that opportunity;
                # every other failure keeps the ordinary three-attempt limit.
                extra_span_recovery = (
                    invalid_span
                    and attempt_count == 3
                    and not saw_invalid_span
                )
                saw_invalid_span = saw_invalid_span or invalid_span
                can_retry = (
                    self._allows_next_json_attempt(error, attempt_count)
                    and attempt_count < effective_max_attempts
                )
                can_extra_span_recover = extra_span_recovery and attempt_count < effective_max_attempts
                if can_retry or can_extra_span_recover:
                    if (
                        purpose == "gate"
                        and not targeted_repair_used
                        and _coverage_shape_repairable(error, raw)
                    ):
                        targeted_repair_used = True
                        targeted_previous_raw = raw
                        targeted_prompt = gate_structure_repair_prompt(
                            raw,
                            _coverage_shape_diagnostics(raw),
                        )
                        targeted_repair_pending = True
                        continue
                    correction_prompt = prompt + "\n\n" + JSON_CORRECTION
                    instruction = self._correction_instruction(error)
                    if instruction is not None and instruction not in correction_instructions:
                        correction_instructions.append(instruction)
                    if correction_instructions:
                        correction_prompt += "\n" + "\n".join(correction_instructions)
                    if (purpose == "gate" and getattr(error, "validation_detail", None) == "unknown_fields"
                        and isinstance(raw, str) and len(raw.encode("utf-8")) <= 64 * 1024):
                        correction_prompt += (
                            "\nPrevious invalid response (untrusted proposals for schema repair only; "
                            "not new evidence or committed memories):\n" + raw
                            + "\nReturn the repaired strict Gate object using only the original supplied "
                            "evidence units. Previous proposal text cannot authorize a new fact."
                        )
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

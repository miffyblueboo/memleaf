"""Bounded Model Route execution and model-output diagnostics."""
from __future__ import annotations
import json
import os
import threading
import time
from typing import Any, Callable, Mapping, Optional
from .config import DEFAULT_MODEL_CONCURRENCY, MAX_MODEL_CONCURRENCY, MIN_MODEL_CONCURRENCY
from .llm import MODEL_VALIDATION_REASONS, CallableBackend, ModelError, ModelUnavailable, ModelRouter
from .models import utc_now
from .prompts import COVERAGE_ALREADY_COMPLETED_CORRECTION, COVERAGE_CANDIDATE_CORRECTION, COVERAGE_CORRECTION, DUPLICATE_TARGET_CORRECTION, EVIDENCE_EVENT_MAPPING_CORRECTION, EVIDENCE_SPAN_CORRECTION, GATE_TYPE_CORRECTION, JSON_CORRECTION, MIXED_FUTURE_USE_CORRECTION, MIXED_PROJECT_SCOPES_CORRECTION, RELATIVE_TIME_CORRECTION, SCOPE_GROUNDING_CORRECTION, SUMMARY_SCOPE_CORRECTION, SUMMARY_TARGET_CORRECTION, SUMMARY_TYPE_CORRECTION, TARGET_RELEVANCE_CORRECTION, UPDATE_TARGET_TYPE_CORRECTION
from .validation import MODEL_VALIDATION_DETAILS, ModelOutputError
from .process_common import _DIAGNOSTIC_FILENAME, _DIAGNOSTIC_MAX_BYTES, _failure_metadata, _model_output_statistics, _safe_evidence_check, _safe_evidence_diagnostics


_METRIC_STAGE_NAMES = frozenset({
    "gate",
    "summarize",
    "semantic_review",
    "coordination",
    "target_reconciliation",
})


def _metric_bucket() -> dict[str, Any]:
    return {
        "call_count": 0,
        "retry_count": 0,
        "failed_calls": 0,
        "request_duration_ms": 0,
        "input_chars": 0,
        "input_bytes": 0,
        "output_chars": 0,
        "output_bytes": 0,
        "max_in_flight": 0,
        "_first_started": None,
        "_last_finished": None,
    }


class ModelExecutor:
    def __init__(self, service: Any):
        self.service = service
        self._metrics_lock = threading.Lock()
        self._metrics = _metric_bucket()
        self._metric_stages: dict[str, dict[str, Any]] = {}
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

    def _metric_begin(self, *, stage: str, input_chars: int, input_bytes: int) -> float:
        started = time.perf_counter()
        with self._metrics_lock:
            self._active_calls += 1
            for bucket in (self._metrics, self._metric_stages.setdefault(stage, _metric_bucket())):
                if bucket["_first_started"] is None:
                    bucket["_first_started"] = started
                bucket["call_count"] += 1
                bucket["input_chars"] += input_chars
                bucket["input_bytes"] += input_bytes
                bucket["max_in_flight"] = max(bucket["max_in_flight"], self._active_calls)
        return started

    def _metric_finish(
        self,
        *,
        stage: str,
        started: float,
        output: Any,
        failed: bool,
        retry: bool,
    ) -> None:
        finished = time.perf_counter()
        elapsed_ms = max(0, round((finished - started) * 1000))
        output_chars = len(output) if isinstance(output, str) else 0
        output_bytes = len(output.encode("utf-8")) if isinstance(output, str) else 0
        with self._metrics_lock:
            for bucket in (self._metrics, self._metric_stages.setdefault(stage, _metric_bucket())):
                bucket["_last_finished"] = finished
                bucket["request_duration_ms"] += elapsed_ms
                bucket["output_chars"] += output_chars
                bucket["output_bytes"] += output_bytes
                if failed:
                    bucket["failed_calls"] += 1
                if retry:
                    bucket["retry_count"] += 1
            self._active_calls = max(0, self._active_calls - 1)

    @staticmethod
    def _public_metric_bucket(bucket: Mapping[str, Any]) -> dict[str, int]:
        first = bucket.get("_first_started")
        last = bucket.get("_last_finished")
        wall_clock_ms = (
            max(0, round((last - first) * 1000))
            if isinstance(first, (int, float)) and isinstance(last, (int, float)) and last >= first
            else 0
        )
        return {
            "call_count": int(bucket.get("call_count", 0)),
            "retry_count": int(bucket.get("retry_count", 0)),
            "failed_calls": int(bucket.get("failed_calls", 0)),
            "request_duration_ms": int(bucket.get("request_duration_ms", 0)),
            "wall_clock_ms": wall_clock_ms,
            "input_chars": int(bucket.get("input_chars", 0)),
            "input_bytes": int(bucket.get("input_bytes", 0)),
            "output_chars": int(bucket.get("output_chars", 0)),
            "output_bytes": int(bucket.get("output_bytes", 0)),
            "max_in_flight": int(bucket.get("max_in_flight", 0)),
        }

    def metrics(self) -> dict[str, Any]:
        """Return aggregate structural telemetry only; never prompts, responses or credentials."""

        with self._metrics_lock:
            total = self._public_metric_bucket(dict(self._metrics))
            stages = {
                key: self._public_metric_bucket(dict(value))
                for key, value in sorted(self._metric_stages.items())
            }
        return {"total": total, "stages": stages}

    def _complete(
        self,
        backend: Any,
        prompt: str,
        *,
        system: str,
        purpose: str,
        metric_stage: str | None = None,
        retry: bool = False,
    ) -> str:
        stage = self._safe_metric_stage(metric_stage or purpose)
        input_chars = len(prompt) + len(system)
        input_bytes = len(prompt.encode("utf-8")) + len(system.encode("utf-8"))
        started = self._metric_begin(stage=stage, input_chars=input_chars, input_bytes=input_bytes)
        value: Any = None
        failed = False
        try:
            value = backend.complete(prompt, system=system, purpose=purpose, temperature=0.0)
        except ModelError as error:
            failed = True
            error.with_stage(purpose)
            raise
        except Exception as error:
            failed = True
            raise ModelError("model backend failed", stage=purpose) from error
        finally:
            # Parser failures are recorded by the next call as retries; this
            # metric records only one bounded backend invocation and never its text.
            self._metric_finish(
                stage=stage,
                started=started,
                output=value,
                failed=failed,
                retry=retry,
            )
        if not isinstance(value, str):
            # The transport call completed, but the response shape is still a
            # model failure. Account for it without inspecting/repr-ing the value.
            with self._metrics_lock:
                self._metrics["failed_calls"] += 1
                self._metric_stages.setdefault(stage, _metric_bucket())["failed_calls"] += 1
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
    ) -> Any:
        correction_prompt = prompt + "\n\n" + JSON_CORRECTION
        correction_instructions: list[str] = []
        saw_invalid_span = False
        for attempt_count in (1, 2, 3, 4):
            raw: Any = None
            try:
                raw = self._complete(
                    backend,
                    prompt if attempt_count == 1 else correction_prompt,
                    system=system,
                    purpose=purpose,
                    metric_stage=metric_stage,
                    retry=attempt_count > 1,
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
                if self._allows_next_json_attempt(error, attempt_count) or extra_span_recovery:
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

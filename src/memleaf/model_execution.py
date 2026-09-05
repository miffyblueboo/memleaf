"""Bounded Model Route execution and model-output diagnostics."""
from __future__ import annotations
import json
import os
from typing import Any, Callable, Mapping, Optional
from .llm import MODEL_VALIDATION_REASONS, CallableBackend, ModelError, ModelUnavailable, ModelRouter
from .models import utc_now
from .prompts import DUPLICATE_TARGET_CORRECTION, GATE_TYPE_CORRECTION, JSON_CORRECTION, MIXED_FUTURE_USE_CORRECTION, MIXED_PROJECT_SCOPES_CORRECTION, RELATIVE_TIME_CORRECTION, SCOPE_GROUNDING_CORRECTION, SUMMARY_SCOPE_CORRECTION, SUMMARY_TARGET_CORRECTION, SUMMARY_TYPE_CORRECTION, TARGET_RELEVANCE_CORRECTION, UPDATE_TARGET_TYPE_CORRECTION
from .validation import MODEL_VALIDATION_DETAILS, ModelOutputError
from .process_common import _DIAGNOSTIC_FILENAME, _DIAGNOSTIC_MAX_BYTES, _failure_metadata, _model_output_statistics


class ModelExecutor:
    def __init__(self, service: Any):
        self.service = service

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

"""Offline, explicitly authorized multi-turn acceptance on the real runtime.

No production Vault is accepted and no model is constructed in planning mode.
Assertions/checklists are evaluation data, never model instructions. A passing
structural report is not a semantic approval, host test or switch permission.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any, Mapping, Sequence

from . import Memleaf
from .config import load_config
from .incremental_prompts import INCREMENTAL_SYSTEM
from .incremental_protocol import MAX_BYTES as MAX_TRACE_BYTES
from .llm import ModelError
from .locking import atomic_write_json
from .migration import _runtime
from .query_scan import scan_memories
from .validation import parse_strict_json

VERSION = 1
MAX_SUITE_BYTES = 2 * 1024 * 1024
MAX_STEPS = 128
MAX_REPEATS = 20
MAX_REQUESTS = 2048
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_CHECK_FIELDS = frozenset({"memory_id", "type", "status", "validity", "scopes", "due_date", "assignee", "waiting_on"})
_MESSAGE_FIELDS = frozenset({"role", "content", "source_time", "message_id", "source_sequence", "final"})
_STOP_CODES = frozenset({"model_auth_failed", "model_unavailable", "model_rate_limited", "model_timeout", "model_network_error", "model_http_error"})
_USAGE_FIELDS = frozenset({"prompt_tokens", "completion_tokens", "total_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens", "reasoning_tokens"})


class AcceptanceError(ValueError):
    """A fixed safe code; do not print arbitrary source/config exception text."""


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _integer(value: Any, minimum: int, maximum: int) -> bool:
    return type(value) is int and minimum <= value <= maximum


def _name(value: Any) -> bool:
    return isinstance(value, str) and _NAME.fullmatch(value) is not None


def _text(value: Any, maximum: int = 65536) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value.encode("utf-8")) <= maximum and "\x00" not in value


def _object(value: Any, allowed: set | frozenset, required: set | frozenset = frozenset()) -> None:
    if not isinstance(value, dict) or set(value) - allowed or not required <= set(value):
        raise AcceptanceError("invalid_suite_schema")


def validate_suite(value: Any) -> dict:
    """Small fixture schema, not a workflow/plugin language or model schema."""
    _object(value, {"version", "suite_id", "cases"}, {"version", "suite_id", "cases"})
    if type(value["version"]) is not int or value["version"] != VERSION or not _name(value["suite_id"]):
        raise AcceptanceError("unsupported_suite")
    if not isinstance(value["cases"], list) or not 1 <= len(value["cases"]) <= 64:
        raise AcceptanceError("invalid_case_count")
    ids: set[str] = set()
    total = 0
    for case in value["cases"]:
        _object(case, {"id", "split", "semantic_checks", "initial_memories", "turns"}, {"id", "split", "semantic_checks", "turns"})
        if not _name(case["id"]) or case["id"] in ids or (not isinstance(case["split"], str) or case["split"] not in {"regression", "holdout"}):
            raise AcceptanceError("invalid_case_identity")
        ids.add(case["id"])
        if (not isinstance(case["semantic_checks"], list) or not 1 <= len(case["semantic_checks"]) <= 20
                or any(not _text(x, 2048) for x in case["semantic_checks"])):
            raise AcceptanceError("invalid_semantic_checklist")
        initial = case.get("initial_memories", [])
        if not isinstance(initial, list) or len(initial) > 64:
            raise AcceptanceError("invalid_initial_memories")
        seen_memories = set()
        for memory in initial:
            _object(memory, _CHECK_FIELDS | {"title", "body", "due_text"}, {"memory_id", "title", "body", "type", "scopes"})
            if (not _name(memory["memory_id"]) or memory["memory_id"] in seen_memories
                    or not _text(memory["title"], 1024)
                    or not (_text(memory["body"]) or (memory.get("validity") == "retracted" and memory["body"] == ""))):
                raise AcceptanceError("invalid_initial_memory")
            # Validate the actual Core model before any execution or output directory.
            from .models import Memory
            try:
                Memory.from_mapping({**memory, "created": "2026-01-01T00:00:00Z", "updated": "2026-01-01T00:00:00Z"})
            except (TypeError, ValueError):
                raise AcceptanceError("invalid_initial_memory") from None
            seen_memories.add(memory["memory_id"])
        turns = case["turns"]
        if not isinstance(turns, list) or not 1 <= len(turns) <= 32:
            raise AcceptanceError("invalid_turn_count")
        total += len(turns)
        seen_turns = set()
        seen_messages = set()
        seen_sequences = set()
        for index, turn in enumerate(turns):
            _object(turn, {"id", "messages", "scope", "expected"}, {"id", "messages"})
            if not _name(turn["id"]) or turn["id"] in seen_turns:
                raise AcceptanceError("invalid_turn_identity")
            seen_turns.add(turn["id"])
            messages = turn["messages"]
            if not isinstance(messages, list) or not 2 <= len(messages) <= 16:
                raise AcceptanceError("invalid_messages")
            for position, message in enumerate(messages):
                _object(message, _MESSAGE_FIELDS, {"role", "content"})
                if not isinstance(message["role"], str) or message["role"] not in {"user", "assistant"} or not _text(message["content"]):
                    raise AcceptanceError("invalid_message")
                if "message_id" in message and not _name(message["message_id"]):
                    raise AcceptanceError("invalid_message_identity")
                if "source_sequence" in message and not _integer(message["source_sequence"], 0, 1_000_000):
                    raise AcceptanceError("invalid_source_sequence")
                message_key = message.get("message_id", f'{turn["id"]}-{position}')
                sequence = message.get("source_sequence")
                if message_key in seen_messages or (sequence is not None and sequence in seen_sequences):
                    raise AcceptanceError("ambiguous_fixture_source")
                seen_messages.add(message_key)
                if sequence is not None:
                    seen_sequences.add(sequence)
                if "final" in message and type(message["final"]) is not bool:
                    raise AcceptanceError("invalid_final")
                when = message.get("source_time")
                if when is not None:
                    try:
                        stamp = datetime.fromisoformat(when)
                        if stamp.utcoffset() is None:
                            raise ValueError()
                    except (TypeError, ValueError):
                        raise AcceptanceError("invalid_source_time") from None
            # Do not synthesize an assistant or final marker for malformed fixtures.
            if (messages[-1]["role"] != "assistant" or messages[-1].get("final") is not True
                    or not any(m["role"] == "user" for m in messages)
                    or sum(m["role"] == "assistant" for m in messages) != 1):
                raise AcceptanceError("fixture_turn_not_complete")
            if "scope" in turn:
                from .scope_state import normalize_scopes
                try:
                    normalize_scopes(turn["scope"])
                except (TypeError, ValueError):
                    raise AcceptanceError("invalid_fixture_scope") from None
            expected = turn.get("expected", {})
            _object(expected, {"count", "required", "forbidden", "same_ids_as", "max_calls", "execution_status", "coverage_status"})
            for key in ("count", "max_calls", "same_ids_as"):
                if key in expected and not _integer(expected[key], 0, index - 1 if key == "same_ids_as" else (2 if key == "max_calls" else 128)):
                    raise AcceptanceError("invalid_expectation")
            for key in ("execution_status", "coverage_status"):
                if key in expected and not _name(expected[key]):
                    raise AcceptanceError("invalid_expectation")
            for key in ("required", "forbidden"):
                constraints = expected.get(key, [])
                if not isinstance(constraints, list) or len(constraints) > 64:
                    raise AcceptanceError("invalid_expectation")
                for row in constraints:
                    _object(row, _CHECK_FIELDS)
                    if not row or any(not isinstance(v, (str, list, type(None))) for v in row.values()):
                        raise AcceptanceError("invalid_expectation")
                    if any(isinstance(v, list) and (len(v) > 32 or any(not isinstance(x, str) for x in v)) for v in row.values()):
                        raise AcceptanceError("invalid_expectation")
    if total > MAX_STEPS or len(_canonical(value)) > MAX_SUITE_BYTES:
        raise AcceptanceError("suite_limit_exceeded")
    return deepcopy(value)


def read_suite(path: Path | str) -> dict:
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_SUITE_BYTES:
        raise AcceptanceError("invalid_suite_file")
    try:
        raw = path.read_bytes()
        if len(raw) > MAX_SUITE_BYTES:
            raise ValueError()
        return validate_suite(parse_strict_json(raw.decode("utf-8")))
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError):
        raise AcceptanceError("invalid_suite_file") from None


def plan_suite(suite: dict, *, repeats: int = 5) -> dict:
    suite = validate_suite(suite)
    if not _integer(repeats, 1, MAX_REPEATS):
        raise AcceptanceError("invalid_repeats")
    turns = sum(len(c["turns"]) for c in suite["cases"])
    return {"version": VERSION, "suite_id": suite["suite_id"], "suite_sha256": _hash(_canonical(suite)),
            "cases": len(suite["cases"]), "turns_per_repeat": turns, "repeats": repeats,
            "normal_request_upper_bound": turns * repeats,
            "recovery_request_upper_bound": turns * repeats * 2,
            "model_calls": 0, "mode": "plan", "semantic_status": "not_run",
            "switch_authorized": False, "prompt_sha256": _hash(INCREMENTAL_SYSTEM.encode("utf-8")),
            "prompt_chars": len(INCREMENTAL_SYSTEM), "prompt_bytes": len(INCREMENTAL_SYSTEM.encode("utf-8"))}


def _label(value: Any) -> str:
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._:/-]{1,160}", value) else "unknown"


def _backend_identity(backend: Any) -> dict:
    endpoint = getattr(backend, "base_url", None)
    # Report only a fingerprint, never endpoint userinfo/path/query credentials.
    return {"provider": _label(getattr(backend, "provider", None)),
            "provider_family": _label(getattr(backend, "provider_family", None)),
            "requested_model": _label(getattr(backend, "model", None)),
            "returned_model": "per_attempt_or_unknown", "endpoint_sha256": _hash(endpoint.encode()) if isinstance(endpoint, str) else None,
            "single_dispatch": True,
            "request_timeout": getattr(backend, "timeout", None) if isinstance(getattr(backend, "timeout", None), (float, int)) else None,
            "output_token_limit": getattr(backend, "single_pass_primary_max_tokens", None) if _integer(getattr(backend, "single_pass_primary_max_tokens", None), 1, 100000) else None}


class _Meter:
    """One offline run, one durable global cap. Not a replacement Core budget."""
    single_pass_safe = True
    single_pass_protocol = True

    def __init__(self, backend: Any, output: Path, limit: int):
        if getattr(backend, "single_pass_safe", False) is not True or not callable(getattr(backend, "complete", None)):
            raise AcceptanceError("backend_not_single_dispatch")
        self.backend, self.output, self.limit = backend, output, limit
        self.attempts: list[dict] = []
        self.trace_dir = output / "traces"
        self.trace_dir.mkdir(mode=0o700)
        self.scope: dict[str, Any] = {}
        self.exhausted = False
        self._write()

    def _write(self):
        atomic_write_json(self.output / "requests.json", {"version": VERSION, "limit": self.limit,
                          "reservations": len(self.attempts), "attempts": self.attempts})

    def complete(self, prompt: str, *, system: str = "", **kwargs):
        if len(self.attempts) >= self.limit:
            self.exhausted = True
            raise ModelError("acceptance request cap reached", code="model_unavailable")
        number = len(self.attempts) + 1
        trace = {"system": system, "prompt": prompt, "purpose": kwargs.get("purpose", ""),
                 "temperature": kwargs.get("temperature", 0.0), "response": None}
        if len(prompt.encode()) + len(system.encode()) > MAX_TRACE_BYTES:
            raise AcceptanceError("acceptance_request_too_large")
        trace_path = self.trace_dir / f"{number:04d}.json"
        atomic_write_json(trace_path, trace)  # Private, inspectable input; never auth headers.
        row = {**self.scope, "outcome": "reserved", "input_bytes": len(prompt.encode()),
               "system_bytes": len(system.encode()), "input_sha256": _hash(prompt.encode()),
               "system_sha256": _hash(system.encode()), "output_bytes": None, "usage": None,
               "provider_outcome": "unknown", "trace": f"traces/{number:04d}.json",
               "returned_model": "unknown", "thinking": {"observation": "unknown"}}
        self.attempts.append(row)
        self._write()  # Durable before calling. A crash may conservatively waste a slot.
        start = time.monotonic()
        try:
            result = self.backend.complete(prompt, system=system, **kwargs)
            row.update(outcome="returned", provider_outcome="returned", output_bytes=len(result.encode()) if isinstance(result, str) else None)
            if isinstance(result, str):
                row["output_sha256"] = _hash(result.encode())
                if len(result.encode()) <= MAX_TRACE_BYTES:
                    trace["response"] = result
                else:
                    trace["response_omitted"] = "output_limit_exceeded"
                atomic_write_json(trace_path, trace)
            return result
        except Exception:
            if row["outcome"] != "returned":
                row["outcome"] = "error"
            raise
        finally:
            row["duration_seconds"] = round(time.monotonic() - start, 6)
            # Opaque backend metrics do not reach disk. Missing usage remains null.
            consume = getattr(self.backend, "consume_call_metrics", None)
            try:
                metrics = consume() if callable(consume) else None
            except Exception:
                metrics = None
                row["metrics_status"] = "unavailable"
            if isinstance(metrics, Mapping):
                usage = {k: v for k, v in metrics.items() if k in _USAGE_FIELDS and _integer(v, 0, 10_000_000)}
                row["usage"] = usage or None
                row["returned_model"] = _label(metrics.get("response_model"))
                for key, allowed in {
                    "thinking_requested": {"default", "disabled", "low", "high", "max"},
                    "thinking_effective": {"unknown", "unsupported", "reasoning_observed", "no_reasoning_observed"},
                    "thinking_observation_source": {"unavailable", "request_parameter", "reasoning_content", "reasoning_tokens"},
                }.items():
                    value = metrics.get(key)
                    if isinstance(value, str) and value in allowed:
                        row["thinking"][key] = value
                if type(metrics.get("thinking_applied")) is bool:
                    row["thinking"]["thinking_applied"] = metrics["thinking_applied"]
            self._write()


def _observe(service: Memleaf) -> list[dict]:
    scan = scan_memories(service.vault, include_history=False)
    if scan.report()["issue_count"]:
        raise AcceptanceError("acceptance_memory_scan_incomplete")
    # Parse exact current records, including retracted identity heads. No hit writes.
    return [{key: item.memory.to_dict().get(key) for key in _CHECK_FIELDS | {"title", "body", "due_text"}}
            for item in scan.records if item.area == "knowledge"]


def _checks(expected: dict, records: list[dict], observations: list[dict], calls: int, result: dict) -> list[dict]:
    def matches(record, values):
        return all(record.get(k) == v for k, v in values.items())
    results = []
    for key, value in expected.items():
        if key == "count":
            good = len(records) == value
        elif key == "same_ids_as":
            good = {r["memory_id"] for r in records} == {r["memory_id"] for r in observations[value]["memories"]}
        elif key == "max_calls":
            good = calls <= value
        elif key in {"execution_status", "coverage_status"}:
            actual = result.get(key) if key == "execution_status" else (result.get("commit") or {}).get(key)
            good = actual == value
        elif key == "required":
            good = all(any(matches(r, wanted) for r in records) for wanted in value)
        else:  # forbidden
            good = all(not any(matches(r, forbidden) for r in records) for forbidden in value)
        results.append({"check": key, "passed": good})
    results.append({"check": "per_work_request_cap", "passed": result.get("reserved_requests", 0) <= 2})
    return results


def execute_suite(suite: dict, *, output: Path | str, backend: Any, max_requests: int,
                  repeats: int = 5, authorize_model: bool = False, fixture: bool = False) -> dict:
    """Run only new isolated Vaults. Fixture=True is reserved for offline tests.

    Output must not exist. This command does not resume a prior acceptance run
    or infer an unspent allowance after a crash. A new run is a new test approval.
    """
    suite = validate_suite(suite)
    plan = plan_suite(suite, repeats=repeats)
    if type(fixture) is not bool or type(authorize_model) is not bool or (not fixture and not authorize_model):
        raise AcceptanceError("model_authorization_required")
    if not _integer(max_requests, 1, MAX_REQUESTS):
        raise AcceptanceError("invalid_acceptance_request_cap")
    if getattr(backend, "single_pass_safe", False) is not True or not callable(getattr(backend, "complete", None)):
        raise AcceptanceError("backend_not_single_dispatch")
    output = Path(output).expanduser()
    if not output.parent.is_dir() or output.exists() or output.is_symlink():
        raise AcceptanceError("new_output_directory_required")
    output.mkdir(mode=0o700)  # Exclusive; do not overwrite another approval's ledger.
    output = output.resolve()
    meter = _Meter(backend, output, max_requests)
    report = {**plan, "mode": "fixture" if fixture else "live", "execution_status": "running",
              "model_calls": 0 if fixture else None,
              "semantic_status": "not_run" if fixture else "not_reviewed", "runtime": _runtime(),
              "backend": _backend_identity(backend), "results": [], "budget_exhausted": False,
              "stop_reason": None, "native_host_status": "not_run", "fixture_origin": "synthetic_or_explicitly_supplied",
              "production_vault_used": False}
    atomic_write_json(output / "suite.json", suite)
    atomic_write_json(output / "report.json", report)
    for repetition in range(repeats):
        for case in suite["cases"]:
            if len(meter.attempts) >= max_requests:
                meter.exhausted = True
                break
            folder = output / f'{case["id"]}-{repetition + 1:02d}'
            folder.mkdir(mode=0o700)
            service = Memleaf.initialize(folder / "vault")
            for memory in case.get("initial_memories", []):
                service.create_memory(**memory, created="2026-01-01T00:00:00Z", updated="2026-01-01T00:00:00Z")
            summary = {"case_id": case["id"], "split": case["split"], "repeat": repetition + 1,
                       "status": "running", "steps": [], "semantic_status": report["semantic_status"]}
            report["results"].append(summary)
            observations: list[dict] = []
            for index, turn in enumerate(case["turns"]):
                if len(meter.attempts) >= max_requests:
                    meter.exhausted = True
                    break
                before = len(meter.attempts)
                meter.scope = {"case_id": case["id"], "repeat": repetition + 1, "step": index + 1}
                start = time.monotonic()
                try:
                    for position, message in enumerate(turn["messages"]):
                        fields = {k: v for k, v in message.items() if k not in {"role", "content"}}
                        fields.setdefault("message_id", f'{turn["id"]}-{position}')
                        service.capture("hermes", "acceptance", turn["id"], message["role"], message["content"], **fields)
                    result = service.process_incremental(source="hermes", session_id="acceptance", turn_id=turn["id"],
                                                         scope=turn.get("scope"), model=meter)
                    if result.get("code") in _STOP_CODES:
                        report["stop_reason"] = result["code"]
                    records = _observe(service)
                    checks = _checks(turn.get("expected", {}), records, observations, len(meter.attempts) - before, result)
                    observations.append({"turn_id": turn["id"], "memories": records, "result": result})
                    step = {"turn_id": turn["id"], "execution_status": result["execution_status"],
                            "coverage_status": (result.get("commit") or {}).get("coverage_status", "unknown"),
                            "assertions": checks, "status": "passed" if all(x["passed"] for x in checks) and result["execution_status"] == turn.get("expected", {}).get("execution_status", "completed") else "failed"}
                except Exception as error:
                    # Preserve earlier successful evidence. Exception text may contain credentials.
                    step = {"turn_id": turn["id"], "status": "error", "code": "replay_step_failed", "assertions": []}
                    if isinstance(error, AcceptanceError):
                        step["code"] = str(error)
                step.update(model_reservations=len(meter.attempts) - before,
                            duration_seconds=round(time.monotonic() - start, 6))
                summary["steps"].append(step)
                atomic_write_json(folder / "observations.json", {"steps": observations, "semantic_checks": case["semantic_checks"],
                                   "semantic_status": report["semantic_status"], "review_required": True})
                atomic_write_json(output / "report.json", report)
                if step["status"] == "error" or meter.exhausted or report["stop_reason"]:
                    break
            summary["status"] = ("passed" if len(summary["steps"]) == len(case["turns"]) and all(s["status"] == "passed" for s in summary["steps"])
                                   else "failed")
            if meter.exhausted or report["stop_reason"]:
                break
        if meter.exhausted or report["stop_reason"]:
            break
    counts = Counter(item["status"] for item in report["results"])
    expected_cases = len(suite["cases"]) * repeats
    expected_steps = plan["turns_per_repeat"] * repeats
    observed_steps = sum(len(case["steps"]) for case in report["results"])
    report.update(execution_status="completed" if observed_steps == expected_steps else "incomplete",
                  steps_expected=expected_steps, steps_run=observed_steps,
                  structural_status="passed" if counts["passed"] == expected_cases else "failed",
                  case_repetitions_expected=expected_cases, case_repetitions_run=len(report["results"]),
                  case_repetitions_passed=counts["passed"], budget_exhausted=meter.exhausted,
                  model_calls=0 if fixture else None, backend_reservations=len(meter.attempts),
                  confirmed_responses=sum(r["outcome"] == "returned" for r in meter.attempts))
    atomic_write_json(output / "report.json", report)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plan or explicitly run an isolated multi-turn acceptance suite; no production switch.")
    parser.add_argument("--suite", required=True, type=Path)
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--authorize-model", action="store_true")
    parser.add_argument("--backend-config", type=Path)
    parser.add_argument("--max-requests", type=int)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        suite = read_suite(args.suite)
        plan = plan_suite(suite, repeats=args.repeat)
        if not args.execute:
            if args.authorize_model or args.backend_config is not None or args.max_requests is not None or args.output is not None:
                raise AcceptanceError("execution_options_require_execute")
            print(json.dumps(plan, ensure_ascii=False, sort_keys=True))
            return 0
        if not args.authorize_model or args.backend_config is None or args.max_requests is None or args.output is None:
            raise AcceptanceError("explicit_execution_options_required")
        if not _integer(args.max_requests, 1, MAX_REQUESTS):
            raise AcceptanceError("invalid_acceptance_request_cap")
        if not args.output.parent.is_dir() or args.output.exists() or args.output.is_symlink():
            raise AcceptanceError("new_output_directory_required")
        # Copy only model routing into memory. Never instantiate the configured Vault
        # or copy its real native sources into the synthetic test environment.
        from .incremental_runtime import _resolve
        from .llm import ModelRouter
        config = load_config(args.backend_config)
        if config["llm"]["thinking"]["single_pass"] != "disabled":
            raise AcceptanceError("acceptance_requires_disabled_thinking")
        backend = _resolve(None, None, ModelRouter.from_config({"llm": config["llm"]}))
        result = execute_suite(suite, output=args.output, backend=backend, max_requests=args.max_requests,
                               repeats=args.repeat, authorize_model=True)
        public = {k: v for k, v in result.items() if k not in {"results", "backend", "runtime"}}
        print(json.dumps(public, ensure_ascii=False, sort_keys=True))
        return 0 if result["structural_status"] == "passed" else 2
    except Exception as error:
        code = str(error) if isinstance(error, AcceptanceError) else "acceptance_failed"
        print(json.dumps({"execution_status": "blocked", "code": code, "switch_authorized": False}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

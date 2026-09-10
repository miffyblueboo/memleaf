#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from memleaf import Memleaf
from memleaf.config import load_config
from memleaf.credentials import credential_text
from memleaf.llm import ModelRouter, ModelUnavailable
from benchmarks.p1.fixture import (
    build_plan,
    evaluation_template,
    load_cases,
    prepare_case_vault,
    selected_cases,
)
from benchmarks.p1.reporting import call_graph, safe_failure, safe_process_result, snapshot


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _git_sha(cwd: Path) -> str | None:
    try:
        value = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=cwd, text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return value if len(value) == 40 else None


class BudgetedModel:
    """Hard-cap delegated model calls while preserving structural provider metrics."""

    def __init__(self, backend: Any, max_calls: int):
        if not hasattr(backend, "complete"):
            raise TypeError("budgeted backend must expose complete()")
        if isinstance(max_calls, bool) or not isinstance(max_calls, int) or max_calls < 1:
            raise ValueError("max_calls must be a positive integer")
        self.backend = backend
        self.max_calls = max_calls
        self._calls = 0
        self._lock = threading.Lock()
        self.provider = getattr(backend, "provider", "unknown")
        self.model = getattr(backend, "model", "unknown")

    @property
    def parallel_safe(self) -> bool:
        return getattr(self.backend, "parallel_safe", False) is True

    @property
    def structured_batch_safe(self) -> bool:
        return getattr(self.backend, "structured_batch_safe", False) is True

    @property
    def calls(self) -> int:
        with self._lock:
            return self._calls

    def complete(self, prompt: str, *, system: str = "", purpose: str = "", temperature: float = 0.0) -> str:
        with self._lock:
            if self._calls >= self.max_calls:
                raise ModelUnavailable("P1 model call budget exhausted")
            self._calls += 1
        return self.backend.complete(prompt, system=system, purpose=purpose, temperature=temperature)

    def consume_call_metrics(self) -> dict[str, Any]:
        consume = getattr(self.backend, "consume_call_metrics", None)
        if not callable(consume):
            return {}
        try:
            value = consume()
        except Exception:
            return {}
        return dict(value) if isinstance(value, Mapping) else {}


def _route_identity(template: Mapping[str, Any]) -> dict[str, Any]:
    llm = template.get("llm") if isinstance(template, Mapping) else None
    if not isinstance(llm, Mapping):
        return {"api_route_ready": False}
    env_name = llm.get("api_key_env")
    direct_key = credential_text(llm.get("api_key"))
    env_key = credential_text(os.environ.get(env_name)) if isinstance(env_name, str) and env_name else None
    router = ModelRouter.from_config(template, mode="api")
    base_url = llm.get("base_url")
    return {
        "provider": llm.get("provider") if isinstance(llm.get("provider"), str) else None,
        "protocol": llm.get("protocol") if isinstance(llm.get("protocol"), str) else None,
        "model": llm.get("model") if isinstance(llm.get("model"), str) else None,
        "mode": "api",
        "thinking": "low",
        "base_url_configured": isinstance(base_url, str) and bool(base_url.strip()),
        "credential_configured": direct_key is not None or env_key is not None,
        "api_route_ready": router.api is not None,
    }


def _snapshot_after(service: Memleaf) -> tuple[dict[str, Any] | None, int | None, bool, str | None]:
    try:
        after = snapshot(service)
        started = time.perf_counter()
        fresh_after = snapshot(Memleaf(service.vault.root))
        elapsed = max(0, round((time.perf_counter() - started) * 1000))
        return after, elapsed, fresh_after == after, None
    except Exception as error:
        return None, None, False, type(error).__name__


def run_case(
    case: Mapping[str, Any],
    *,
    template: Mapping[str, Any],
    fixed_time: str,
    repetition: int,
    arm_label: str,
    model: BudgetedModel,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix=f"memleaf-p1-{case['id']}-") as directory:
        service = prepare_case_vault(Path(directory) / "vault", case, template=template, fixed_time=fixed_time)
        before = snapshot(service)
        started = time.perf_counter()
        process_result: dict[str, Any] | None = None
        failure: dict[str, Any] | None = None
        try:
            raw_result = service.process(source=case["source"], session_id=case["session_id"], model=model)
            process_result = safe_process_result(raw_result)
            run_status = "success"
        except Exception as error:
            failure = safe_failure(error)
            run_status = "error"
        process_wall_ms = max(0, round((time.perf_counter() - started) * 1000))
        after, visibility_ms, visibility_consistent, visibility_error_type = _snapshot_after(service)
        metric_source = process_result if process_result is not None else failure or {}
        row: dict[str, Any] = {
            "case_id": case["id"],
            "category": case.get("category"),
            "arm": arm_label,
            "repetition": repetition,
            "run_status": run_status,
            "expected_rubric": list(case.get("expected", [])),
            "process_wall_ms": process_wall_ms,
            "fresh_instance_visibility_ms": visibility_ms,
            "fresh_instance_visibility_consistent": visibility_consistent,
            "before": before,
            "after": after,
            "call_graph": call_graph(metric_source),
        }
        if process_result is not None:
            row["process_result"] = process_result
        if failure is not None:
            row["failure"] = failure
        if visibility_error_type is not None:
            row["visibility_error_type"] = visibility_error_type
        return row


def _write_output(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run or inspect memleaf P1 baseline cases")
    here = Path(__file__).resolve().parent
    parser.add_argument("--cases-file", type=Path, default=here / "cases-v1.json.gz")
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--repetitions", type=int)
    parser.add_argument("--arm-label", default="B0")
    parser.add_argument("--config-template", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--max-process-runs", type=int, default=30)
    parser.add_argument("--max-model-calls", type=int)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)

    data = load_cases(args.cases_file)
    repetitions = args.repetitions or int(data.get("repetitions_per_case_per_arm", 3))
    if repetitions < 1:
        raise SystemExit("--repetitions must be >= 1")
    cases = selected_cases(data, args.case)
    plan = build_plan(data, cases, repetitions)
    plan["model_call_budget"] = args.max_model_calls
    if plan["planned_process_runs"] > args.max_process_runs:
        raise SystemExit(f"planned process runs {plan['planned_process_runs']} exceed --max-process-runs {args.max_process_runs}")

    if args.execute and (args.max_model_calls is None or args.max_model_calls < 1):
        raise SystemExit("--execute requires --max-model-calls >= 1")
    if args.execute and (args.config_template is None or args.output is None):
        raise SystemExit("--execute requires --config-template and --output")

    template: dict[str, Any] | None = None
    if args.config_template is not None:
        template = evaluation_template(load_config(args.config_template))
        plan["route"] = _route_identity(template)
    if not args.execute:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0

    assert template is not None
    route = _route_identity(template)
    if route.get("api_route_ready") is not True:
        raise SystemExit("config template cannot construct the pinned API Model Route")

    router = ModelRouter.from_config(template, mode="api")
    budgeted = BudgetedModel(router, args.max_model_calls)
    planned_process_runs = int(plan["planned_process_runs"])
    output: dict[str, Any] = {
        "schema_version": 2,
        "generated_at": _utc_now(),
        "runner_git_sha": _git_sha(Path.cwd()),
        "arm": args.arm_label,
        "source_case_file": args.cases_file.name,
        "baseline_refs": data.get("baseline_refs", {}),
        "provider": route.get("provider"),
        "protocol": route.get("protocol"),
        "model": route.get("model"),
        "thinking": "low",
        "planned_process_runs": planned_process_runs,
        "process_run_count": 0,
        "success_count": 0,
        "error_count": 0,
        "model_call_budget": args.max_model_calls,
        "model_calls_used": 0,
        "completed_plan": False,
        "stopped_reason": None,
        "price_usd": None,
        "price_note": "No price inferred by runner; derive cost only from verified provider pricing.",
        "results": [],
    }
    _write_output(args.output, output)

    stop = False
    for repetition in range(1, repetitions + 1):
        for case in cases:
            if budgeted.calls >= args.max_model_calls:
                output["stopped_reason"] = "model_call_budget_exhausted"
                stop = True
                break
            row = run_case(
                case,
                template=template,
                fixed_time=str(data.get("fixed_event_time", "2026-09-09T09:00:00Z")),
                repetition=repetition,
                arm_label=args.arm_label,
                model=budgeted,
            )
            output["results"].append(row)
            output["process_run_count"] = len(output["results"])
            output["success_count"] = sum(item.get("run_status") == "success" for item in output["results"])
            output["error_count"] = sum(item.get("run_status") != "success" for item in output["results"])
            output["model_calls_used"] = budgeted.calls
            if budgeted.calls >= args.max_model_calls and len(output["results"]) < planned_process_runs:
                output["stopped_reason"] = "model_call_budget_exhausted"
                stop = True
            _write_output(args.output, output)
            if stop:
                break
        if stop:
            break

    output["completed_plan"] = len(output["results"]) == planned_process_runs and output["stopped_reason"] is None
    output["model_calls_used"] = budgeted.calls
    if output["completed_plan"]:
        output["status"] = "complete" if output["error_count"] == 0 else "complete_with_errors"
    else:
        output["status"] = "partial"
    _write_output(args.output, output)
    print(json.dumps({
        "written": str(args.output),
        "process_run_count": output["process_run_count"],
        "model_calls_used": output["model_calls_used"],
        "status": output["status"],
        "stopped_reason": output["stopped_reason"],
    }, ensure_ascii=False))
    if output["stopped_reason"] == "model_call_budget_exhausted":
        return 2
    return 0 if output["completed_plan"] and output["error_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

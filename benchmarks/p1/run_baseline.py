#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from memleaf import Memleaf
from memleaf.config import load_config
from benchmarks.p1.fixture import build_plan, load_cases, prepare_case_vault, selected_cases
from benchmarks.p1.reporting import call_graph, safe_process_result, snapshot


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _git_sha(cwd: Path) -> str | None:
    try:
        value = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=cwd, text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return value if len(value) == 40 else None


def run_case(case: Mapping[str, Any], *, template: Mapping[str, Any], fixed_time: str, repetition: int, arm_label: str) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix=f"memleaf-p1-{case['id']}-") as directory:
        service = prepare_case_vault(Path(directory) / "vault", case, template=template, fixed_time=fixed_time)
        before = snapshot(service)
        started = time.perf_counter()
        result = service.process(source=case["source"], session_id=case["session_id"])
        process_wall_ms = max(0, round((time.perf_counter() - started) * 1000))
        after = snapshot(service)
        visibility_started = time.perf_counter()
        fresh_after = snapshot(Memleaf(service.vault.root))
        visibility_ms = max(0, round((time.perf_counter() - visibility_started) * 1000))
        if fresh_after != after:
            raise RuntimeError("fresh Memleaf instance did not observe committed Markdown state")
        safe_result = safe_process_result(result)
        return {
            "case_id": case["id"], "category": case.get("category"), "arm": arm_label,
            "repetition": repetition, "expected_rubric": list(case.get("expected", [])),
            "process_wall_ms": process_wall_ms, "fresh_instance_visibility_ms": visibility_ms,
            "before": before, "after": after, "process_result": safe_result,
            "call_graph": call_graph(safe_result),
        }


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
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)

    data = load_cases(args.cases_file)
    repetitions = args.repetitions or int(data.get("repetitions_per_case_per_arm", 3))
    if repetitions < 1:
        raise SystemExit("--repetitions must be >= 1")
    cases = selected_cases(data, args.case)
    plan = build_plan(data, cases, repetitions)
    if plan["planned_process_runs"] > args.max_process_runs:
        raise SystemExit(f"planned process runs {plan['planned_process_runs']} exceed --max-process-runs {args.max_process_runs}")
    if not args.execute:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    if args.config_template is None or args.output is None:
        raise SystemExit("--execute requires --config-template and --output")

    template = load_config(args.config_template)
    llm = template.get("llm") if isinstance(template, Mapping) else None
    if not isinstance(llm, Mapping) or not llm.get("provider") or not llm.get("model"):
        raise SystemExit("config template must pin llm.provider and exact llm.model")

    results = []
    for repetition in range(1, repetitions + 1):
        for case in cases:
            results.append(run_case(case, template=template, fixed_time=str(data.get("fixed_event_time", "2026-09-09T09:00:00Z")), repetition=repetition, arm_label=args.arm_label))
    output = {
        "schema_version": 1, "generated_at": _utc_now(), "runner_git_sha": _git_sha(Path.cwd()),
        "arm": args.arm_label, "source_case_file": args.cases_file.name,
        "baseline_refs": data.get("baseline_refs", {}), "provider": llm.get("provider"),
        "protocol": llm.get("protocol"), "model": llm.get("model"), "thinking": "low",
        "process_run_count": len(results), "price_usd": None,
        "price_note": "No price inferred by runner; derive cost only from verified provider pricing.",
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"written": str(args.output), "process_run_count": len(results)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

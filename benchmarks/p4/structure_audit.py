#!/usr/bin/env python3
from __future__ import annotations

import json
import math


def _p3_common_create_calls(candidate_count: int) -> dict[str, int]:
    return {
        "identification_gate": 1,
        "target_reconciliation": 0,
        "summary": math.ceil(candidate_count / 2),
        "semantic_review": math.ceil(candidate_count / 4),
    }


def _p3_fresh_target_calls(candidate_count: int) -> dict[str, int]:
    return {
        "identification_gate": 1,
        "target_reconciliation": candidate_count,
        "summary": candidate_count,
        "semantic_review": math.ceil(candidate_count / 4),
    }


def _p4_calls(candidate_count: int) -> dict[str, int]:
    return {
        "identification": 1,
        "core_lookup_model_calls": 0,
        "maintenance_plan": math.ceil(candidate_count / 4),
        "semantic_review": math.ceil(candidate_count / 4),
    }


def _total(value: dict[str, int]) -> int:
    return sum(value.values())


def build_report() -> dict:
    scenarios = []
    for count in (1, 2, 4):
        p3 = _p3_common_create_calls(count)
        p4 = _p4_calls(count)
        scenarios.append({
            "scenario": "ordinary_independent_create",
            "candidate_count": count,
            "p3_stage_calls": p3,
            "p3_total_model_calls": _total(p3),
            "p4_stage_calls": p4,
            "p4_total_model_calls": _total(p4),
            "call_reduction_ratio": round((_total(p3) - _total(p4)) / _total(p3), 6),
        })

    count = 4
    p3 = _p3_fresh_target_calls(count)
    p4 = _p4_calls(count)
    scenarios.append({
        "scenario": "conditional_all_candidates_require_fresh_target_reconciliation",
        "candidate_count": count,
        "p3_stage_calls": p3,
        "p3_total_model_calls": _total(p3),
        "p4_stage_calls": p4,
        "p4_total_model_calls": _total(p4),
        "call_reduction_ratio": round((_total(p3) - _total(p4)) / _total(p3), 6),
    })

    return {
        "schema_version": 1,
        "measurement": "derived_structural_call_graph_zero_model_calls",
        "p3_summary_batch_size": 2,
        "p3_review_batch_size": 4,
        "p4_maintenance_batch_size": 4,
        "p4_review_batch_size": 4,
        "scenarios": scenarios,
        "notes": [
            "Ordinary scenarios assume the visible turn fits one stage-one Gate/identification call; larger evidence batching is outside this proxy.",
            "The fresh-target scenario is conditional, not a claim about ordinary frequency.",
            "This report measures call-graph structure only, not tokenizer tokens, latency, model quality, provider behavior, or production throughput.",
            "P4 still retains independent semantic review; no review omission is credited as a speedup.",
        ],
    }


def main() -> int:
    print(json.dumps(build_report(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

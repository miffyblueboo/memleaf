#!/usr/bin/env python3
from __future__ import annotations

import json

from tests.test_batch_review_integration import run_create_case


def build_report() -> dict:
    scenarios = []
    for request_count in (1, 2, 4, 5, 8):
        result, executor = run_create_case(request_count)
        actual_calls = len(executor.calls)
        legacy_calls = request_count
        scenarios.append({
            "reviewable_create_requests": request_count,
            "legacy_review_calls": legacy_calls,
            "p3_review_calls": actual_calls,
            "calls_removed": legacy_calls - actual_calls,
            "call_reduction_ratio": round(
                (legacy_calls - actual_calls) / legacy_calls,
                6,
            ),
            "result_count": len(result),
        })
    return {
        "schema_version": 1,
        "measurement": "synthetic_fake_executor_call_count_zero_real_model_calls",
        "max_review_batch_items": 4,
        "semantic_contract": "unchanged_per_item_review_parser_with_single-item_fallback",
        "scenarios": scenarios,
        "note": (
            "Structural call-count evidence only; not a real-model token, latency, "
            "or semantic-quality measurement."
        ),
    }


def main() -> int:
    print(json.dumps(build_report(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

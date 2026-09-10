#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping

_MILLION = Decimal(1_000_000)
_TOKEN_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "prompt_cache_hit_tokens",
    "prompt_cache_miss_tokens",
)


def _nonnegative_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _rate(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError) as error:
        raise argparse.ArgumentTypeError("rate must be a non-negative decimal") from error
    if not parsed.is_finite() or parsed < 0:
        raise argparse.ArgumentTypeError("rate must be a non-negative finite decimal")
    return parsed


def _iter_calls(document: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    results = document.get("results")
    if not isinstance(results, list):
        return
    for result in results:
        if not isinstance(result, Mapping):
            continue
        calls = result.get("call_graph")
        if not isinstance(calls, list):
            continue
        for call in calls:
            if isinstance(call, Mapping):
                yield call


def _money(tokens: int, rate_per_million: Decimal) -> Decimal:
    return Decimal(tokens) * rate_per_million / _MILLION


def summarize_cost(
    document: Mapping[str, Any],
    *,
    input_cache_hit_per_million: Decimal,
    input_cache_miss_per_million: Decimal,
    output_per_million: Decimal,
    currency: str,
    project_calls: int | None = None,
) -> dict[str, Any]:
    calls = list(_iter_calls(document))
    exact_total = Decimal(0)
    conservative_total = Decimal(0)
    exact_available = True
    conservative_available = True
    exact_call_costs: list[Decimal] = []
    conservative_call_costs: list[Decimal] = []
    availability = {field: 0 for field in _TOKEN_FIELDS}
    totals = {field: 0 for field in _TOKEN_FIELDS}
    exact_cache_breakdown_calls = 0

    for call in calls:
        tokens = {field: _nonnegative_int(call.get(field)) for field in _TOKEN_FIELDS}
        for field, value in tokens.items():
            if value is not None:
                availability[field] += 1
                totals[field] += value

        prompt = tokens["prompt_tokens"]
        completion = tokens["completion_tokens"]
        hit = tokens["prompt_cache_hit_tokens"]
        miss = tokens["prompt_cache_miss_tokens"]

        call_exact: Decimal | None = None
        if completion is not None and hit is not None and miss is not None:
            # A provider may expose the split without prompt_tokens. If prompt_tokens is
            # present, require equality so a partial cache breakdown cannot masquerade
            # as an exact bill.
            if prompt is None or hit + miss == prompt:
                call_exact = (
                    _money(hit, input_cache_hit_per_million)
                    + _money(miss, input_cache_miss_per_million)
                    + _money(completion, output_per_million)
                )
                exact_cache_breakdown_calls += 1
        if call_exact is None:
            exact_available = False
        else:
            exact_total += call_exact
            exact_call_costs.append(call_exact)

        call_conservative: Decimal | None = None
        if prompt is not None and completion is not None:
            input_upper_rate = max(input_cache_hit_per_million, input_cache_miss_per_million)
            call_conservative = _money(prompt, input_upper_rate) + _money(completion, output_per_million)
        if call_conservative is None:
            conservative_available = False
        else:
            conservative_total += call_conservative
            conservative_call_costs.append(call_conservative)

    result: dict[str, Any] = {
        "schema_version": 1,
        "source_schema_version": document.get("schema_version"),
        "provider": document.get("provider"),
        "model": document.get("model"),
        "thinking": document.get("thinking"),
        "currency": currency,
        "rates_per_million_tokens": {
            "input_cache_hit": str(input_cache_hit_per_million),
            "input_cache_miss": str(input_cache_miss_per_million),
            "output": str(output_per_million),
        },
        "call_count": len(calls),
        "model_calls_used_reported": document.get("model_calls_used"),
        "token_field_availability_calls": availability,
        "token_totals_observed": totals,
        "exact_cache_breakdown_calls": exact_cache_breakdown_calls,
        "exact_cost_available": bool(calls) and exact_available,
        "conservative_observed_cost_available": bool(calls) and conservative_available,
        "exact_observed_cost": str(exact_total) if calls and exact_available else None,
        "conservative_observed_cost": str(conservative_total) if calls and conservative_available else None,
        "notes": [
            "Exact cost requires completion tokens plus a complete cache-hit/cache-miss input split for every retained call.",
            "Conservative observed cost charges every observed prompt token at the more expensive input rate; it is not a bound on future calls.",
            "No token count is inferred from characters when provider usage is unavailable.",
        ],
    }

    if project_calls is not None:
        if isinstance(project_calls, bool) or project_calls < 1:
            raise ValueError("project_calls must be a positive integer")
        projection: dict[str, Any] = {
            "calls": project_calls,
            "basis": "maximum observed per-call cost; empirical projection, not a hard future bound",
            "exact_rate_projection": None,
            "conservative_rate_projection": None,
        }
        if exact_call_costs:
            projection["exact_rate_projection"] = str(max(exact_call_costs) * project_calls)
        if conservative_call_costs:
            projection["conservative_rate_projection"] = str(max(conservative_call_costs) * project_calls)
        result["projected_calls"] = projection

    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compute a provider-priced cost report from a P1 result JSON")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--input-cache-hit-per-million", type=_rate, required=True)
    parser.add_argument("--input-cache-miss-per-million", type=_rate, required=True)
    parser.add_argument("--output-per-million", type=_rate, required=True)
    parser.add_argument("--currency", required=True)
    parser.add_argument("--project-calls", type=int)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    document = json.loads(args.input.read_text(encoding="utf-8"))
    if not isinstance(document, Mapping):
        raise SystemExit("input must contain a JSON object")
    report = summarize_cost(
        document,
        input_cache_hit_per_million=args.input_cache_hit_per_million,
        input_cache_miss_per_million=args.input_cache_miss_per_million,
        output_per_million=args.output_per_million,
        currency=args.currency,
        project_calls=args.project_calls,
    )
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if report["conservative_observed_cost_available"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

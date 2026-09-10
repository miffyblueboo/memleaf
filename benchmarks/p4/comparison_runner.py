#!/usr/bin/env python3
"""Run the P4 responsibility-consolidation fixture without real model/Vault IO."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memleaf.admission import (  # noqa: E402
    EvidenceUnit,
    parse_coverage,
    split_gate_envelope,
    split_semantic_envelope,
    supporting_units,
    validate_bindings,
    validate_coverage_bindings,
)
from memleaf.candidate_identification import parse_identification_candidates  # noqa: E402
from memleaf.maintenance_plan_adapter import (  # noqa: E402
    adapt_core_lookup_contexts,
    compare_shadow_outcomes,
    make_existing_summary_validator,
)
from memleaf.maintenance_plan_stage import run_maintenance_plan_stage  # noqa: E402
from memleaf.validation import ModelOutputError, parse_summarize_output  # noqa: E402


DEFAULT_FIXTURE = Path(__file__).with_name("cases-v1.json")


class ScriptedStageTwoExecutor:
    """One-call executor used only to exercise the real P4 parser boundary."""

    def __init__(self, raw: str):
        self.raw = raw
        self.calls = 0

    def _complete_json_stage(
        self,
        _backend: Any,
        _prompt: str,
        *,
        system: str,
        purpose: str,
        parser: Any,
        diagnostic_context: Any = None,
    ) -> Any:
        if purpose != "summarize" or not isinstance(system, str) or not system:
            raise AssertionError("unexpected P4 stage-two model-call contract")
        self.calls += 1
        if self.calls != 1:
            raise AssertionError("scripted P4 comparison must make exactly one stage-two call per case")
        return parser(self.raw)


def _load_fixture(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("unsupported P4 comparison fixture")
    cases = value.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("P4 comparison fixture has no cases")
    return value


def _units(raw_rows: Any) -> list[EvidenceUnit]:
    if not isinstance(raw_rows, list) or not raw_rows:
        raise ValueError("comparison case requires evidence_units")
    result: list[EvidenceUnit] = []
    for raw in raw_rows:
        if not isinstance(raw, Mapping):
            raise ValueError("evidence unit must be an object")
        text = raw.get("text")
        unit_id = raw.get("unit_id")
        event_key = raw.get("event_key")
        origin = raw.get("origin")
        source_role = raw.get("source_role")
        if not all(isinstance(value, str) and value for value in (text, unit_id, event_key, origin, source_role)):
            raise ValueError("evidence unit is missing required text identity")
        result.append(EvidenceUnit(
            unit_id=unit_id,
            event_key=event_key,
            origin=origin,
            text=text,
            source_role=source_role,
            section_path=tuple(
                value for value in raw.get("section_path", [])
                if isinstance(value, str)
            ),
            start=0,
            end=len(text),
        ))
    return result


def _validated_identification(case: Mapping[str, Any]) -> tuple[
    list[dict[str, Any]],
    list[EvidenceUnit],
    dict[str, dict[str, Any]],
]:
    units = _units(case.get("evidence_units"))
    stage1 = case.get("stage1")
    if not isinstance(stage1, Mapping):
        raise ValueError("comparison case requires stage1 envelope")
    raw = json.dumps(stage1, ensure_ascii=False, separators=(",", ":"))
    without_coverage, coverage_value = split_gate_envelope(raw)
    semantic_raw, binding_value = split_semantic_envelope(without_coverage)
    parsed = parse_identification_candidates(
        semantic_raw,
        current_event_keys=[unit.event_key for unit in units],
    )
    candidates = parsed.get("candidates")
    if not isinstance(candidates, list):
        raise ModelOutputError("stage-one parser omitted candidates", validation_detail="root_shape")
    bindings = validate_bindings(binding_value, units, candidates)
    for candidate in candidates:
        candidate_id = candidate["candidate_id"]
        candidate["_evidence_bindings"] = bindings.get(candidate_id, [])
        candidate["_evidence_unit_ids"] = [
            claim["unit_id"]
            for claim in candidate["_evidence_bindings"]
            if isinstance(claim, Mapping) and isinstance(claim.get("unit_id"), str)
        ]
    coverage = parse_coverage(coverage_value, units, candidates, require_complete=True)
    validate_coverage_bindings(coverage, units, candidates)
    return candidates, units, coverage


def _admitted_evidence_by_candidate(
    candidates: list[dict[str, Any]],
    units: list[EvidenceUnit],
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        rows: list[dict[str, Any]] = []
        for unit in supporting_units(candidate, units):
            rows.append({
                "unit_id": unit.unit_id,
                "event_key": unit.event_key,
                "role": unit.source_role,
                "content": unit.text,
                "evidence_origin": unit.origin,
                "section_path": list(unit.section_path),
            })
        if not rows:
            raise ModelOutputError("candidate has no admitted support", validation_detail="invalid_evidence")
        result[candidate["candidate_id"]] = rows
    return result


def _summary_parser_factory(
    candidates: list[dict[str, Any]],
    evidence_by_candidate: Mapping[str, list[dict[str, Any]]],
    local_by_candidate: Mapping[str, list[dict[str, Any]]],
    native_by_candidate: Mapping[str, list[dict[str, Any]]],
):
    by_candidate = {candidate["candidate_id"]: candidate for candidate in candidates}

    def factory(candidate_id: str, decision: str, target: str | None):
        candidate = by_candidate[candidate_id]
        locals_ = local_by_candidate[candidate_id]
        native = native_by_candidate[candidate_id]
        target_type = None
        if decision == "UPDATE":
            target_row = next(
                (
                    row for row in locals_
                    if isinstance(row.get("memory_id"), str)
                    and isinstance(target, str)
                    and row["memory_id"].casefold() == target.casefold()
                ),
                None,
            )
            if target_row is None or not isinstance(target_row.get("type"), str):
                raise ModelOutputError("P4 UPDATE target lacks local type context", validation_detail="invalid_update_target")
            target_type = target_row["type"]

        def parse(raw: str) -> Mapping[str, Any]:
            return parse_summarize_output(
                raw,
                current_event_keys=[
                    row["event_key"]
                    for row in evidence_by_candidate[candidate_id]
                    if isinstance(row.get("event_key"), str)
                ],
                related_native_ids=[
                    row["native_id"]
                    for row in native
                    if isinstance(row.get("native_id"), str)
                ],
                related_memory_ids=[
                    row["memory_id"]
                    for row in locals_
                    if isinstance(row.get("memory_id"), str)
                ],
                scope_registry={},
                expected_type=candidate["type"],
                expected_update_memory_id=target if decision == "UPDATE" else None,
                expected_target_type=target_type,
                expected_scopes=candidate["scopes"],
                expected_scope_source=candidate["scope_source"],
                allow_no_change=False,
                allow_update_target=(decision == "UPDATE"),
            )

        return parse

    return factory


def run_case(case: Mapping[str, Any]) -> dict[str, Any]:
    case_id = case.get("case_id")
    if not isinstance(case_id, str) or not case_id:
        raise ValueError("comparison case requires case_id")
    candidates, units, coverage = _validated_identification(case)
    evidence_by_candidate = _admitted_evidence_by_candidate(candidates, units)
    related = case.get("related_by_candidate")
    completeness = case.get("lookup_complete_by_candidate")
    if not isinstance(related, Mapping) or not isinstance(completeness, Mapping):
        raise ValueError("comparison case requires Core lookup fixtures")
    lookup_states, local_by_candidate, native_by_candidate = adapt_core_lookup_contexts(
        candidates,
        related,
        lookup_complete_by_candidate=completeness,
        search_error_candidate_ids=case.get("search_error_candidate_ids", []),
        evidence_insufficient_candidate_ids=case.get("evidence_insufficient_candidate_ids", []),
    )
    validator = make_existing_summary_validator(
        _summary_parser_factory(
            candidates,
            evidence_by_candidate,
            local_by_candidate,
            native_by_candidate,
        )
    )
    stage2 = case.get("stage2")
    if not isinstance(stage2, Mapping):
        raise ValueError("comparison case requires scripted stage2 envelope")
    executor = ScriptedStageTwoExecutor(
        json.dumps(stage2, ensure_ascii=False, separators=(",", ":"))
    )
    outcomes = run_maintenance_plan_stage(
        executor,
        object(),
        candidates=candidates,
        evidence_by_candidate=evidence_by_candidate,
        lookup_states=lookup_states,
        related_memories_by_candidate=local_by_candidate,
        native_context_by_candidate=native_by_candidate,
        validate_summary=validator,
        diagnostic_context={"case_id": case_id},
    )
    baseline = case.get("baseline_dispositions")
    if not isinstance(baseline, list):
        raise ValueError("comparison case requires baseline_dispositions")
    parity = compare_shadow_outcomes(baseline, outcomes)
    return {
        "case_id": case_id,
        "candidate_count": len(candidates),
        "coverage_unit_count": len(coverage),
        "lookup_statuses": {
            candidate_id: lookup_states[candidate_id]["status"]
            for candidate_id in sorted(lookup_states)
        },
        "stage2_scripted_model_calls": executor.calls,
        "candidate_set_equal": parity["candidate_set_equal"],
        "decision_target_equal": parity["decision_target_equal"],
        "difference_count": len(parity["differences"]),
        "differences": parity["differences"],
    }


def build_report(path: Path = DEFAULT_FIXTURE) -> dict[str, Any]:
    fixture = _load_fixture(path)
    cases = [run_case(case) for case in fixture["cases"]]
    return {
        "schema_version": 1,
        "measurement": "scripted_end_to_end_contract_comparison_zero_real_model_calls_zero_vault_writes",
        "fixture_schema_version": fixture["schema_version"],
        "case_count": len(cases),
        "total_candidate_count": sum(case["candidate_count"] for case in cases),
        "total_stage2_scripted_model_calls": sum(case["stage2_scripted_model_calls"] for case in cases),
        "all_candidate_sets_equal": all(case["candidate_set_equal"] for case in cases),
        "all_decision_targets_equal": all(case["decision_target_equal"] for case in cases),
        "cases": cases,
        "notes": [
            "Stage-one output is scripted but validated through the existing Gate candidate, evidence-binding, and coverage validators.",
            "Stage-two output is scripted but passes through the real P4 maintenance protocol and existing parse_summarize_output constraints.",
            "No provider/model configuration is loaded, no external model call is possible, and no Vault/writer API is referenced.",
            "Parity here validates fixture plumbing and decision/target association only; it is not real-model semantic-quality evidence.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = build_report(args.input)
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0 if report["all_candidate_sets_equal"] and report["all_decision_targets_equal"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

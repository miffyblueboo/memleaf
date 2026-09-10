from __future__ import annotations

import json
from types import SimpleNamespace
import unittest

from memleaf.admission import (
    EvidenceUnit,
    parse_coverage,
    resolve_omitted_candidate_event_ids,
    split_gate_envelope,
    split_semantic_envelope,
    validate_bindings,
)
from memleaf.model_execution import (
    ModelExecutor,
    _coverage_shape_diagnostics,
    _validate_coverage_shape_repair,
)
from memleaf.prompts import (
    GATE_OUTPUT_PROTOCOL,
    GATE_PROTOCOL_EXAMPLE,
    GATE_STRUCTURE_REPAIR_SYSTEM,
)
from memleaf.validation import ModelOutputError, parse_gate_output


class _ServiceStub:
    def __init__(self):
        self.vault = SimpleNamespace(config=lambda: {"llm": {"diagnostic_logging": False}})


def _unit() -> EvidenceUnit:
    return EvidenceUnit(
        unit_id="u1",
        event_key="e1",
        origin="user_assertion",
        text="Alpha applies.",
        source_role="user",
    )


def _parse_gate(raw: str):
    unit = _unit()
    raw, binding_value = split_semantic_envelope(raw)
    raw, coverage_value = split_gate_envelope(raw)
    parsed = parse_gate_output(
        raw,
        current_event_keys=("e1",),
        allow_omitted_evidence_event_ids=True,
    )
    bindings = validate_bindings(binding_value, (unit,), parsed["candidates"])
    coverage = parse_coverage(
        coverage_value,
        (unit,),
        parsed["candidates"],
        require_complete=True,
    )
    resolve_omitted_candidate_event_ids(parsed["candidates"], bindings, (unit,))
    return parsed, coverage, bindings


class GateOutputProtocolV041Tests(unittest.TestCase):
    def test_documented_full_example_passes_real_gate_parsers(self):
        parsed, coverage, bindings = _parse_gate(GATE_PROTOCOL_EXAMPLE)
        self.assertEqual(parsed["candidates"][0]["candidate_id"], "c1")
        self.assertEqual(coverage["u1"]["decision"], "CANDIDATE")
        self.assertEqual(bindings["c1"][0]["quote"], "Alpha applies.")
        self.assertIn("Coverage rows may contain only", GATE_OUTPUT_PROTOCOL)
        self.assertIn("Each binding row is exactly", GATE_OUTPUT_PROTOCOL)

    def test_coverage_non_object_and_extra_field_remain_strictly_rejected(self):
        candidate = json.loads(GATE_PROTOCOL_EXAMPLE)["candidates"]
        with self.assertRaises(ModelOutputError) as non_object:
            parse_coverage(["bad-row"], (_unit(),), candidate)
        self.assertEqual(non_object.exception.evidence_check, "coverage_shape")

        row = {"unit_id": "u1", "decision": "CANDIDATE", "candidate_ids": ["c1"],
               "explanation": "not part of the protocol"}
        with self.assertRaises(ModelOutputError) as extra:
            parse_coverage([row], (_unit(),), candidate)
        self.assertEqual(extra.exception.evidence_check, "coverage_shape")

    def test_structural_diagnostic_exposes_only_shape_metadata(self):
        value = json.loads(GATE_PROTOCOL_EXAMPLE)
        value["coverage"][0]["explanation"] = "sensitive business prose must not be logged"
        diagnostic = _coverage_shape_diagnostics(json.dumps(value))
        self.assertEqual(diagnostic["coverage_row_index"], 0)
        self.assertEqual(diagnostic["coverage_actual_type"], "object")
        self.assertEqual(diagnostic["coverage_unexpected_fields"], ["explanation"])
        self.assertEqual(diagnostic["coverage_unexpected_field_count"], 1)
        self.assertEqual(
            set(diagnostic["coverage_allowed_fields"]),
            {"unit_id", "decision", "candidate_ids", "reason", "memory_id"},
        )
        serialized = json.dumps(diagnostic)
        self.assertNotIn("sensitive business prose", serialized)

        value["coverage"] = ["opaque-value"]
        diagnostic = _coverage_shape_diagnostics(json.dumps(value))
        self.assertEqual(diagnostic["coverage_row_index"], 0)
        self.assertEqual(diagnostic["coverage_actual_type"], "string")
        self.assertNotIn("opaque-value", json.dumps(diagnostic))

    def test_lossless_shape_repair_accepts_only_unknown_field_removal(self):
        invalid = json.loads(GATE_PROTOCOL_EXAMPLE)
        invalid["coverage"][0]["explanation"] = "extra"
        fixed = json.loads(GATE_PROTOCOL_EXAMPLE)
        _validate_coverage_shape_repair(json.dumps(invalid), json.dumps(fixed))

        changed_candidate = json.loads(GATE_PROTOCOL_EXAMPLE)
        changed_candidate["candidates"][0]["memory"] = "Beta applies."
        with self.assertRaises(ModelOutputError):
            _validate_coverage_shape_repair(json.dumps(invalid), json.dumps(changed_candidate))

        changed_scope = json.loads(GATE_PROTOCOL_EXAMPLE)
        changed_scope["candidates"][0]["scopes"] = ["project:Beta"]
        with self.assertRaises(ModelOutputError):
            _validate_coverage_shape_repair(json.dumps(invalid), json.dumps(changed_scope))

        dropped_candidate = json.loads(GATE_PROTOCOL_EXAMPLE)
        dropped_candidate["candidates"] = []
        with self.assertRaises(ModelOutputError):
            _validate_coverage_shape_repair(json.dumps(invalid), json.dumps(dropped_candidate))

        changed_link = json.loads(GATE_PROTOCOL_EXAMPLE)
        changed_link["coverage"][0]["candidate_ids"] = ["other"]
        with self.assertRaises(ModelOutputError):
            _validate_coverage_shape_repair(json.dumps(invalid), json.dumps(changed_link))

    def test_extra_coverage_field_uses_bounded_repair_without_original_evidence(self):
        invalid = json.loads(GATE_PROTOCOL_EXAMPLE)
        invalid["coverage"][0]["explanation"] = "extra"
        invalid_raw = json.dumps(invalid)
        fixed_raw = GATE_PROTOCOL_EXAMPLE
        prompts = []
        systems = []

        class Backend:
            def complete(self, prompt, *, system="", **kwargs):
                prompts.append(prompt)
                systems.append(system)
                return invalid_raw if len(prompts) == 1 else fixed_raw

        executor = ModelExecutor(_ServiceStub())
        result, coverage, bindings = executor._complete_json_stage(
            Backend(),
            "ORIGINAL-EVIDENCE-" + ("x" * 12000),
            system="PRIMARY-GATE-SYSTEM",
            purpose="gate",
            parser=_parse_gate,
        )
        self.assertEqual(len(prompts), 2)
        self.assertNotIn("ORIGINAL-EVIDENCE-", prompts[1])
        self.assertEqual(systems[1], GATE_STRUCTURE_REPAIR_SYSTEM)
        self.assertEqual(result["candidates"][0]["memory"], "Alpha applies.")
        self.assertEqual(coverage["u1"]["candidate_ids"], ["c1"])
        self.assertEqual(bindings["c1"][0]["quote"], "Alpha applies.")
        metrics = executor.metrics()
        self.assertEqual(metrics["operations"]["gate_primary"]["call_count"], 1)
        self.assertEqual(metrics["operations"]["gate_format_repair"]["call_count"], 1)
        self.assertLess(metrics["calls"][1]["input_chars"], metrics["calls"][0]["input_chars"])

    def test_semantic_change_in_targeted_repair_is_rejected_then_full_gate_retries(self):
        invalid = json.loads(GATE_PROTOCOL_EXAMPLE)
        invalid["coverage"][0]["explanation"] = "extra"
        changed = json.loads(GATE_PROTOCOL_EXAMPLE)
        changed["candidates"][0]["memory"] = "Beta applies."
        outputs = [json.dumps(invalid), json.dumps(changed), GATE_PROTOCOL_EXAMPLE]
        prompts = []
        systems = []

        class Backend:
            def complete(self, prompt, *, system="", **kwargs):
                prompts.append(prompt)
                systems.append(system)
                return outputs[len(prompts) - 1]

        executor = ModelExecutor(_ServiceStub())
        parsed, coverage, _bindings = executor._complete_json_stage(
            Backend(),
            "ORIGINAL-EVIDENCE-Alpha applies.",
            system="PRIMARY-GATE-SYSTEM",
            purpose="gate",
            parser=_parse_gate,
        )
        self.assertEqual(len(prompts), 3)
        self.assertEqual(systems[1], GATE_STRUCTURE_REPAIR_SYSTEM)
        self.assertEqual(systems[2], "PRIMARY-GATE-SYSTEM")
        self.assertIn("ORIGINAL-EVIDENCE-Alpha applies.", prompts[2])
        self.assertEqual(parsed["candidates"][0]["memory"], "Alpha applies.")
        self.assertEqual(coverage["u1"]["candidate_ids"], ["c1"])
        self.assertEqual(executor.metrics()["operations"]["gate_semantic_retry"]["call_count"], 1)

    def test_non_object_coverage_row_does_not_use_lossless_targeted_repair(self):
        invalid = json.loads(GATE_PROTOCOL_EXAMPLE)
        invalid["coverage"] = ["bad-row"]
        outputs = [json.dumps(invalid), GATE_PROTOCOL_EXAMPLE]
        systems = []

        class Backend:
            def complete(self, prompt, *, system="", **kwargs):
                systems.append(system)
                return outputs[len(systems) - 1]

        executor = ModelExecutor(_ServiceStub())
        parsed, coverage, _bindings = executor._complete_json_stage(
            Backend(),
            "ORIGINAL-EVIDENCE-Alpha applies.",
            system="PRIMARY-GATE-SYSTEM",
            purpose="gate",
            parser=_parse_gate,
        )
        self.assertEqual(len(systems), 2)
        self.assertEqual(systems, ["PRIMARY-GATE-SYSTEM", "PRIMARY-GATE-SYSTEM"])
        self.assertEqual(parsed["candidates"][0]["memory"], "Alpha applies.")
        self.assertEqual(coverage["u1"]["candidate_ids"], ["c1"])
        self.assertEqual(executor.metrics()["operations"]["gate_semantic_retry"]["call_count"], 1)


if __name__ == "__main__":
    unittest.main()

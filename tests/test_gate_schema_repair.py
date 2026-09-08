"""Schema retries retain enough prior proposal context to repair placement."""
import json
from types import SimpleNamespace
import unittest

from memleaf.admission import analyze_turn_evidence, split_gate_envelope, split_semantic_envelope, validate_bindings
from memleaf.model_execution import ModelExecutor
from memleaf.validation import ModelOutputError, parse_gate_output


class GateSchemaRepairTests(unittest.TestCase):
    @staticmethod
    def _scope_span_fixture():
        text = "Orion requires a release review."
        unit = analyze_turn_evidence([{
            "role": "user",
            "content": text,
            "event_key": "event-1",
        }])[0]
        valid_candidate = {
            "candidate_id": "c-1",
            "memory": text,
            "duplicate": False,
            "worth": True,
            "type": "todo",
            "scopes": ["project:Orion"],
            "scope_source": "model",
            "evidence_event_ids": ["event-1"],
        }
        scope_invalid_candidate = {
            **valid_candidate,
            "memory": "Alpha requires a release review.",
        }
        span_invalid = {
            "candidates": [valid_candidate],
            "coverage": [],
            "evidence_bindings": [{
                "candidate_id": "c-1",
                "claims": [{
                    "unit_id": unit.unit_id,
                    "quote": "Orion release review",
                    "role": "source_excerpt",
                }],
            }],
        }
        valid = {
            "candidates": [valid_candidate],
            "coverage": [],
            "evidence_bindings": [{
                "candidate_id": "c-1",
                "claims": [{
                    "unit_id": unit.unit_id,
                    "quote": text,
                    "role": "source_excerpt",
                }],
            }],
        }
        scope_invalid = {
            "candidates": [scope_invalid_candidate],
            "coverage": [],
            "evidence_bindings": [],
        }

        def parse(raw):
            raw, bindings = split_semantic_envelope(raw)
            raw, _ = split_gate_envelope(raw)
            parsed = parse_gate_output(
                raw,
                current_event_keys=("event-1",),
                scope_registry={"project:Orion": {}},
            )
            if bindings is not None:
                validate_bindings(bindings, (unit,), parsed["candidates"])
            return parsed

        return unit, parse, (
            json.dumps(scope_invalid),
            json.dumps(span_invalid),
            json.dumps(valid),
        )

    def test_nested_bindings_are_rejected_then_repaired_through_strict_parser(self):
        candidate = {"candidate_id": "c-1", "memory": "Alpha requires a release review.",
                     "duplicate": False, "worth": True, "type": "todo", "scopes": ["global"],
                     "scope_source": "model", "evidence_event_ids": ["event-1"]}
        invalid = json.dumps({"candidates": [{**candidate, "evidence_bindings": []}],
                              "coverage": [], "evidence_bindings": []})
        corrected = json.dumps({"candidates": [candidate], "coverage": [], "evidence_bindings": []})
        prompts = []

        class Backend:
            def complete(self, prompt, **kwargs):
                prompts.append(prompt)
                return invalid if len(prompts) == 1 else corrected

        def parse(raw):
            raw, _ = split_semantic_envelope(raw)
            raw, _ = split_gate_envelope(raw)
            return parse_gate_output(raw, current_event_keys=("event-1",))

        service = SimpleNamespace(vault=SimpleNamespace(config=lambda: {}))
        result = ModelExecutor(service)._complete_json_stage(
            Backend(), "Original immutable source evidence", system="Strict Gate", purpose="gate", parser=parse)
        self.assertEqual(len(prompts), 2)
        self.assertIn("TOP-LEVEL sibling", prompts[1])
        self.assertIn(invalid, prompts[1])
        self.assertIn("not new evidence or committed memories", prompts[1])
        self.assertEqual(result["candidates"][0]["memory"], candidate["memory"])
        self.assertNotIn("evidence_bindings", result["candidates"][0])

    def test_scope_scope_then_first_span_gets_one_whole_unit_recovery(self):
        unit, parse, (scope_invalid, span_invalid, valid) = self._scope_span_fixture()
        repaired = json.loads(valid)
        repaired["evidence_bindings"][0]["claims"] = [
            {"unit_id": unit.unit_id, "whole_unit": True, "role": "assertion"}]
        valid = json.dumps(repaired)
        prompts = []

        class Backend:
            def complete(self, prompt, **kwargs):
                prompts.append(prompt)
                return (
                    scope_invalid if len(prompts) <= 2
                    else span_invalid if len(prompts) == 3
                    else valid
                )

        service = SimpleNamespace(vault=SimpleNamespace(config=lambda: {}))
        result = ModelExecutor(service)._complete_json_stage(
            Backend(), "Original immutable source evidence", system="Strict Gate", purpose="gate", parser=parse)

        self.assertEqual(len(prompts), 4)
        self.assertIn("scope_not_grounded", prompts[1])
        self.assertIn("scope_not_grounded", prompts[2])
        self.assertIn("invalid_span", prompts[3])
        self.assertIn('"whole_unit":true', prompts[3])
        self.assertIn("Original immutable source evidence", prompts[3])
        self.assertEqual(result["candidates"][0]["memory"], "Orion requires a release review.")

    def test_later_scope_repair_keeps_prior_source_reference_constraint(self):
        unit, parse, (scope_invalid, span_invalid, valid) = self._scope_span_fixture()
        repaired = json.loads(valid)
        repaired["evidence_bindings"][0]["claims"] = [
            {"unit_id": unit.unit_id, "whole_unit": True, "role": "assertion"}]
        prompts = []

        class Backend:
            def complete(self, prompt, **kwargs):
                prompts.append(prompt)
                if len(prompts) == 1:
                    return span_invalid
                if len(prompts) == 2:
                    return scope_invalid
                return json.dumps(repaired)

        service = SimpleNamespace(vault=SimpleNamespace(config=lambda: {}))
        result = ModelExecutor(service)._complete_json_stage(
            Backend(), "Immutable evidence", system="Strict Gate", purpose="gate", parser=parse)
        self.assertEqual(len(prompts), 3)
        self.assertIn('"whole_unit":true', prompts[2])
        self.assertIn("scope_not_grounded", prompts[2])
        self.assertEqual(result["candidates"][0]["memory"], unit.text)

    def test_repeated_invalid_span_stays_within_three_attempts(self):
        _unit, parse, (_scope_invalid, span_invalid, _valid) = self._scope_span_fixture()
        prompts = []

        class Backend:
            def complete(self, prompt, **kwargs):
                prompts.append(prompt)
                return span_invalid

        service = SimpleNamespace(vault=SimpleNamespace(config=lambda: {}))
        with self.assertRaises(ModelOutputError) as raised:
            ModelExecutor(service)._complete_json_stage(
                Backend(), "Original immutable source evidence", system="Strict Gate", purpose="gate", parser=parse)

        self.assertEqual(len(prompts), 3)
        self.assertEqual(raised.exception.attempt_count, 3)

    def test_failed_fourth_span_recovery_raises_without_a_fifth_attempt(self):
        _unit, parse, (scope_invalid, span_invalid, _valid) = self._scope_span_fixture()
        prompts = []

        class Backend:
            def complete(self, prompt, **kwargs):
                prompts.append(prompt)
                return scope_invalid if len(prompts) <= 2 else span_invalid

        service = SimpleNamespace(vault=SimpleNamespace(config=lambda: {}))
        with self.assertRaises(ModelOutputError) as raised:
            ModelExecutor(service)._complete_json_stage(
                Backend(), "Original immutable source evidence", system="Strict Gate", purpose="gate", parser=parse)

        self.assertEqual(len(prompts), 4)
        self.assertEqual(raised.exception.attempt_count, 4)


if __name__ == "__main__":
    unittest.main()

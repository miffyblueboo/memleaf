from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from memleaf.admission import analyze_turn_evidence
from memleaf.config import DEFAULT_THINKING, load_config, save_config
from memleaf.llm.openai_compatible import OpenAICompatibleBackend
from memleaf.memory_planner import _model_project_scope_is_source_grounded
from memleaf.model_execution import ModelExecutor
from memleaf.prompts import GATE_COVERAGE_SYSTEM, GATE_SYSTEM, coverage_repair_prompt
from memleaf.update_review import CREATE_SEMANTIC_REVIEW_SYSTEM, UPDATE_SEMANTIC_REVIEW_SYSTEM


class _Response:
    def __init__(self, payload):
        self.payload = payload
    def read(self):
        return json.dumps(self.payload).encode("utf-8")
    def close(self):
        pass


class _Vault:
    def config(self):
        return {"process": {"model_concurrency": 1}, "llm": {"diagnostic_logging": False}}


class _Service:
    vault = _Vault()


def _candidate(unit, scope):
    return {
        "candidate_id": "c1",
        "memory": unit.text,
        "worth": True,
        "duplicate": False,
        "type": "project",
        "scopes": [scope],
        "scope_source": "model",
        "_evidence_bindings": [{
            "unit_id": unit.unit_id,
            "quote": unit.text,
            "role": "assertion",
        }],
    }


class V038ScopeGateLatencyTests(unittest.TestCase):
    def test_new_project_plus_registered_platform_is_not_a_name_conflict(self):
        units = analyze_turn_evidence([{
            "role": "user",
            "content": "Alpha 项目的监管改造属于 Alpha 项目，实际功能在 Orion 平台实施。",
            "event_key": "u1",
        }])
        self.assertTrue(_model_project_scope_is_source_grounded(
            _candidate(units[0], "project:Alpha"),
            units,
            {"project:Orion": {"aliases": ["Orion"]}},
        ))

    def test_registered_model_scope_also_requires_name_or_alias_in_bound_source(self):
        units = analyze_turn_evidence([{
            "role": "user", "content": "Alpha 项目的批量导入仍待确认。", "event_key": "u1"
        }])
        self.assertFalse(_model_project_scope_is_source_grounded(
            _candidate(units[0], "project:Orion"),
            units,
            {"project:Orion": {"aliases": ["OR-1"]}},
        ))
        alias_units = analyze_turn_evidence([{
            "role": "user", "content": "OR-1 项目的批量导入仍待确认。", "event_key": "u2"
        }])
        self.assertTrue(_model_project_scope_is_source_grounded(
            _candidate(alias_units[0], "project:Orion"),
            alias_units,
            {"project:Orion": {"aliases": ["OR-1"]}},
        ))

    def test_unsupported_new_project_stays_rejected(self):
        units = analyze_turn_evidence([{
            "role": "user", "content": "Orion 平台需要升级。", "event_key": "u1"
        }])
        self.assertFalse(_model_project_scope_is_source_grounded(
            _candidate(units[0], "project:Alpha"), units, {"project:Orion": {}}
        ))

    def test_semantic_review_treats_project_scope_as_affiliation_claim(self):
        for system in (CREATE_SEMANTIC_REVIEW_SYSTEM, UPDATE_SEMANTIC_REVIEW_SYSTEM):
            flattened = " ".join(system.split())
            self.assertIn("Scope is itself a claimed project affiliation", flattened)
            self.assertIn("implementation location is insufficient", flattened)
            self.assertIn("use DEFERRED", flattened)

    def test_gate_duplicate_tail_removed_and_coverage_protocol_is_narrow(self):
        self.assertLess(len(GATE_SYSTEM), 21000)
        self.assertLess(len(GATE_COVERAGE_SYSTEM), 7000)
        self.assertLess(len(GATE_COVERAGE_SYSTEM), len(GATE_SYSTEM))
        prompt = coverage_repair_prompt(
            "Evidence units:\nONLY-UNRESOLVED",
            related_memories=[{"memory_id": "m1", "title": "comparison"}],
            scope_background=["project:Alpha"],
            scope_registry=[{"scope": "project:Alpha"}],
            already_handled_candidate_ids=["done-1"],
        )
        self.assertIn("ONLY-UNRESOLVED", prompt)
        self.assertIn("done-1", prompt)
        self.assertNotIn("Complete turn events", prompt)

    def test_thinking_defaults_to_low_and_config_is_strict(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.yaml"
            config = load_config(path, vault=Path(temporary) / "vault")
            self.assertEqual(config["llm"]["thinking"], DEFAULT_THINKING)
            self.assertEqual(DEFAULT_THINKING, {
                "gate": "low", "summarize": "low", "compact": "low"
            })
            config["llm"]["thinking"]["gate"] = "high"
            save_config(path, config)
            self.assertEqual(load_config(path)["llm"]["thinking"]["gate"], "high")
            config = load_config(path)
            config["llm"]["thinking"]["gate"] = "turbo"
            with self.assertRaises(ValueError):
                save_config(path, config)

    def test_deepseek_low_thinking_and_usage_metrics_are_safe(self):
        captured = []
        def opener(request, timeout):
            del timeout
            captured.append(json.loads(request.data.decode("utf-8")))
            return _Response({
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"content": "{\"ok\":true}", "reasoning_content": "hidden"},
                }],
                "usage": {
                    "prompt_tokens": 120,
                    "completion_tokens": 30,
                    "total_tokens": 150,
                    "prompt_cache_hit_tokens": 80,
                    "prompt_cache_miss_tokens": 40,
                    "completion_tokens_details": {"reasoning_tokens": 22},
                },
            })
        backend = OpenAICompatibleBackend(
            base_url="https://example.invalid/v1",
            api_key="SECRET-KEY",
            model="deepseek-v4-flash",
            opener=opener,
            json_mode=True,
            provider_name="deepseek",
            thinking={"gate": "low"},
        )
        executor = ModelExecutor(_Service())
        value = executor._complete(
            backend,
            "SECRET-PROMPT",
            system="SECRET-SYSTEM",
            purpose="gate",
            metric_stage="gate",
            metric_operation="gate_primary",
        )
        self.assertEqual(value, '{"ok":true}')
        self.assertEqual(captured[0]["thinking"], {"type": "enabled"})
        self.assertEqual(captured[0]["reasoning_effort"], "low")
        call = executor.metrics()["calls"][0]
        self.assertEqual(call["prompt_tokens"], 120)
        self.assertEqual(call["prompt_cache_hit_tokens"], 80)
        self.assertEqual(call["reasoning_tokens"], 22)
        self.assertEqual(call["thinking_mode"], "low")
        serialized = json.dumps(executor.metrics(), ensure_ascii=False)
        self.assertNotIn("SECRET-PROMPT", serialized)
        self.assertNotIn("SECRET-SYSTEM", serialized)
        self.assertNotIn("SECRET-KEY", serialized)

    def test_deepseek_disabled_thinking_payload(self):
        captured = []
        def opener(request, timeout):
            del timeout
            captured.append(json.loads(request.data.decode("utf-8")))
            return _Response({
                "choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]
            })
        backend = OpenAICompatibleBackend(
            base_url="https://example.invalid/v1",
            api_key="key",
            model="deepseek-v4-flash",
            opener=opener,
            provider_name="deepseek",
            thinking={"gate": "disabled"},
        )
        self.assertEqual(backend.complete("x", purpose="gate"), "ok")
        self.assertEqual(captured[0]["thinking"], {"type": "disabled"})
        self.assertNotIn("reasoning_effort", captured[0])


if __name__ == "__main__":
    unittest.main()

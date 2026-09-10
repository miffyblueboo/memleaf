from __future__ import annotations

from pathlib import Path

ROOT = Path.cwd()

# --- Backend capability: only fixed built-in API routes may activate B3. ---
path = ROOT / "src/memleaf/llm/base.py"
text = path.read_text(encoding="utf-8")
text = text.replace(
    'MODEL_ERROR_STAGES = frozenset({"gate", "summarize"})',
    'MODEL_ERROR_STAGES = frozenset({"gate", "summarize", "single_pass"})',
)
text = text.replace(
    '    structured_batch_safe: bool\n\n    def complete(',
    '    structured_batch_safe: bool\n    single_pass_safe: bool\n\n    def complete(',
)
text = text.replace(
    '    structured_batch_safe = False\n\n    def __init__(self, callback:',
    '    structured_batch_safe = False\n    single_pass_safe = False\n\n    def __init__(self, callback:',
)
text = text.replace(
    '    structured_batch_safe = True\n\n    def __init__(',
    '    structured_batch_safe = True\n    single_pass_safe = True\n\n    def __init__(',
)
path.write_text(text, encoding="utf-8")

path = ROOT / "src/memleaf/llm/router.py"
text = path.read_text(encoding="utf-8")
anchor = '''    @staticmethod
    def _coerce_host(value: Any) -> Optional[ModelBackend]:
'''
insert = '''    @property
    def single_pass_safe(self) -> bool:
        """Expose B3 only when routing is fixed to a built-in safe API backend."""

        if self.mode == "api":
            return getattr(self.api, "single_pass_safe", False) is True
        if self.mode == "auto" and self.host is None:
            return getattr(self.api, "single_pass_safe", False) is True
        return False

'''
if text.count(anchor) != 1:
    raise SystemExit("router capability anchor missing")
text = text.replace(anchor, insert + anchor, 1)
path.write_text(text, encoding="utf-8")

# --- Thinking: the one B3 semantic call uses the same default low policy. ---
path = ROOT / "src/memleaf/llm/thinking.py"
text = path.read_text(encoding="utf-8")
old = 'THINKING_PURPOSES = frozenset({"gate", "summarize", "compact"})'
new = 'THINKING_PURPOSES = frozenset({"gate", "summarize", "compact", "single_pass"})'
if text.count(old) != 1:
    raise SystemExit("thinking purpose anchor missing")
path.write_text(text.replace(old, new, 1), encoding="utf-8")

# --- Diagnostics/failure metadata: separate B3 from legacy Gate schema. ---
path = ROOT / "src/memleaf/process_common.py"
text = path.read_text(encoding="utf-8")
text = text.replace(
    '    if stage not in {"gate", "summarize"}:',
    '    if stage not in {"gate", "summarize", "single_pass"}:',
)
anchor = '''_DIAGNOSTIC_SUMMARY_ALLOWED = frozenset(
    (
        "memory_id",
'''
if anchor not in text:
    raise SystemExit("diagnostic summary anchor missing")
# Put compact B3 diagnostic field sets before the summary set. Counts only, never field names/values.
insert = '''_DIAGNOSTIC_SINGLE_PASS_REQUIRED = frozenset(("protocol_version", "items", "no_memory"))
_DIAGNOSTIC_SINGLE_PASS_ALLOWED = _DIAGNOSTIC_SINGLE_PASS_REQUIRED
_DIAGNOSTIC_SINGLE_PASS_ITEM_REQUIRED = frozenset(("candidate_id", "decision", "evidence"))
_DIAGNOSTIC_SINGLE_PASS_ITEM_ALLOWED = frozenset((
    "candidate_id", "decision", "evidence", "type", "scopes", "scope_source",
    "memory", "target_memory_id", "reason",
))


'''
text = text.replace(anchor, insert + anchor, 1)
old_branch = '''    if purpose == "gate":
        required = _DIAGNOSTIC_GATE_REQUIRED
        allowed = _DIAGNOSTIC_GATE_ALLOWED
    elif purpose == "summarize":
        required = _DIAGNOSTIC_SUMMARY_REQUIRED
        allowed = _DIAGNOSTIC_SUMMARY_ALLOWED
    else:
        return stats
    stats["missing_fields_count"] = len(required - set(parsed))
    stats["unknown_fields_count"] = len(set(parsed) - allowed)
    if purpose != "gate":
        return stats
'''
new_branch = '''    if purpose == "gate":
        required = _DIAGNOSTIC_GATE_REQUIRED
        allowed = _DIAGNOSTIC_GATE_ALLOWED
    elif purpose == "summarize":
        required = _DIAGNOSTIC_SUMMARY_REQUIRED
        allowed = _DIAGNOSTIC_SUMMARY_ALLOWED
    elif purpose == "single_pass":
        required = _DIAGNOSTIC_SINGLE_PASS_REQUIRED
        allowed = _DIAGNOSTIC_SINGLE_PASS_ALLOWED
    else:
        return stats
    stats["missing_fields_count"] = len(required - set(parsed))
    stats["unknown_fields_count"] = len(set(parsed) - allowed)
    if purpose == "single_pass":
        items = parsed.get("items")
        if not isinstance(items, list):
            return stats
        stats["candidate_count"] = len(items)
        missing_count = stats["missing_fields_count"]
        unknown_count = stats["unknown_fields_count"]
        for item in items:
            if not isinstance(item, Mapping):
                continue
            missing_count += len(_DIAGNOSTIC_SINGLE_PASS_ITEM_REQUIRED - set(item))
            unknown_count += len(set(item) - _DIAGNOSTIC_SINGLE_PASS_ITEM_ALLOWED)
        stats["missing_fields_count"] = missing_count
        stats["unknown_fields_count"] = unknown_count
        return stats
    if purpose != "gate":
        return stats
'''
if text.count(old_branch) != 1:
    raise SystemExit("diagnostic purpose branch anchor missing")
text = text.replace(old_branch, new_branch, 1)
path.write_text(text, encoding="utf-8")

# --- ModelExecutor: explicit single_pass metrics and exactly one repair chance. ---
path = ROOT / "src/memleaf/model_execution.py"
text = path.read_text(encoding="utf-8")
text = text.replace(
    '    "target_reconciliation",\n})',
    '    "target_reconciliation",\n    "single_pass",\n})',
)
old_sig = '''        diagnostic_context: Mapping[str, Any] | None = None,
        metric_stage: str | None = None,
    ) -> Any:
        correction_prompt = prompt + "\\n\\n" + JSON_CORRECTION
'''
new_sig = '''        diagnostic_context: Mapping[str, Any] | None = None,
        metric_stage: str | None = None,
        max_attempts: int | None = None,
    ) -> Any:
        if max_attempts is None:
            effective_max_attempts = 4
        elif isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not 1 <= max_attempts <= 4:
            raise ValueError("max_attempts must be an integer from 1 to 4")
        else:
            effective_max_attempts = max_attempts
        correction_prompt = prompt + "\\n\\n" + JSON_CORRECTION
'''
if text.count(old_sig) != 1:
    raise SystemExit("complete_json_stage signature anchor missing")
text = text.replace(old_sig, new_sig, 1)
text = text.replace(
    '        for attempt_count in (1, 2, 3, 4):',
    '        for attempt_count in range(1, effective_max_attempts + 1):',
    1,
)
old_retry = '''                if self._allows_next_json_attempt(error, attempt_count) or extra_span_recovery:
                    if (
'''
new_retry = '''                can_retry = (
                    self._allows_next_json_attempt(error, attempt_count)
                    and attempt_count < effective_max_attempts
                )
                can_extra_span_recover = extra_span_recovery and attempt_count < effective_max_attempts
                if can_retry or can_extra_span_recover:
                    if (
'''
if text.count(old_retry) != 1:
    raise SystemExit("complete_json_stage retry anchor missing")
text = text.replace(old_retry, new_retry, 1)
path.write_text(text, encoding="utf-8")

# --- B3 call gets its own purpose and bounded one-repair policy. ---
path = ROOT / "src/memleaf/single_pass_plan.py"
text = path.read_text(encoding="utf-8")
old = '''        system=SINGLE_PASS_SYSTEM,
        purpose="gate",
        parser=lambda raw: parse_single_pass_output(
'''
new = '''        system=SINGLE_PASS_SYSTEM,
        purpose="single_pass",
        max_attempts=2,
        parser=lambda raw: parse_single_pass_output(
'''
if text.count(old) != 1:
    raise SystemExit("B3 stage purpose anchor missing")
path.write_text(text.replace(old, new, 1), encoding="utf-8")

# --- Planner falls back to P3 automatically unless backend route is explicitly safe. ---
path = ROOT / "src/memleaf/single_pass_memory_planner.py"
text = path.read_text(encoding="utf-8")
old = '''        # Explicit remember has different authorization semantics. Keep the
        # proven legacy route until the dedicated B3 explicit contract lands.
        if explicit:
            return super()._collect_turn_outputs(
                backend,
                turn,
                state,
                explicit=True,
                explicit_candidate=explicit_candidate,
                scope=scope,
            )

        authorized_project_scopes = _explicit_project_scope_authorizations(scope)
'''
new = '''        # Explicit remember is already a single summarize call. Host/custom
        # callbacks are caller-owned and stay on the proven P3 route. B3 is
        # activated only for a fixed built-in API route that advertises the
        # capability explicitly.
        if explicit or getattr(backend, "single_pass_safe", False) is not True:
            return super()._collect_turn_outputs(
                backend,
                turn,
                state,
                explicit=explicit,
                explicit_candidate=explicit_candidate,
                scope=scope,
            )

        authorized_project_scopes = _explicit_project_scope_authorizations(scope)
'''
if text.count(old) != 1:
    raise SystemExit("B3 fallback anchor missing")
path.write_text(text.replace(old, new, 1), encoding="utf-8")

# --- Processor selects the B3 planner class; unsafe routes self-fallback. ---
path = ROOT / "src/memleaf/processing.py"
text = path.read_text(encoding="utf-8")
text = text.replace(
    'from .memory_planner import MemoryPlanner\n',
    'from .single_pass_memory_planner import SinglePassMemoryPlanner\n',
)
old = '        self.planner = MemoryPlanner(service, audit=self.audit, model=self.model, inputs=self.inputs)'
new = '        self.planner = SinglePassMemoryPlanner(service, audit=self.audit, model=self.model, inputs=self.inputs)'
if text.count(old) != 1:
    raise SystemExit("Processor planner anchor missing")
path.write_text(text.replace(old, new, 1), encoding="utf-8")

# --- Focused activation tests. ---
test_path = ROOT / "tests/test_b3_activation.py"
test_path.write_text(r'''from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from memleaf.llm.base import CallableBackend, HTTPModelBackend, ModelError
from memleaf.llm.router import ModelRouter
from memleaf.llm.thinking import requested_thinking_mode
from memleaf.model_execution import ModelExecutor
from memleaf.process_common import _failure_metadata, _model_output_statistics
from memleaf.processing import Processor
from memleaf.single_pass_memory_planner import SinglePassMemoryPlanner
from memleaf.validation import ModelOutputError


class Api:
    parallel_safe = True
    structured_batch_safe = True
    single_pass_safe = True
    provider = "deepseek"
    model = "deepseek-v4-flash-vision-exp"
    def complete(self, prompt, *, system="", purpose="", temperature=0.0):
        return "{}"


class Vault:
    def config(self): return {"llm": {}}


class Service:
    vault = Vault()


class SequenceBackend:
    single_pass_safe = True
    parallel_safe = True
    structured_batch_safe = True
    provider = "test"
    model = "test"
    def __init__(self, values): self.values=list(values); self.calls=[]
    def complete(self, prompt, *, system="", purpose="", temperature=0.0):
        self.calls.append((prompt, system, purpose))
        return self.values.pop(0)


class B3ActivationTests(unittest.TestCase):
    def test_fixed_api_router_exposes_single_pass_but_host_routes_do_not(self):
        api = Api()
        self.assertTrue(ModelRouter(mode="api", api=api).single_pass_safe)
        self.assertTrue(ModelRouter(mode="auto", api=api).single_pass_safe)
        self.assertFalse(ModelRouter(mode="auto", api=api, host=lambda prompt: "{}").single_pass_safe)
        self.assertFalse(ModelRouter(mode="host", host=lambda prompt: "{}").single_pass_safe)
        self.assertFalse(CallableBackend(lambda prompt: "{}").single_pass_safe)

    def test_builtin_http_backend_advertises_single_pass(self):
        backend = object.__new__(HTTPModelBackend)
        self.assertTrue(backend.single_pass_safe)

    def test_single_pass_thinking_defaults_low(self):
        self.assertEqual(requested_thinking_mode({}, "single_pass"), "low")
        self.assertEqual(requested_thinking_mode({"single_pass": "disabled"}, "single_pass"), "disabled")

    def test_processor_constructs_single_pass_planner(self):
        # Avoid constructing all collaborators: this assertion protects the
        # production wiring without relying on source-text inspection.
        self.assertIn("SinglePassMemoryPlanner", Processor.__init__.__code__.co_names)

    def test_single_pass_failure_metadata_and_stats_are_stage_specific(self):
        error = ModelOutputError("bad", validation_detail="unknown_fields")
        error.stage = "single_pass"
        error.attempt_count = 2
        code, stage, reason, detail, attempts = _failure_metadata(error)
        self.assertEqual(stage, "single_pass")
        self.assertEqual(attempts, 2)
        stats = _model_output_statistics(json.dumps({
            "protocol_version": "b3-single-pass-v1",
            "items": [{"candidate_id": "c1", "decision": "CREATE", "evidence": [], "memory": {}, "extra": "secret-like"}],
            "no_memory": [],
        }), "single_pass")
        self.assertEqual(stats["candidate_count"], 1)
        self.assertEqual(stats["missing_fields_count"], 0)
        self.assertEqual(stats["unknown_fields_count"], 1)
        self.assertNotIn("extra", json.dumps(stats))
        self.assertNotIn("secret-like", json.dumps(stats))

    def test_single_pass_gets_only_one_repair_and_never_gate_repair_contract(self):
        executor = ModelExecutor(Service())
        backend = SequenceBackend([
            '{"bad":true}',
            '{"protocol_version":"b3-single-pass-v1","items":[],"no_memory":[]}',
        ])
        def parser(raw):
            value = json.loads(raw)
            if set(value) != {"protocol_version", "items", "no_memory"}:
                raise ModelOutputError("bad schema", validation_detail="unknown_fields")
            return value
        result = executor._complete_json_stage(
            backend,
            "ORIGINAL",
            system="B3SYSTEM",
            purpose="single_pass",
            parser=parser,
            max_attempts=2,
        )
        self.assertEqual(result["items"], [])
        self.assertEqual(len(backend.calls), 2)
        self.assertTrue(all(call[2] == "single_pass" for call in backend.calls))
        self.assertTrue(all(call[1] == "B3SYSTEM" for call in backend.calls))
        self.assertNotIn("Gate JSON", backend.calls[1][0])
        self.assertIn("unknown_fields", backend.calls[1][0])
        metrics = executor.metrics()
        self.assertEqual(metrics["stages"]["single_pass"]["call_count"], 2)
        self.assertEqual(metrics["stages"]["single_pass"]["retry_count"], 1)

    def test_single_pass_stops_after_second_invalid_output(self):
        executor = ModelExecutor(Service())
        backend = SequenceBackend(['{}', '{}', '{}'])
        def parser(raw):
            raise ModelOutputError("bad", validation_detail="invalid_evidence")
        with self.assertRaises(ModelOutputError) as caught:
            executor._complete_json_stage(
                backend, "ORIGINAL", system="B3SYSTEM", purpose="single_pass",
                parser=parser, max_attempts=2,
            )
        self.assertEqual(len(backend.calls), 2)
        self.assertEqual(caught.exception.attempt_count, 2)


if __name__ == "__main__":
    unittest.main()
''', encoding="utf-8")

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from memleaf import Memleaf
from memleaf.admission import EvidenceUnit
from memleaf.extraction_budget import SinglePassBudgetBackend
from memleaf.extraction_capability import supports_single_pass_protocol
from memleaf.llm import CallableBackend, ModelRouter
from memleaf.single_pass_plan import run_single_pass_stage


class _SafeApi:
    provider = "test-api"
    model = "test"
    single_pass_safe = True
    parallel_safe = True

    structured_batch_safe = True

    def complete(self, prompt, *, system="", purpose="", temperature=0.0):
        del prompt, system, purpose, temperature
        return "{}"


class _IncompatibleBackend:
    provider = "custom"
    model = "legacy"
    single_pass_safe = False

    def complete(self, prompt, *, system="", purpose="", temperature=0.0):
        del prompt, system, purpose, temperature
        return "{}"


class _CaptureExecutor:
    def __init__(self):
        self.backend = None

    def _complete_json_stage(
        self,
        backend,
        prompt,
        *,
        system,
        purpose,
        parser,
        diagnostic_context=None,
        max_attempts=None,
    ):
        del prompt, system, purpose, parser, diagnostic_context, max_attempts
        self.backend = backend
        return {"protocol_version": "b3-single-pass-v1", "items": [], "no_memory": []}


class SinglePassProtocolCapabilityV2Tests(unittest.TestCase):
    def test_callable_backend_is_protocol_capable_but_not_strict_sla_safe(self):
        backend = CallableBackend(lambda prompt, **kwargs: "{}")
        self.assertTrue(supports_single_pass_protocol(backend))
        self.assertFalse(backend.single_pass_safe)

    def test_host_router_uses_b3_protocol_without_claiming_strict_sla(self):
        router = ModelRouter(mode="host", host=lambda prompt, **kwargs: "{}")
        self.assertTrue(supports_single_pass_protocol(router))
        self.assertFalse(router.single_pass_safe)

    def test_auto_router_requires_every_reachable_route_to_speak_b3(self):
        good = ModelRouter(
            mode="auto",
            host=lambda prompt, **kwargs: "{}",
            api=_SafeApi(),
        )
        self.assertTrue(supports_single_pass_protocol(good))
        self.assertFalse(good.single_pass_safe)

        bad = ModelRouter(
            mode="auto",
            host=lambda prompt, **kwargs: "{}",
            api=_IncompatibleBackend(),
        )
        self.assertFalse(supports_single_pass_protocol(bad))

    def test_protocol_only_backend_is_not_silently_promoted_to_strict_budget(self):
        unit = EvidenceUnit(
            "unit-1",
            "event-1",
            "user_assertion",
            "Alpha uses PostgreSQL.",
            source_role="user",
        )
        protocol_only = CallableBackend(lambda prompt, **kwargs: "{}")
        executor = _CaptureExecutor()
        run_single_pass_stage(
            executor,
            protocol_only,
            evidence_units=[unit],
            validate_memory=lambda *args: {},
        )
        self.assertIs(executor.backend, protocol_only)

        strict = _SafeApi()
        executor = _CaptureExecutor()
        run_single_pass_stage(
            executor,
            strict,
            evidence_units=[unit],
            validate_memory=lambda *args: {},
        )
        self.assertIsInstance(executor.backend, SinglePassBudgetBackend)

    def test_prompt_only_host_callback_receives_inline_b3_contract(self):
        calls = []

        def host(prompt):
            calls.append(prompt)
            self.assertIn("You are memleaf's single-pass memory planner.", prompt)
            self.assertIn("B3_INPUT\n", prompt)
            payload = json.loads(
                prompt.split("B3_INPUT\n", 1)[1].split(
                    "\nReturn the complete strict B3 envelope.", 1
                )[0]
            )
            return json.dumps(
                {
                    "protocol_version": "b3-single-pass-v1",
                    "items": [],
                    "no_memory": [
                        {"unit_id": row["unit_id"], "reason": "no_future_value"}
                        for row in payload["current_evidence"]
                    ],
                },
                ensure_ascii=False,
            )

        with tempfile.TemporaryDirectory(prefix="memleaf-host-b3-") as tempdir:
            service = Memleaf(Path(tempdir) / "vault", model=host)
            service.capture(
                "hermes",
                "host-b3",
                "turn-1",
                "user",
                "今天天气不错。",
                event_id="host-b3-user",
            )
            service.capture(
                "hermes",
                "host-b3",
                "turn-1",
                "assistant",
                "是的。",
                event_id="host-b3-assistant",
            )

            result = service.process(source="hermes", session_id="host-b3")

        self.assertEqual(result["processed_turns"], 1)
        self.assertEqual(result["memories_written"], 0)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()

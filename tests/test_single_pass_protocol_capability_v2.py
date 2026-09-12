from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from memleaf import Memleaf
from memleaf.extraction_capability import supports_single_pass_protocol
from memleaf.llm import CallableBackend, ModelRouter


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

    def test_raw_host_callback_runs_ordinary_extraction_through_b3(self):
        calls = []

        def host(prompt, *, system="", purpose="", temperature=0.0):
            del temperature
            calls.append({"prompt": prompt, "system": system, "purpose": purpose})
            self.assertEqual(purpose, "single_pass")
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
        self.assertEqual(calls[0]["purpose"], "single_pass")


if __name__ == "__main__":
    unittest.main()

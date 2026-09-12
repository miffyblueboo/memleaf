from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from memleaf import Memleaf


class _FailIfCalledBackend:
    provider = "test"
    model = "must-not-run"
    single_pass_safe = True

    def __init__(self):
        self.calls = 0

    def complete(self, prompt, *, system="", purpose="", temperature=0.0):
        del prompt, system, purpose, temperature
        self.calls += 1
        raise AssertionError("read-only turn must not call the model")


class ReadOnlyZeroCallV2Tests(unittest.TestCase):
    def test_explicit_memory_write_disable_is_settled_without_model(self):
        with tempfile.TemporaryDirectory(prefix="memleaf-read-only-") as tempdir:
            service = Memleaf(Path(tempdir) / "vault")
            backend = _FailIfCalledBackend()
            service.capture(
                "hermes",
                "read-only",
                "turn-1",
                "user",
                "不要修改记忆",
                event_id="read-only-user",
            )
            service.capture(
                "hermes",
                "read-only",
                "turn-1",
                "assistant",
                "好的，我只回答当前问题。",
                event_id="read-only-assistant",
            )

            result = service.process(
                source="hermes",
                session_id="read-only",
                model=backend,
            )

            self.assertEqual(result["processed_turns"], 1)
            self.assertEqual(result["memories_written"], 0)
            self.assertEqual(result["memory_ids"], [])
            self.assertEqual(result["model_metrics"]["total"]["call_count"], 0)
            self.assertEqual(backend.calls, 0)
            self.assertEqual(service._read_memories_unlocked("knowledge"), [])

            replay = service.process(
                source="hermes",
                session_id="read-only",
                model=backend,
            )
            self.assertEqual(replay["processed_turns"], 0)
            self.assertEqual(replay["model_metrics"]["total"]["call_count"], 0)
            self.assertEqual(backend.calls, 0)


if __name__ == "__main__":
    unittest.main()

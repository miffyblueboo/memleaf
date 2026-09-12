from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from memleaf import Memleaf


class _FailIfCalledBackend:
    provider = "test"
    model = "backlog-no-model"
    single_pass_safe = True

    def __init__(self):
        self.calls = 0

    def complete(self, prompt, *, system="", purpose="", temperature=0.0):
        del prompt, system, purpose, temperature
        self.calls += 1
        raise AssertionError("explicit no-write backlog must not call the model")


class BacklogBoundV2Tests(unittest.TestCase):
    def test_one_process_call_settles_at_most_four_complete_turns(self):
        with tempfile.TemporaryDirectory(prefix="memleaf-backlog-") as tempdir:
            service = Memleaf(Path(tempdir) / "vault")
            backend = _FailIfCalledBackend()
            for index in range(1, 6):
                service.capture(
                    "hermes",
                    "session",
                    f"turn-{index}",
                    "user",
                    "不要修改记忆",
                    event_id=f"u-{index}",
                )
                service.capture(
                    "hermes",
                    "session",
                    f"turn-{index}",
                    "assistant",
                    "好的，只处理当前请求。",
                    event_id=f"a-{index}",
                )

            first = service.process(
                source="hermes",
                session_id="session",
                model=backend,
            )

            self.assertEqual(first["processed_turns"], 4)
            self.assertEqual(first["pending_inbox_turns"], 1)
            self.assertEqual(first["memories_written"], 0)
            self.assertEqual(first["model_metrics"]["total"]["call_count"], 0)
            self.assertEqual(backend.calls, 0)

            second = service.process(
                source="hermes",
                session_id="session",
                model=backend,
            )

            self.assertEqual(second["processed_turns"], 1)
            self.assertEqual(second["pending_inbox_turns"], 0)
            self.assertEqual(second["memories_written"], 0)
            self.assertEqual(backend.calls, 0)

            replay = service.process(
                source="hermes",
                session_id="session",
                model=backend,
            )
            self.assertEqual(replay["processed_turns"], 0)
            self.assertEqual(replay["pending_inbox_turns"], 0)
            self.assertEqual(backend.calls, 0)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from memleaf import Memleaf
from memleaf.host_runtime import HostRuntime
from memleaf.inbox import parse_inbox
from memleaf.retrieval_gate import validate_turn


class HostRuntimeContractTests(unittest.TestCase):
    def _runtime(self, root: Path, host: str) -> HostRuntime:
        return HostRuntime(Memleaf(root), host)

    def _exercise_no_match_turn(self, runtime: HostRuntime) -> tuple[str, list[tuple[str, str]]]:
        opened = runtime.open_turn(
            session_id="session-1",
            turn_id="turn-1",
            user_content="hello",
        )
        prepared = runtime.prepare_memory_tool(
            session_id="session-1",
            turn_id="turn-1",
            arguments={"query": "hello"},
        )
        self.assertTrue(prepared.allowed)
        self.assertEqual(opened.retrieval_id, prepared.retrieval_id)
        self.assertEqual(opened.retrieval_id, prepared.arguments["retrieval_id"])
        self.assertTrue(
            runtime.observe_search(
                session_id="session-1",
                turn_id="turn-1",
                status="no_match",
                call_id="call-1",
                supplied_retrieval_id=opened.retrieval_id,
            )
        )
        completed = runtime.complete_turn(
            session_id="session-1",
            turn_id="turn-1",
            assistant_content="hello back",
            auto_process=False,
        )
        self.assertFalse(completed.retry_required)
        self.assertFalse(completed.degraded)
        turns = parse_inbox(runtime.vault)
        self.assertEqual(1, len(turns))
        events = [(event.role, event.content) for event in turns[0].events]
        return opened.retrieval_id, events

    def test_same_lifecycle_contract_for_codex_and_future_host(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memleaf-host-runtime-") as temporary:
            root = Path(temporary)
            codex = self._runtime(root / "codex-vault", "codex")
            future = self._runtime(root / "future-vault", "future-agent")

            codex_id, codex_events = self._exercise_no_match_turn(codex)
            future_id, future_events = self._exercise_no_match_turn(future)

            self.assertEqual([("user", "hello"), ("assistant", "hello back")], codex_events)
            self.assertEqual(codex_events, future_events)
            self.assertEqual("NO_MATCH", validate_turn(codex.vault, codex_id)["status"])
            self.assertEqual("NO_MATCH", validate_turn(future.vault, future_id)["status"])

    def test_continuation_prompt_is_not_captured_as_business_text(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memleaf-host-runtime-") as temporary:
            runtime = self._runtime(Path(temporary) / "vault", "codex")
            opened = runtime.open_turn(
                session_id="session-1",
                turn_id="turn-1",
                user_content="hello",
            )
            blocked = runtime.complete_turn(
                session_id="session-1",
                turn_id="turn-1",
                assistant_content="must not persist yet",
            )
            self.assertTrue(blocked.retry_required)
            self.assertIsNotNone(blocked.retry_reason)

            resumed = runtime.open_turn(
                session_id="session-1",
                turn_id="turn-2",
                user_content=blocked.retry_reason,
            )
            self.assertTrue(resumed.continuation)
            self.assertEqual(opened.retrieval_id, resumed.retrieval_id)
            turns = parse_inbox(runtime.vault)
            self.assertEqual(1, len(turns))
            self.assertEqual(["hello"], [event.content for event in turns[0].events])

    def test_legacy_codex_ingest_state_is_preserved_and_migrated_lazily(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memleaf-host-runtime-") as temporary:
            runtime = self._runtime(Path(temporary) / "vault", "codex")
            runtime.vault.host_ingest_path.write_text(
                '{"version":1,"codex":{"legacy":{"process_pending":true,"injected_turn_ids":["old"]}},"transcripts":{}}',
                encoding="utf-8",
            )
            self.assertTrue(runtime.injection_delivered("legacy", "old"))
            runtime.mark_injection_delivered("legacy", "new")
            state = runtime.vault.host_ingest_path.read_text(encoding="utf-8")
            self.assertIn('"hosts"', state)
            self.assertIn('"codex"', state)
            self.assertIn('"old"', state)
            self.assertIn('"new"', state)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from memleaf import Memleaf
from memleaf.host_runtime import HostRuntime
from memleaf.index import event_key


class _QueueBackend:
    provider = "fake"
    model = "cross-host-acceptance"

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)

    def complete(self, prompt: str, *, system: str = "", purpose: str = "", temperature: float = 0.0) -> str:
        del prompt, system, purpose, temperature
        if not self.responses:
            raise AssertionError("cross-host acceptance model queue exhausted")
        return self.responses.pop(0)


def _model_responses(host: str, session: str, turn: str, *, title: str, body: str) -> list[str]:
    evidence = event_key(f"{host}/{session}/{turn}/user")
    return [
        json.dumps(
            {
                "candidates": [
                    {
                        "candidate_id": "candidate-1",
                        "memory": body,
                        "evidence_event_ids": [evidence],
                        "duplicate": False,
                        "worth": True,
                        "type": "fact",
                        "scopes": ["project:cross-host"],
                        "scope_source": "model",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        json.dumps(
            {
                "title": title,
                "body": body,
                "tags": ["cross-host"],
                "type": "fact",
                "scopes": ["project:cross-host"],
                "scope_source": "model",
                "sources": [{"event_key": evidence}],
            },
            ensure_ascii=False,
        ),
    ]


class CrossHostAcceptanceTests(unittest.TestCase):
    def _roundtrip(self, writer_host: str, reader_host: str) -> None:
        with tempfile.TemporaryDirectory(prefix=f"memleaf-{writer_host}-{reader_host}-") as temporary:
            vault = Path(temporary) / "共享 Vault"
            session = f"{writer_host}-session-a"
            turn = "turn-1"
            title = f"{writer_host} to {reader_host}"
            body = (
                f"cross-host project durable fact written by {writer_host} "
                f"and recalled by {reader_host}."
            )

            writer_service = Memleaf(
                vault,
                model=_QueueBackend(
                    _model_responses(
                        writer_host,
                        session,
                        turn,
                        title=title,
                        body=body,
                    )
                ),
            )
            writer = HostRuntime(writer_service, writer_host)
            opened = writer.open_turn(
                session_id=session,
                turn_id=turn,
                user_content=body,
            )
            self.assertTrue(
                writer.observe_search(
                    session_id=session,
                    turn_id=turn,
                    status="no_match",
                    call_id="writer-search",
                    supplied_retrieval_id=opened.retrieval_id,
                )
            )
            completed = writer.complete_turn(
                session_id=session,
                turn_id=turn,
                assistant_content="Recorded.",
                auto_process=True,
            )
            self.assertFalse(completed.retry_required)
            self.assertFalse(completed.process_failed)
            self.assertEqual(1, writer_service.stats()["knowledge"])

            # A fresh service/runtime represents a new host session/process and
            # must discover the fact only through the shared Vault.
            reader_service = Memleaf(vault)
            reader = HostRuntime(reader_service, reader_host)
            catalog = reader.scope_catalog()
            scopes = [item["scope"] for item in catalog["scopes"]]
            self.assertIn("project:cross-host", scopes)

            results = reader_service.search_candidates(
                "Durable fact written",
                scope="project:cross-host",
            )
            self.assertEqual("found", results["status"])
            self.assertGreaterEqual(len(results["results"]), 1)
            memory_id = results["results"][0]["memory_id"]
            memory = reader_service.read(memory_id)
            self.assertIsNotNone(memory)
            self.assertEqual(body, memory.body)

    def test_codex_to_codex_cross_session(self) -> None:
        self._roundtrip("codex", "codex")

    def test_hermes_to_codex_shared_vault(self) -> None:
        self._roundtrip("hermes", "codex")

    def test_codex_to_hermes_shared_vault(self) -> None:
        self._roundtrip("codex", "hermes")


if __name__ == "__main__":
    unittest.main()

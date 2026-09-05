import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from memleaf import Memleaf
from memleaf.config import save_config
from memleaf.index import event_key
from memleaf.native_index import NativeIndexError, NativeIndexer


class QueueBackend:
    provider = "fake"
    model = "b3b2b-test"

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    def complete(self, prompt, *, system="", purpose="", temperature=0.0):
        self.calls.append({"prompt": prompt, "purpose": purpose})
        if not self.responses:
            raise AssertionError("fake backend queue exhausted")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class StageB3BNativeContextTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.service = Memleaf(self.root / "vault")

    def tearDown(self):
        self.tempdir.cleanup()

    def set_sources(self, sources):
        config = self.service.vault.config()
        config["native_sources"] = sources
        save_config(self.service.vault.config_path, config)
        self.service.refresh_native_sources()

    def read_index(self):
        return json.loads(self.service.vault.native_sources_index_path.read_text(encoding="utf-8"))

    def capture_turn(self, *, source="codex", session="s", turn="t1", user="visible user", assistant="visible assistant"):
        self.service.capture(source, session, turn, "user", user, event_id=f"{turn}-user")
        self.service.capture(source, session, turn, "assistant", assistant, event_id=f"{turn}-assistant")
        return event_key(f"{turn}-user")

    @staticmethod
    def gate(candidate):
        return json.dumps({"candidates": [candidate] if candidate is not None else []})

    @staticmethod
    def candidate(event_key_value, *, memory="new state", duplicate=False, worth=True):
        return {
            "candidate_id": "candidate-1",
            "memory": memory,
            "evidence_event_ids": [event_key_value],
            "duplicate": duplicate,
            "worth": worth,
            "type": "fact",
            "scopes": ["global"],
            "scope_source": "model",
        }

    @staticmethod
    def summary(event_key_value, body, *, shadow=None):
        value = {
            "title": "New state",
            "body": body,
            "tags": ["native"],
            "type": "fact",
            "scopes": ["global"],
            "scope_source": "model",
            "sources": [{"event_key": event_key_value}],
        }
        if shadow is not None:
            value["shadow_native_ids"] = list(shadow)
        return json.dumps(value)

    def test_context_source_allowlist_and_own_source_process_visibility(self):
        own = self.root / "own.md"
        shared = self.root / "shared.md"
        private = self.root / "private.md"
        own.write_text("# Own\nownonly\n", encoding="utf-8")
        shared.write_text("# Shared\nsharedonly\n", encoding="utf-8")
        private.write_text("# Private\nprivateonly\n", encoding="utf-8")
        self.set_sources(
            {
                "own": {"agent": "codex", "path": str(own), "share": False},
                "shared": {"agent": "hermes", "path": str(shared), "share": True},
                "private": {"agent": "hermes", "path": str(private), "share": False},
            }
        )

        own_context = self.service.context("ownonly", source="codex", session_id="s")
        self.assertEqual([], own_context)
        shared_result = self.service.context("sharedonly", source="codex", session_id="s")
        self.assertEqual(len(shared_result), 1)
        native_id = shared_result[0].memory_id
        self.assertTrue(native_id.startswith("native-"))
        self.assertEqual({"memory_id", "title", "scopes"}, set(shared_result[0].to_dict()))
        self.assertEqual(
            [native_id],
            [entry.memory_id for entry in self.service.context(native_id, source="codex", session_id="s")],
        )
        self.assertEqual(self.service.context("privateonly", source="codex", session_id="s"), [])
        self.assertEqual(self.service.read_page(native_id)["body"], "# Shared\nsharedonly")

        event_key_value = self.capture_turn(user="ownonly", assistant="ack")
        backend = QueueBackend([self.gate(None)])
        self.service.process(model=backend)
        prompt = backend.calls[0]["prompt"]
        self.assertIn("ownonly", prompt)
        self.assertNotIn(str(own), prompt)
        self.assertNotIn("file_hash", prompt)
        self.assertNotIn("mtime_ns", prompt)
        self.assertIn(event_key_value, prompt)

    def test_context_dedupes_memleaf_first_and_limit_is_after_union(self):
        shared = self.root / "shared.md"
        shared_body = "# Shared\nidentical native body"
        shared.write_text(shared_body + "\n", encoding="utf-8")
        self.set_sources({"shared": {"agent": "hermes", "path": str(shared), "share": True}})
        local = self.service.create_memory(
            memory_id="local-copy",
            title="Local copy",
            body=shared_body,
            scopes=["global"],
        )
        before = shared.read_bytes()

        result = self.service.context("identical native body", limit=1)

        self.assertEqual([memory.memory_id for memory in result], [local.memory_id])
        self.assertEqual(shared.read_bytes(), before)
        self.assertEqual(self.service.read(local.memory_id).hit_count, 0)
        index = self.read_index()
        self.assertNotIn("hit_count", json.dumps(index))

    def test_partial_context_agent_arguments_are_rejected(self):
        with self.assertRaises(ValueError):
            self.service.context("anything", source="codex")
        with self.assertRaises(ValueError):
            self.service.context("anything", session_id="s")

    def test_process_duplicate_uses_native_background_but_explicit_remember_writes(self):
        own = self.root / "own.md"
        native_body = "# Own\nremembered native fact"
        own.write_text(native_body + "\n", encoding="utf-8")
        self.set_sources({"own": {"agent": "codex", "path": str(own), "share": False}})
        event_key_value = self.capture_turn(user=native_body, assistant="ack")
        backend = QueueBackend([self.gate(self.candidate(event_key_value, duplicate=True, worth=False))])

        result = self.service.process(model=backend)

        self.assertEqual(result["memories_written"], 0)
        self.assertEqual(self.service._read_memories_unlocked("knowledge"), [])
        self.assertIn("remembered native fact", backend.calls[0]["prompt"])

        remember_event = event_key("explicit-event")
        remember_backend = QueueBackend([self.summary(remember_event, native_body)])
        remembered = self.service.remember(
            native_body,
            source="codex",
            session_id="remember",
            turn_id="remember-turn",
            event_id="explicit-event",
            model=remember_backend,
        )
        self.assertEqual(remembered["memories_written"], 1)
        self.assertEqual(len(self.service._read_memories_unlocked("knowledge")), 1)
        self.assertEqual([call["purpose"] for call in remember_backend.calls], ["summarize"])

    def test_shadow_ids_are_strict_and_success_hides_old_native_without_writing_source(self):
        shared = self.root / "shared.md"
        native_body = "# Legacy\nlegacy status"
        shared.write_text(native_body + "\n", encoding="utf-8")
        self.set_sources({"shared": {"agent": "hermes", "path": str(shared), "share": True}})
        native_id = self.read_index()["sources"]["shared"]["segments"][0]["native_id"]
        before = shared.read_bytes()

        event_key_value = self.capture_turn(user="legacy status is now replaced", assistant="confirmed")
        invalid = QueueBackend(
            [
                self.gate(self.candidate(event_key_value, memory="legacy status is now replaced")),
                self.summary(event_key_value, "new status", shadow=["native-not-related"]),
                self.summary(event_key_value, "new status", shadow=["native-not-related"]),
                self.summary(event_key_value, "new status", shadow=["native-not-related"]),
            ]
        )
        with self.assertRaises(ValueError):
            self.service.process(model=invalid)
        self.assertEqual(self.service._read_memories_unlocked("knowledge"), [])

        backend = QueueBackend(
            [
                self.gate(self.candidate(event_key_value, memory="legacy status is now replaced")),
                self.summary(event_key_value, "new status", shadow=[native_id]),
            ]
        )
        result = self.service.process(model=backend)
        self.assertEqual(result["memories_written"], 1)
        self.assertEqual(shared.read_bytes(), before)
        segment = self.read_index()["sources"]["shared"]["segments"][0]
        self.assertEqual(segment["shadowed_by"], result["memory_ids"][0])
        self.assertNotIn("shadow_native_ids", self.service.read(result["memory_ids"][0]).to_markdown())
        current = self.service.context("legacy status")
        self.assertEqual([memory.memory_id for memory in current], [result["memory_ids"][0]])
        self.assertTrue(all(set(memory.to_dict()) == {"memory_id", "title", "scopes"} for memory in current))

    def test_read_page_rejects_private_or_changed_native_ids_without_writing_source(self):
        shared = self.root / "shared.md"
        shared.write_text("# Shared\nshared page\n", encoding="utf-8")
        self.set_sources({"shared": {"agent": "hermes", "path": str(shared), "share": True}})
        native_id = self.read_index()["sources"]["shared"]["segments"][0]["native_id"]
        before = shared.read_bytes()

        self.set_sources({"shared": {"agent": "hermes", "path": str(shared), "share": False}})
        self.assertIsNone(self.service.read_page(native_id))
        self.assertEqual(before, shared.read_bytes())

        self.set_sources({"shared": {"agent": "hermes", "path": str(shared), "share": True}})
        shared.write_text("# Shared\nchanged page\n", encoding="utf-8")
        changed_bytes = shared.read_bytes()
        self.assertIsNone(self.service.read_page(native_id))
        self.assertEqual(changed_bytes, shared.read_bytes())

    def test_shadow_failure_does_not_advance_watermark_and_retry_is_idempotent(self):
        shared = self.root / "shared.md"
        native_body = "# Legacy\nlegacy retry status"
        shared.write_text(native_body + "\n", encoding="utf-8")
        self.set_sources({"shared": {"agent": "hermes", "path": str(shared), "share": True}})
        native_id = self.read_index()["sources"]["shared"]["segments"][0]["native_id"]

        event_key_value = self.capture_turn(user="legacy retry status is replaced", assistant="confirmed")
        responses = [
            self.gate(self.candidate(event_key_value, memory="legacy retry status is replaced")),
            self.summary(event_key_value, "retry status", shadow=[native_id]),
            self.gate(self.candidate(event_key_value, memory="legacy retry status is replaced")),
            self.summary(event_key_value, "retry status", shadow=[native_id]),
        ]
        backend = QueueBackend(responses)
        with patch.object(NativeIndexer, "apply_shadow_unlocked", side_effect=NativeIndexError("injected")):
            with self.assertRaises(NativeIndexError):
                self.service.process(model=backend)
        processed = json.loads(self.service.vault.processed_index_path.read_text(encoding="utf-8"))
        state = processed["sessions"]["codex/s"]
        self.assertEqual(state["processing"]["status"], "failed")
        self.assertNotIn("processed_turns", state)
        self.assertEqual(len(self.service._read_memories_unlocked("knowledge")), 1)

        result = self.service.process(model=backend)

        self.assertEqual(result["memories_written"], 0)
        self.assertEqual(len(self.service._read_memories_unlocked("knowledge")), 1)
        self.assertEqual(len(self.service._read_memories_unlocked("history")), 0)
        processed = json.loads(self.service.vault.processed_index_path.read_text(encoding="utf-8"))
        self.assertEqual(processed["sessions"]["codex/s"]["watermark"], 1)
        active_memory_id = self.service._read_memories_unlocked("knowledge")[0].memory.memory_id
        self.assertEqual(
            self.read_index()["sources"]["shared"]["segments"][0]["shadowed_by"],
            active_memory_id,
        )

    def test_changed_source_only_preserves_shadow_for_same_native_id(self):
        source = self.root / "native.md"
        source.write_text("# Stable\nstable text\n# Changed\nold text\n", encoding="utf-8")
        self.set_sources({"source": {"agent": "hermes", "path": str(source), "share": True}})
        before = self.read_index()["sources"]["source"]["segments"]
        stable_id = before[0]["native_id"]
        changed_id = before[1]["native_id"]
        NativeIndexer(self.service.vault).apply_shadow([{"source_id": "source", "native_id": stable_id}], "mem-new")

        source.write_text("# Stable\nstable text\n# Changed\nnew text\n", encoding="utf-8")
        self.service.refresh_native_sources()
        after = self.read_index()["sources"]["source"]["segments"]
        after_by_locator = {item["locator"]: item for item in after}

        self.assertEqual(after_by_locator["heading:1"]["native_id"], stable_id)
        self.assertEqual(after_by_locator["heading:1"]["shadowed_by"], "mem-new")
        self.assertNotEqual(after_by_locator["heading:2"]["native_id"], changed_id)
        self.assertNotIn("shadowed_by", after_by_locator["heading:2"])
        index_text = self.service.vault.native_sources_index_path.read_text(encoding="utf-8")
        self.assertNotIn("stable text", index_text)
        self.assertNotIn("new text", index_text)


if __name__ == "__main__":
    unittest.main()

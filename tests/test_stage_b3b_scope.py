import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from memleaf import Memleaf
from memleaf.config import save_config
from memleaf.index import event_key
from memleaf.processing import ProcessingError


class QueueBackend:
    provider = "fake"
    model = "b3b-scope"

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    def complete(self, prompt, *, system="", purpose="", temperature=0.0):
        self.calls.append({"prompt": prompt, "purpose": purpose})
        if not self.responses:
            raise AssertionError("fake model queue exhausted")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class Clock:
    def __init__(self):
        self.value = datetime(2026, 8, 24, tzinfo=timezone.utc)

    def __call__(self):
        return self.value


class StageB3BScopeTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "vault"
        self.clock = Clock()
        self.service = Memleaf(self.path, clock=self.clock)

    def tearDown(self):
        self.tempdir.cleanup()

    @staticmethod
    def gate(candidate_values):
        return json.dumps({"candidates": candidate_values})

    @staticmethod
    def candidate(candidate_id, scopes, *, worth=False, evidence=None):
        return {
            "candidate_id": candidate_id,
            "memory": "a scope observation",
            "evidence_event_ids": list(evidence or ["missing"]),
            "duplicate": False,
            "worth": worth,
            "type": "fact",
            "scopes": list(scopes),
            "scope_source": "model",
        }

    @staticmethod
    def summary(event_key_value, scopes, *, title="Scoped fact"):
        return json.dumps(
            {
                "title": title,
                "body": "A scoped fact.",
                "tags": ["scope-test"],
                "type": "fact",
                "scopes": list(scopes),
                "scope_source": "model",
                "sources": [{"event_key": event_key_value}],
            }
        )

    def capture_turn(self, *, session="s", turn="t1", source="codex", prefix="e"):
        user_event = f"{prefix}-user"
        assistant_event = f"{prefix}-assistant"
        self.service.capture(source, session, turn, "user", "visible user", event_id=user_event)
        self.service.capture(source, session, turn, "assistant", "visible assistant", event_id=assistant_event)
        return event_key(user_event), event_key(assistant_event)

    def processed(self):
        return json.loads(self.service.vault.processed_index_path.read_text(encoding="utf-8"))

    def set_registry(self, scopes):
        config = self.service.vault.config()
        config["scopes"] = scopes
        save_config(self.service.vault.config_path, config)

    def add_scoped_memories(self):
        self.service.create_memory(
            memory_id="global-topic",
            title="Global topic",
            body="global topic",
            tags=["topic"],
            scopes=["global"],
        )
        self.service.create_memory(
            memory_id="alpha-topic",
            title="Alpha topic",
            body="alpha topic",
            tags=["topic"],
            scopes=["project:alpha"],
        )
        self.service.create_memory(
            memory_id="beta-topic",
            title="Beta topic",
            body="beta topic",
            tags=["topic"],
            scopes=["project:beta"],
        )

    def test_success_observes_scopes_and_registers_nodes(self):
        user_key, _ = self.capture_turn()
        backend = QueueBackend(
            [self.gate([self.candidate("c1", ["project:alpha"], evidence=[user_key])])]
        )

        result = self.service.process(model=backend)

        self.assertEqual(result["processed_turns"], 1)
        state = self.processed()["sessions"]["codex/s"]
        self.assertEqual(state["scopes"], ["project:alpha"])
        self.assertEqual(self.service.vault.config()["scopes"]["project:alpha"], {})

    def test_gate_empty_preserves_scope_and_failed_registration_does_not_update(self):
        user_key, _ = self.capture_turn(prefix="first")
        backend = QueueBackend(
            [self.gate([self.candidate("c1", ["project:old"], evidence=[user_key])])]
        )
        self.service.process(model=backend)
        self.assertEqual(self.processed()["sessions"]["codex/s"]["scopes"], ["project:old"])

        next_user, _ = self.capture_turn(turn="t2", prefix="second")
        backend.responses.append(self.gate([]))
        self.service.process(model=backend)
        self.assertEqual(self.processed()["sessions"]["codex/s"]["scopes"], ["project:old"])
        self.assertNotIn("project:new", self.service.vault.config()["scopes"])

        third_user, _ = self.capture_turn(turn="t3", prefix="third")
        backend.responses.append(self.gate([self.candidate("c3", ["project:new"], evidence=[third_user])]))
        with patch("memleaf.processing.save_config", side_effect=OSError("config write")):
            with self.assertRaises(OSError):
                self.service.process(model=backend)
        state = self.processed()["sessions"]["codex/s"]
        self.assertEqual(state["scopes"], ["project:old"])
        self.assertEqual(state.get("watermark"), 2)
        self.assertEqual(state["processing"]["status"], "failed")
        self.assertNotIn("project:new", self.service.vault.config()["scopes"])

    def test_multiple_turns_use_last_turn_with_observed_scopes(self):
        first_user, _ = self.capture_turn(turn="t1", prefix="one")
        second_user, _ = self.capture_turn(turn="t2", prefix="two")
        backend = QueueBackend(
            [
                self.gate([self.candidate("one", ["project:one"], evidence=[first_user])]),
                self.gate([self.candidate("two", ["project:two"], evidence=[second_user])]),
            ]
        )

        result = self.service.process(model=backend)

        self.assertEqual(result["processed_turns"], 2)
        self.assertEqual(self.processed()["sessions"]["codex/s"]["scopes"], ["project:two"])
        self.assertEqual(set(self.service.vault.config()["scopes"]), {"project:one", "project:two"})

    def test_summary_scopes_are_observed_and_remember_prefers_user_scope(self):
        user_key, _ = self.capture_turn(prefix="summary")
        backend = QueueBackend(
            [
                self.gate([self.candidate("c1", ["global"], worth=True, evidence=[user_key])]),
                self.summary(user_key, ["project:from-summary"]),
            ]
        )
        self.service.process(model=backend)
        self.assertEqual(
            self.processed()["sessions"]["codex/s"]["scopes"],
            ["global", "project:from-summary"],
        )

        remember_key = event_key("remember-scope")
        remember_backend = QueueBackend([self.summary(remember_key, ["project:model-scope"])])
        result = self.service.remember(
            "remembered scope",
            source="codex",
            session_id="remember-session",
            turn_id="remember-turn",
            event_id="remember-scope",
            scopes=["project:user-scope"],
            model=remember_backend,
        )
        self.assertEqual(result["processed_turns"], 1)
        self.assertEqual(
            self.processed()["sessions"]["codex/remember-session"]["scopes"],
            ["project:user-scope"],
        )
        self.assertIn("project:user-scope", self.service.vault.config()["scopes"])

    def test_context_default_global_and_temporary_query_override(self):
        self.set_registry(
            {
                "project:alpha": {"aliases": ["alpha"]},
                "project:beta": {"aliases": ["beta"]},
            }
        )
        self.add_scoped_memories()
        processed = self.processed()
        processed["sessions"]["codex/s"] = {"scopes": ["project:beta"], "custom": "keep"}
        self.service.vault.processed_index_path.write_text(json.dumps(processed), encoding="utf-8")

        default_ids = {memory.memory_id for memory in self.service.context("topic")}
        query_ids = {memory.memory_id for memory in self.service.context("alpha topic", source="codex", session_id="s")}
        explicit_ids = {
            memory.memory_id
            for memory in self.service.context(
                "alpha topic",
                scope="project:beta",
                source="codex",
                session_id="s",
            )
        }

        self.assertEqual(default_ids, {"global-topic"})
        self.assertEqual(query_ids, {"global-topic", "alpha-topic"})
        self.assertEqual(explicit_ids, {"global-topic", "beta-topic"})
        self.assertEqual(self.processed()["sessions"]["codex/s"]["scopes"], ["project:beta"])

    def test_ascii_project_matching_uses_boundaries(self):
        self.set_registry({"project:alpha": {"aliases": ["alpha"]}})
        self.add_scoped_memories()

        alphabet_ids = {memory.memory_id for memory in self.service.context("alphabet topic")}
        alpha_ids = {memory.memory_id for memory in self.service.context("alpha topic")}

        self.assertEqual(alphabet_ids, {"global-topic"})
        self.assertEqual(alpha_ids, {"global-topic", "alpha-topic"})

    def test_project_path_uses_longest_configured_project_and_not_shared_parent(self):
        root = Path(self.tempdir.name) / "workspace"
        alpha = root / "alpha"
        self.set_registry(
            {
                "portfolio:shared": {"paths": [str(root)]},
                "project:alpha": {"paths": [str(alpha)]},
                "project:beta": {"paths": [str(root / "beta")]},
            }
        )
        self.add_scoped_memories()

        alpha_ids = {
            memory.memory_id
            for memory in self.service.context("topic", project_path=alpha / "nested" / "file.txt")
        }
        shared_ids = {
            memory.memory_id
            for memory in self.service.context("topic", project_path=root / "other" / "file.txt")
        }

        self.assertEqual(alpha_ids, {"global-topic", "alpha-topic"})
        self.assertEqual(shared_ids, {"global-topic"})

        initialized = self.service.context(
            "topic",
            source="codex",
            session_id="path-session",
            project_path=alpha / "nested",
        )
        self.assertEqual({memory.memory_id for memory in initialized}, {"global-topic", "alpha-topic"})
        self.assertEqual(
            self.processed()["sessions"]["codex/path-session"]["scopes"],
            ["project:alpha"],
        )

        no_state = self.processed()
        with self.assertRaises(ValueError):
            self.service.context("topic", project_path=alpha, source="codex")
        self.assertNotIn("codex/None", self.processed().get("sessions", {}))
        self.assertEqual(self.processed()["sessions"], no_state["sessions"])

    def test_context_initialization_preserves_processing_owned_fields_under_threads(self):
        root = Path(self.tempdir.name) / "workspace"
        self.set_registry({"project:alpha": {"paths": [str(root / "alpha")]}})
        self.add_scoped_memories()
        processed = self.processed()
        processed["sessions"]["codex/concurrent"] = {
            "processing": {"status": "processing", "token": "owned"},
            "custom": "preserve",
        }
        self.service.vault.processed_index_path.write_text(json.dumps(processed), encoding="utf-8")
        errors = []

        def invoke():
            try:
                self.service.context(
                    "topic",
                    source="codex",
                    session_id="concurrent",
                    project_path=root / "alpha" / "file.txt",
                )
            except BaseException as error:
                errors.append(error)

        threads = [threading.Thread(target=invoke, daemon=True) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2.0)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        state = self.processed()["sessions"]["codex/concurrent"]
        self.assertEqual(state["scopes"], ["project:alpha"])
        self.assertEqual(state["processing"], {"status": "processing", "token": "owned"})
        self.assertEqual(state["custom"], "preserve")

    def test_registry_rejects_invalid_types_and_self_relationships(self):
        config = self.service.vault.config()
        for registry in (
            {"not-a-scope": {}},
            {"project:alpha": {"aliases": "alpha"}},
            {"project:alpha": {"paths": [""]}},
            {"project:alpha": {"parent": "project:alpha"}},
            {"project:alpha": {"children": ["project:alpha"]}},
        ):
            with self.subTest(registry=registry):
                candidate = dict(config)
                candidate["scopes"] = registry
                with self.assertRaises(ValueError):
                    save_config(self.service.vault.config_path, candidate)


if __name__ == "__main__":
    unittest.main()

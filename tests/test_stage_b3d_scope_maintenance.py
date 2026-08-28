import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from memleaf import Memleaf
from memleaf.config import save_config
from memleaf.index import event_key
from memleaf.memory_writer import MemoryWriter
from memleaf.scope_maintenance import ScopeMaintainer
from memleaf.scope_state import ScopeError, validate_scope_registry
from memleaf.validation import ModelOutputError, validate_gate_output


class QueueBackend:
    provider = "fake"
    model = "b3d-scope"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, prompt, *, system="", purpose="", temperature=0.0):
        self.calls.append({"purpose": purpose, "prompt": prompt})
        return self.responses.pop(0)


class Clock:
    def __call__(self):
        return datetime(2026, 8, 24, tzinfo=timezone.utc)


class StageB3DScopeMaintenanceTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.vault = Path(self.tempdir.name) / "vault"
        self.service = Memleaf(self.vault, clock=Clock())
        config = self.service.vault.config()
        config["scopes"] = {"project:old": {}}
        save_config(self.service.vault.config_path, config)

    def tearDown(self):
        self.tempdir.cleanup()

    @staticmethod
    def gate(event_key_value, *, scopes=None, duplicate_memory_id=None, candidate_id="scope-fact"):
        candidate = {
            "candidate_id": candidate_id,
            "memory": "fact moved to the new project",
            "evidence_event_ids": [event_key_value],
            "duplicate": duplicate_memory_id is not None,
            "worth": duplicate_memory_id is None,
            "type": "fact",
            "scopes": list(scopes or ["project:old"]),
            "scope_source": "model",
        }
        if duplicate_memory_id is not None:
            candidate["duplicate_memory_id"] = duplicate_memory_id
        return json.dumps(
            {
                "candidates": [candidate]
            }
        )

    @staticmethod
    def summary(event_key_value, *, scopes=None, scope_operations=None):
        return json.dumps(
            {
                "title": "Moved fact",
                "body": "The same fact now belongs to the new project.",
                "tags": ["scope"],
                "type": "fact",
                # Keep the source scope in the summary.  The maintainer must
                # migrate it through source->target even on an idempotent
                # retry after source has disappeared from the registry.
                "scopes": list(scopes or ["project:old"]),
                "scope_source": "model",
                "sources": [{"event_key": event_key_value}],
                "scope_operations": list(scope_operations if scope_operations is not None else [
                    {
                        "op": "upsert",
                        "scope": "project:new",
                        "parent": None,
                        "aliases": ["new-project"],
                    },
                    {
                        "op": "merge",
                        "source": "project:old",
                        "target": "project:new",
                        "aliases": ["renamed-project"],
                    },
                ]),
            }
        )

    def capture_turn(self, *, turn_id="turn-1", prefix="scope", content="move this fact"):
        self.service.capture(
            "codex", "s", turn_id, "user", content, event_id=f"{prefix}-user"
        )
        self.service.capture(
            "codex", "s", turn_id, "assistant", "confirmed", event_id=f"{prefix}-assistant"
        )
        return event_key(f"{prefix}-user")

    def make_merge_case(self, name):
        path = Path(self.tempdir.name) / name
        service = Memleaf(path, clock=Clock())
        config = service.vault.config()
        config["scopes"] = {"project:old": {}}
        save_config(service.vault.config_path, config)
        service.capture("codex", "s", "turn-1", "user", "move this fact", event_id=f"{name}-user")
        service.capture("codex", "s", "turn-1", "assistant", "confirmed", event_id=f"{name}-assistant")
        key = event_key(f"{name}-user")
        backend = QueueBackend(
            [self.gate(key), self.summary(key), self.gate(key), self.summary(key)]
        )
        return service, backend

    def assert_fault_is_retryable(self, name, fault):
        service, backend = self.make_merge_case(name)
        with fault(service):
            with self.assertRaises(Exception):
                service.process(model=backend)
        processed = json.loads(service.vault.processed_index_path.read_text(encoding="utf-8"))
        state = processed["sessions"]["codex/s"]
        self.assertNotIn("watermark", state)
        self.assertEqual(state["processing"]["status"], "failed")
        self.assertIn("project:old", service.vault.config()["scopes"])
        service.process(model=backend)
        final = json.loads(service.vault.processed_index_path.read_text(encoding="utf-8"))
        self.assertEqual(final["sessions"]["codex/s"]["watermark"], 1)
        self.assertNotIn("project:old", service.vault.config()["scopes"])
        self.assertEqual(len(service.vault.list_markdown("knowledge")), 1)
        self.assertEqual(len(service.vault.list_markdown("history")), 0)

    def test_prepare_infers_completed_merge_from_full_source_alias(self):
        config = self.service.vault.config()
        config["scopes"] = {"project:new": {"aliases": ["project:old"]}}

        prepared = ScopeMaintainer(self.service).prepare(
            config,
            operations=[
                {
                    "op": "merge",
                    "source": "project:old",
                    "target": "project:new",
                    "aliases": [],
                }
            ],
            observed_scopes=["project:old"],
            session_scopes={"codex/s": ["project:old"]},
        )

        self.assertEqual(prepared.migrations, {"project:old": "project:new"})
        self.assertEqual(prepared.session_scopes["codex/s"], ["project:new"])
        self.assertEqual(prepared.config["scopes"], config["scopes"])

    def test_registry_canonicalizes_parent_children_and_rejects_conflicts(self):
        canonical = validate_scope_registry(
            {
                "domain:eng": {"children": ["portfolio:platform"]},
                "portfolio:platform": {
                    "parent": "domain:eng",
                    "children": ["project:orion"],
                },
                "project:orion": {"parent": "portfolio:platform"},
            }
        )
        self.assertEqual(canonical["portfolio:platform"]["parent"], "domain:eng")
        self.assertEqual(canonical["domain:eng"]["children"], ["portfolio:platform"])
        self.assertEqual(canonical["portfolio:platform"]["children"], ["project:orion"])

        invalid = [
            {
                "domain:eng": {"children": ["portfolio:platform"]},
                "portfolio:platform": {"parent": "domain:other"},
            },
            {
                "domain:eng": {"parent": "portfolio:platform"},
                "portfolio:platform": {"parent": "domain:eng"},
            },
            {"project:orion": {"parent": "project:other"}},
        ]
        for registry in invalid:
            with self.assertRaises(ScopeError):
                validate_scope_registry(registry)

    def test_scope_registry_prompt_projection_excludes_paths(self):
        config = self.service.vault.config()
        config["scopes"] = {
            "domain:eng": {"paths": ["/private/secret/project"]},
            "project:old": {"parent": "domain:eng", "aliases": ["legacy"]},
        }
        save_config(self.service.vault.config_path, config)
        key = self.capture_turn(prefix="projection")
        backend = QueueBackend([json.dumps({"candidates": []})])

        self.service.process(model=backend)

        prompt = backend.calls[0]["prompt"]
        self.assertIn("project:old", prompt)
        self.assertIn("legacy", prompt)
        self.assertNotIn("/private/secret/project", prompt)

    def test_unauthorized_scope_operation_is_zero_write_and_no_watermark(self):
        key = self.capture_turn(prefix="unauthorized")
        bad_ops = [
            {
                "op": "merge",
                "source": "project:not-registered",
                "target": "project:new",
                "aliases": [],
            }
        ]
        backend = QueueBackend(
            [
                self.gate(key),
                self.summary(key, scope_operations=bad_ops),
                self.summary(key, scope_operations=bad_ops),
                self.summary(key, scope_operations=bad_ops),
            ]
        )

        with self.assertRaises(ModelOutputError):
            self.service.process(model=backend)

        self.assertEqual(self.service.vault.list_markdown("knowledge"), [])
        processed = json.loads(self.service.vault.processed_index_path.read_text(encoding="utf-8"))
        state = processed["sessions"]["codex/s"]
        self.assertNotIn("watermark", state)
        self.assertEqual(state["processing"]["status"], "failed")
        self.assertEqual(self.service.vault.config()["scopes"], {"project:old": {}})

    def test_scope_operation_cycle_is_rejected_before_any_write(self):
        key = self.capture_turn(prefix="cycle")
        cycle_ops = [
            {
                "op": "merge",
                "source": "project:old",
                "target": "project:new",
                "aliases": [],
            },
            {
                "op": "merge",
                "source": "project:new",
                "target": "project:old",
                "aliases": [],
            },
        ]
        backend = QueueBackend(
            [
                self.gate(key),
                self.summary(key, scope_operations=cycle_ops),
                self.summary(key, scope_operations=cycle_ops),
                self.summary(key, scope_operations=cycle_ops),
            ]
        )

        with self.assertRaises(ModelOutputError):
            self.service.process(model=backend)

        self.assertEqual(self.service.vault.list_markdown("knowledge"), [])
        self.assertNotIn("project:new", self.service.vault.config()["scopes"])

    def test_merge_migrates_active_history_session_and_parent_children_without_history(self):
        config = self.service.vault.config()
        config["scopes"] = {
            "domain:eng": {},
            "portfolio:platform": {"parent": "domain:eng"},
            "project:old": {"parent": "portfolio:platform"},
        }
        save_config(self.service.vault.config_path, config)
        self.service.create_memory(
            memory_id="old-active",
            title="Old active",
            body="unrelated old active",
            type="fact",
            scopes=["project:old"],
        )
        self.service.create_memory(
            memory_id="old-history",
            title="Old history",
            body="unrelated old history",
            type="fact",
            scopes=["project:old"],
            area="history",
            active_memory_id="old-active",
        )
        key = self.capture_turn(prefix="migration")
        operations = [
            {
                "op": "upsert",
                "scope": "project:new",
                "parent": "portfolio:platform",
                "aliases": ["new-project"],
            },
            {
                "op": "merge",
                "source": "project:old",
                "target": "project:new",
                "aliases": ["old-project"],
            },
        ]
        backend = QueueBackend([self.gate(key), self.summary(key, scope_operations=operations)])

        result = self.service.process(model=backend)

        self.assertEqual(result["processed_turns"], 1)
        registry = self.service.vault.config()["scopes"]
        self.assertNotIn("project:old", registry)
        self.assertEqual(registry["project:new"]["parent"], "portfolio:platform")
        self.assertIn("project:new", registry["portfolio:platform"]["children"])
        self.assertEqual(self.service.read("old-active").scopes, ["project:new"])
        self.assertEqual(self.service.read("old-history", include_history=True).scopes, ["project:new"])
        state = json.loads(self.service.vault.processed_index_path.read_text(encoding="utf-8"))["sessions"]["codex/s"]
        self.assertEqual(state["scopes"], ["project:new"])
        self.assertEqual(len(self.service.vault.list_markdown("history")), 1)
        self.assertEqual(self.service.read(result["memory_ids"][0]).scopes, ["project:new"])

    def test_automatic_duplicate_memory_id_cross_scope_is_rejected_without_writes(self):
        existing = self.service.create_memory(
            memory_id="existing-fact",
            title="Existing fact",
            body="shared fact marker",
            tags=["keep"],
            type="fact",
            scopes=["project:old"],
        )
        key = self.capture_turn(prefix="duplicate", content="shared fact marker")
        backend = QueueBackend(
            [
                self.gate(
                    key,
                    scopes=["project:new"],
                    duplicate_memory_id=existing.memory_id,
                )
            ]
        )

        with self.assertRaises(ModelOutputError):
            self.service.process(model=backend)

        unchanged = self.service.read(existing.memory_id)
        self.assertEqual(unchanged.body, existing.body)
        self.assertEqual(unchanged.title, existing.title)
        self.assertEqual(unchanged.tags, existing.tags)
        self.assertEqual(unchanged.scopes, ["project:old"])
        self.assertEqual(len(self.service.vault.list_markdown("knowledge")), 1)
        self.assertEqual(len(self.service.vault.list_markdown("history")), 0)
        self.assertNotIn(key, {item.get("event_key") for item in unchanged.sources})

    def test_automatic_duplicate_same_scope_is_no_change_without_sources_or_history(self):
        existing = self.service.create_memory(
            memory_id="existing-fact",
            title="Existing fact",
            body="shared fact marker",
            tags=["keep"],
            type="fact",
            scopes=["project:old"],
        )
        key = self.capture_turn(prefix="duplicate-same-scope", content="shared fact marker")
        backend = QueueBackend(
            [
                self.gate(
                    key,
                    scopes=["project:old"],
                    duplicate_memory_id=existing.memory_id,
                )
            ]
        )

        result = self.service.process(model=backend)

        self.assertEqual(result["memories_written"], 0)
        self.assertEqual(result["metadata_merged"], 0)
        self.assertEqual(result["memory_ids"], [])
        unchanged = self.service.read(existing.memory_id)
        self.assertEqual(unchanged.body, existing.body)
        self.assertEqual(unchanged.title, existing.title)
        self.assertEqual(unchanged.tags, existing.tags)
        self.assertEqual(unchanged.scopes, ["project:old"])
        self.assertEqual(len(self.service.vault.list_markdown("knowledge")), 1)
        self.assertEqual(len(self.service.vault.list_markdown("history")), 0)
        self.assertNotIn(key, {item.get("event_key") for item in unchanged.sources})

    def test_duplicate_memory_id_is_restricted_to_related_active_memleaf_and_batch_unique(self):
        existing = self.service.create_memory(
            memory_id="existing-fact",
            title="Existing fact",
            body="only related marker",
            type="fact",
            scopes=["global"],
        )
        key = self.capture_turn(prefix="not-related", content="unrelated text")
        invalid_gate = self.gate(key, duplicate_memory_id=existing.memory_id)
        backend = QueueBackend([invalid_gate, invalid_gate, invalid_gate])
        with self.assertRaises(ModelOutputError):
            self.service.process(model=backend)
        self.assertEqual(len(self.service.vault.list_markdown("knowledge")), 1)

        key2 = self.capture_turn(turn_id="turn-2", prefix="duplicate-batch", content="only related marker")
        duplicate_candidates = []
        for candidate_id in ("d1", "d2"):
            duplicate_candidates.append(
                {
                    "candidate_id": candidate_id,
                    "memory": "only related marker",
                    "evidence_event_ids": [key2],
                    "duplicate": True,
                    "worth": False,
                    "type": "fact",
                    "scopes": ["global"],
                    "scope_source": "model",
                    "duplicate_memory_id": existing.memory_id,
                }
            )
        invalid_batch = json.dumps({"candidates": duplicate_candidates})
        backend = QueueBackend([invalid_batch, invalid_batch, invalid_batch])
        with self.assertRaises(ModelOutputError):
            self.service.process(model=backend)
        state = json.loads(self.service.vault.processed_index_path.read_text(encoding="utf-8"))["sessions"]["codex/s"]
        self.assertNotIn("watermark", state)
        self.assertEqual(len(self.service.vault.list_markdown("history")), 0)

    def test_duplicate_memory_id_field_is_forbidden_for_non_duplicate_candidate(self):
        candidate = {
            "candidate_id": "bad",
            "memory": "fact",
            "evidence_event_ids": ["a"],
            "duplicate": False,
            "worth": True,
            "type": "fact",
            "scopes": ["global"],
            "scope_source": "model",
            "duplicate_memory_id": "existing",
        }
        with self.assertRaises(ModelOutputError):
            validate_gate_output({"candidates": [candidate]}, ["a"], related_memory_ids=["existing"])

    def test_preflight_rejects_normal_then_duplicate_id_collision(self):
        self.service.create_memory(
            memory_id="collision-target",
            title="Existing",
            body="existing",
            type="fact",
            scopes=["global"],
        )
        writer = MemoryWriter(self.service)
        normal = {"summary": {}, "memory_id": "collision-target"}
        duplicate = {
            "summary": {},
            "memory_id": "collision-target",
            "duplicate_memory_id": "collision-target",
        }

        with self.assertRaises(ModelOutputError):
            writer._preflight([normal, duplicate])

    def test_preflight_rejects_duplicate_then_normal_id_collision(self):
        self.service.create_memory(
            memory_id="collision-target",
            title="Existing",
            body="existing",
            type="fact",
            scopes=["global"],
        )
        writer = MemoryWriter(self.service)
        normal = {"summary": {}, "memory_id": "collision-target"}
        duplicate = {
            "summary": {},
            "memory_id": "collision-target",
            "duplicate_memory_id": "collision-target",
        }

        with self.assertRaises(ModelOutputError):
            writer._preflight([duplicate, normal])

    def test_scope_memory_write_failure_is_forward_retryable(self):
        self.assert_fault_is_retryable(
            "fault-memory",
            lambda service: patch(
                "memleaf.memory_writer.atomic_write_text",
                side_effect=OSError("injected memory write"),
            ),
        )

    def test_scope_processed_write_failure_is_forward_retryable(self):
        self.assert_fault_is_retryable(
            "fault-processed",
            lambda service: patch(
                "memleaf.scope_maintenance.atomic_write_json",
                side_effect=OSError("injected processed write"),
            ),
        )

    def test_scope_index_failure_is_forward_retryable(self):
        self.assert_fault_is_retryable(
            "fault-index",
            lambda service: patch.object(
                service,
                "_rebuild_index_unlocked",
                side_effect=OSError("injected index write"),
            ),
        )

    def test_scope_config_failure_is_forward_retryable_and_config_last(self):
        self.assert_fault_is_retryable(
            "fault-config",
            lambda service: patch(
                "memleaf.scope_maintenance.save_config",
                side_effect=OSError("injected config write"),
            ),
        )

    def test_config_success_then_processed_failure_is_idempotent_on_retry(self):
        self.service.capture(
            "codex", "s", "turn-1", "user", "move this fact", event_id="scope-user"
        )
        self.service.capture(
            "codex", "s", "turn-1", "assistant", "confirmed", event_id="scope-assistant"
        )
        key = event_key("scope-user")
        backend = QueueBackend(
            [
                self.gate(key),
                self.summary(key),
                self.gate(key),
                self.summary(key),
            ]
        )

        original_atomic = __import__("memleaf.processing", fromlist=["atomic_write_json"]).atomic_write_json
        calls = {"processed": 0}

        def fail_final_processed(path, value):
            if path == self.service.vault.processed_index_path:
                calls["processed"] += 1
                # The first call is the processing claim.  The second is the
                # post-commit watermark write, after config has been saved.
                if calls["processed"] == 2:
                    raise OSError("injected final processed write failure")
            return original_atomic(path, value)

        with patch("memleaf.processing.atomic_write_json", side_effect=fail_final_processed):
            with self.assertRaises(OSError):
                self.service.process(model=backend)

        config_after_failure = self.service.vault.config()
        self.assertNotIn("project:old", config_after_failure["scopes"])
        aliases = config_after_failure["scopes"]["project:new"]["aliases"]
        self.assertIn("project:old", aliases)
        processed = json.loads(self.service.vault.processed_index_path.read_text(encoding="utf-8"))
        state = processed["sessions"]["codex/s"]
        self.assertNotIn("watermark", state)
        self.assertEqual(state["processing"]["status"], "failed")
        self.assertEqual(len(self.service.vault.list_markdown("history")), 0)

        result = self.service.process(model=backend)

        self.assertEqual(result["processed_turns"], 1)
        self.assertEqual(result["memories_written"], 0)
        self.assertEqual(len(self.service.vault.list_markdown("knowledge")), 1)
        self.assertEqual(len(self.service.vault.list_markdown("history")), 0)
        self.assertEqual(len(self.service.vault.list_markdown("inbox")), 1)
        processed = json.loads(self.service.vault.processed_index_path.read_text(encoding="utf-8"))
        state = processed["sessions"]["codex/s"]
        self.assertEqual(state["watermark"], 1)
        self.assertEqual(state["processing"]["status"], "idle")
        self.assertEqual(len(state["processed_turns"]), 1)
        self.assertEqual(
            [call["purpose"] for call in backend.calls],
            ["gate", "summarize", "gate", "summarize"],
        )


if __name__ == "__main__":
    unittest.main()

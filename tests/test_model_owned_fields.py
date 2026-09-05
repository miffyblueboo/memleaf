"""Phase-four contracts: models own fields, Core owns mutation integrity.

No live model is used. Malformed semantic proposals are never repaired into
new facts by test helpers, and source/type/scope assertions stay independent.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from memleaf import Memleaf
from memleaf.compaction import Compactor, CompactionError
from memleaf.config import save_config
from memleaf.index import event_key
from memleaf.validation import ModelOutputError, parse_gate_output, parse_summarize_output
from tests import test_extraction_quality_regressions as fixture


class ModelOwnedFields(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.core = Memleaf(Path(temp.name) / "vault 中文")
        cfg = self.core.vault.config()
        cfg["scopes"] = {"project:alpha": {}, "project:beta": {}}
        save_config(self.core.vault.config_path, cfg)

    def candidate(self, **overrides):
        return {**fixture.candidate("c", ["u"], memory="alpha 部署计划需要修改。",
                                   type="fact", scopes=["project:alpha"]), **overrides}

    def capture(self, text):
        self.core.capture("hermes", "s", "t", "user", text, event_id="u")
        self.core.capture("hermes", "s", "t", "assistant", "Noted.", event_id="a")
        return event_key("u")

    def test_gate_keywords_cannot_relabel_valid_types(self):
        for kind in ("fact", "event", "other", "project", "todo"):
            c = self.candidate(type=kind)
            before = deepcopy(c)
            with self.subTest(kind=kind):
                parsed = parse_gate_output(fixture.gate([c]), event_keys=["u"])
                self.assertEqual(parsed["candidates"][0], before)
                self.assertEqual(c, before)

    def test_summary_keywords_cannot_relabel_valid_types(self):
        for kind in ("fact", "event", "other", "project"):
            raw = fixture.summary("u", title="alpha 部署计划", body="alpha 部署计划要求分批部署。",
                                  type=kind, scopes=["project:alpha"])
            with self.subTest(kind=kind):
                result = parse_summarize_output(raw, current_event_keys=["u"], expected_type=kind)
                self.assertEqual(result["type"], kind)

    def test_summary_cannot_change_gate_or_target_type(self):
        raw = fixture.summary("u", title="alpha 部署计划", body="alpha 部署计划要求分批部署。",
                              type="fact", scopes=["project:alpha"], update_memory_id="mem-alpha")
        with self.assertRaises(ModelOutputError):
            parse_summarize_output(raw, current_event_keys=["u"], related_memory_ids=["mem-alpha"],
                                   expected_type="project", expected_target_type="project")

    def test_final_semantic_deferral_preserves_model_fields(self):
        c = self.candidate(memory="beta 的数据库改为 PostgreSQL。")
        original = deepcopy(c)
        raw = fixture.gate([c])
        with self.assertRaises(ModelOutputError):
            parse_gate_output(raw, event_keys=["u"], scope_registry=self.core.vault.config()["scopes"])
        parsed = parse_gate_output(raw, event_keys=["u"], scope_registry=self.core.vault.config()["scopes"],
                                   defer_semantic_errors=True)["candidates"]
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].pop("_defer_reason"), "scope_conflict")
        self.assertEqual(parsed[0], original)

    def test_final_deferral_still_rejects_invalid_evidence(self):
        c = self.candidate(memory="beta uses PostgreSQL.", evidence_event_ids=["foreign"])
        with self.assertRaises(ModelOutputError):
            parse_gate_output(fixture.gate([c]), event_keys=["u"], defer_semantic_errors=True)

    def test_final_deferral_still_rejects_unregistered_target(self):
        c = self.candidate(memory="beta uses PostgreSQL.", update_memory_id="not-authorized")
        with self.assertRaises(ModelOutputError):
            parse_gate_output(fixture.gate([c]), event_keys=["u"], defer_semantic_errors=True)

    def test_bad_scope_does_not_block_valid_sibling_or_get_reassigned(self):
        key = self.capture("beta uses PostgreSQL. alpha uses JDK 17.")
        bad = self.candidate(memory="beta uses PostgreSQL.", evidence_event_ids=[key])
        good = self.candidate(candidate_id="good", memory="alpha uses JDK 17.", evidence_event_ids=[key])
        backend = fixture.QueueBackend([fixture.gate([bad, good])] * 3 + [
            fixture.summary(key, title="alpha JDK", body="alpha uses JDK 17.", type="fact", scopes=["project:alpha"])
        ])
        result = self.core.process(model=backend)
        memories = [r.memory for r in self.core._read_memories_unlocked("knowledge")]
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0].body, "alpha uses JDK 17.")
        self.assertEqual(memories[0].scopes, ["project:alpha"])
        self.assertEqual(result["deferred_candidates"], 1)
        self.assertEqual([c["purpose"] for c in backend.calls], ["gate"] * 3 + ["summarize"])
        self.assertEqual(self.core.vault.list_markdown("history"), [])
        state = json.loads(self.core.vault.processed_index_path.read_text(encoding="utf-8"))
        row = state["sessions"]["hermes/s"]["processed_turns"][0]["deferred_candidates"][0]
        self.assertEqual(row["candidate_id"], "c")
        self.assertEqual(row["scopes"], ["project:alpha"])
        self.assertEqual(row["reason"], "scope_conflict")

    def test_repeated_mixed_project_proposal_is_not_locally_split(self):
        key = self.capture("alpha uses PostgreSQL. beta uses MySQL.")
        c = self.candidate(memory="alpha uses PostgreSQL. beta uses MySQL.", evidence_event_ids=[key],
                           scopes=["project:alpha", "project:beta"])
        backend = fixture.QueueBackend([fixture.gate([c])] * 3)
        result = self.core.process(model=backend)
        self.assertEqual(result["memories_written"], 0)
        self.assertEqual(result["deferred_candidates"], 1)
        self.assertEqual([c["purpose"] for c in backend.calls], ["gate"] * 3)
        self.assertTrue(self.core.vault.inbox_path.joinpath("hermes", "s.md").exists())


    def test_model_selected_same_scope_update_is_not_second_guessed_by_core_text_matching(self):
        target = self.core.create_memory(memory_id="alpha-owner", title="alpha 负责人",
            body="alpha 负责人为甲。", type="fact", scopes=["project:alpha"])
        key = self.capture("alpha 负责人仍为甲。alpha 数据库迁移至 PostgreSQL。")
        c = self.candidate(memory="alpha 数据库迁移至 PostgreSQL。", evidence_event_ids=[key],
                           update_memory_id=target.memory_id)
        original = deepcopy(c)
        body = "alpha 数据库迁移至 PostgreSQL。"
        backend = fixture.QueueBackend([
            fixture.gate([c]),
            fixture.summary(key, title=target.title, body=body, type="fact",
                            scopes=["project:alpha"], update_memory_id=target.memory_id),
        ])
        result = self.core.process(model=backend)
        self.assertEqual(result["memory_ids"], [target.memory_id])
        self.assertEqual(c, original)
        self.assertEqual(len(self.core.vault.list_markdown("knowledge")), 1)
        self.assertEqual(self.core.read(target.memory_id).body, body)
        self.assertEqual(len(self.core.vault.list_markdown("history")), 1)
        self.assertEqual([call["purpose"] for call in backend.calls], ["gate", "summarize"])

    def test_exact_title_ambiguity_never_selects_a_target(self):
        from memleaf.processing import Processor
        records = [self.core.create_memory(memory_id="config-"+str(n), title="alpha 数据库配置",
            body=body, type="fact", scopes=["project:alpha"]).to_dict()
            for n, body in enumerate(("alpha 数据库配置使用 PostgreSQL。", "alpha 数据库配置使用 MySQL。"))]
        c = self.candidate(memory="alpha 数据库配置需调整。")
        result = Processor(self.core).inputs._infer_update_target(c, records)
        self.assertNotIn("update_memory_id", result)
        self.assertEqual(result.pop("_defer_reason"), "ambiguous_update_target")
        self.assertEqual(result, c)

    def test_model_replacement_is_not_concatenated_with_retired_body(self):
        target = self.core.create_memory(memory_id="alpha-rule", title="alpha 部署规则",
            body="alpha 部署规则使用旧验证流程。", type="project", scopes=["project:alpha"])
        key = self.capture("alpha 部署规则补充说明：旧验证流程不再采用，改为新验证流程。")
        c = self.candidate(memory="alpha 部署规则改为新验证流程。", evidence_event_ids=[key],
                           type="project", update_memory_id=target.memory_id)
        body = "alpha 部署规则采用新验证流程。"
        backend = fixture.QueueBackend([fixture.gate([c]), fixture.summary(key, title=target.title,
            body=body, type="project", scopes=["project:alpha"], update_memory_id=target.memory_id)])
        result = self.core.process(model=backend)
        self.assertEqual(result["memory_ids"], [target.memory_id])
        self.assertEqual(self.core.read(target.memory_id).body, body)
        history = self.core._read_memories_unlocked("history")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].memory.body, target.body)


class MutationBoundaryContracts(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.core = Memleaf(Path(temp.name) / "vault")

    def spy_boundary(self):
        entered = []
        original = self.core._mutation_boundary
        @contextmanager
        def spy():
            with original():
                entered.append(True)
                yield
        return entered, patch.object(self.core, "_mutation_boundary", spy)

    def test_raw_write_and_both_forget_entries_use_boundary(self):
        entered, spy = self.spy_boundary()
        with spy:
            self.core.create_memory(memory_id="one", title="One", body="body one")
            self.assertTrue(self.core.forget_memory("one"))
            self.core.create_memory(memory_id="two", title="Two", body="body two")
            self.core.forget_about("two")
        self.assertEqual(len(entered), 4)
        self.assertEqual(self.core.vault.list_markdown("knowledge"), [])

    def test_recovery_failure_prevents_write_and_releases_lock(self):
        with patch.object(self.core, "_recover_compaction_unlocked", side_effect=CompactionError("blocked")):
            with self.assertRaises(CompactionError):
                self.core.create_memory(memory_id="one", title="One", body="one")
        self.assertEqual(self.core.vault.list_markdown("knowledge"), [])
        self.core.create_memory(memory_id="two", title="Two", body="two")
        self.assertIsNotNone(self.core.read("two"))

    def test_compactor_snapshot_and_commit_share_boundary(self):
        entered, spy = self.spy_boundary()
        with spy:
            compactor = Compactor(self.core)
            selected, active, _ = compactor._snapshot(1, .3)
            self.assertEqual(selected, [])
            self.assertEqual(compactor._commit(selected, active, [], now="2026-09-05T00:00:00Z"), ([], []))
        self.assertEqual(len(entered), 2)

    def test_automatic_and_explicit_planning_do_not_hold_mutation_boundary(self):
        inside = []
        entered = []
        original = self.core._mutation_boundary
        @contextmanager
        def spy():
            with original():
                inside.append(True)
                entered.append(True)
                try:
                    yield
                finally:
                    inside.pop()
        key = event_key("u")
        c = fixture.candidate("c", [key], memory="The service uses UTF-8.")
        backend = fixture.QueueBackend([fixture.gate([c]), fixture.summary(key, title="Encoding",
                                        body="The service uses UTF-8.", type="fact")])
        real = backend.complete
        def checked(*args, **kwargs):
            self.assertFalse(inside, "model must run outside mutation critical section")
            return real(*args, **kwargs)
        backend.complete = checked
        self.core.capture("hermes", "s", "t", "user", c["memory"], event_id="u")
        self.core.capture("hermes", "s", "t", "assistant", "Noted.", event_id="a")
        with patch.object(self.core, "_mutation_boundary", spy):
            result = self.core.process(model=backend)
        self.assertEqual(result["memories_written"], 1)
        self.assertTrue(entered)
        entered.clear()
        def explicit(prompt, **kwargs):
            self.assertFalse(inside)
            candidate = json.JSONDecoder().raw_decode(prompt.split("Candidate:\n", 1)[1])[0]
            value = json.loads(fixture.summary(candidate["evidence_event_ids"][0], title="Timezone",
                                   body="The preferred timezone is UTC.", type="preference"))
            value["scope_source"] = "user"
            return json.dumps(value)
        with patch.object(self.core, "_mutation_boundary", spy):
            result = self.core.remember("The preferred timezone is UTC.", scopes=["global"], model=explicit)
        self.assertEqual(result["memories_written"], 1)
        self.assertTrue(entered)

if __name__ == "__main__":
    unittest.main()

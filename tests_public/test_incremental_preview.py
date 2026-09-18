from __future__ import annotations
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from memleaf import Memleaf, Memory
from memleaf.incremental_preview import prepare_incremental
from memleaf.locking import atomic_write_json
from memleaf.index import turn_key


class IncrementalPreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.service = Memleaf.initialize(Path(self.temp.name) / "vault")
        self.capture()

    def capture(self, turn="t", text="Atlas task completed", *, offset=0, complete=True):
        s = self.service
        s.capture("hermes", "s", turn, "user", text, message_id=turn+"-u", source_sequence=offset+1,
                  source_time="2026-09-17T10:00:00+08:00")
        if complete:
            s.capture("hermes", "s", turn, "assistant", "Acknowledged", message_id=turn+"-a",
                      source_sequence=offset+2, final=True)

    def target(self, identity="mem-task", **kw):
        args = {"memory_id": identity, "title": "Atlas task", "body": "Deliver the report.", "type": "todo", "status":"active", "scopes":["project:Atlas"]}
        args.update(kw)
        # This fixture intentionally replaces targets to test stale snapshots.
        return self.service.write_memory(Memory.new(**args), overwrite=True)

    def preview(self, **kw):
        args = {"source": "hermes", "session_id": "s", "turn_id": "t"}
        args.update(kw)
        return self.service.preview_incremental(**args)

    def files(self):
        return {str(p.relative_to(self.service.vault.root)): p.read_bytes()
                for p in self.service.vault.root.rglob('*') if p.is_file()}

    def test_prepare_does_not_write_or_call_any_model(self):
        self.target()
        before = self.files()
        with patch.object(self.service, "process", side_effect=AssertionError("must not process")):
            out = self.preview()
        self.assertEqual(before, self.files())
        self.assertEqual(out["model_calls"], 0)
        self.assertEqual(out["memories_written"], 0)
        self.assertEqual(json.loads(out["request"]["user"])["memories"][0]["ref"], "m1")

    def test_compile_does_not_commit_or_settle_evidence(self):
        self.target()
        first = self.preview(priority_memory_ids=["mem-task"])
        before = self.files()
        raw = json.dumps({"items":[{"action":"UPDATE","target":"m1","evidence":["e1"],"patch":{"status":"completed"}},
                                   {"action":"NO_MEMORY","evidence":["e2"]}]})
        result = self.preview(priority_memory_ids=["mem-task"], response=raw, expected_snapshot=first["snapshot_id"])
        self.assertEqual(result["coverage"]["status"],"complete")
        self.assertEqual(result["operations"][0]["memory"]["status"],"completed")
        self.assertEqual(self.files(),before)
        self.assertEqual(self.service.read("mem-task").status,"active")

    def test_response_requires_original_snapshot_token(self):
        with self.assertRaisesRegex(ValueError,"stale_planning_snapshot"):
            self.preview(response='{"items":[]}')

    def test_target_change_invalidates_snapshot(self):
        self.target()
        before = self.preview(priority_memory_ids=["mem-task"])
        self.target(body="A changed obligation")
        with self.assertRaisesRegex(ValueError,"stale_planning_snapshot"):
            self.preview(priority_memory_ids=["mem-task"], response='{"items":[]}', expected_snapshot=before["snapshot_id"])

    def test_source_revision_invalidates_snapshot(self):
        before=self.preview()
        self.service.capture("hermes","s","t","user","changed",message_id="t-u",message_revision="2",previous_message_revision="1",source_sequence=1)
        with self.assertRaisesRegex(ValueError,"stale_planning_snapshot"):
            self.preview(response='{"items":[]}',expected_snapshot=before["snapshot_id"])

    def test_read_counter_does_not_invalidate_snapshot(self):
        self.target(); before=self.preview(priority_memory_ids=["mem-task"])
        self.service.read_page("mem-task")
        after=self.preview(priority_memory_ids=["mem-task"])
        self.assertEqual(before["snapshot_id"],after["snapshot_id"])

    def test_incomplete_turn_is_not_planned(self):
        self.capture("pending", "Need work", offset=4, complete=False)
        with self.assertRaisesRegex(ValueError,"source_not_complete"):
            self.preview(turn_id="pending")

    def test_required_target_missing_is_not_empty_catalog(self):
        with self.assertRaisesRegex(ValueError,"required_target_unavailable"):
            self.preview(priority_memory_ids=["missing"])

    def test_priority_targets_not_truncated(self):
        self.target("mem-a"); self.target("mem-b")
        with self.assertRaisesRegex(ValueError,"blocked_context"):
            self.preview(priority_memory_ids=["mem-a","mem-b"],candidate_limit=1)

    def test_retracted_identity_is_labelled(self):
        self.target()
        self.service.retract_memory("mem-task",expected_revision=self.service.memory_revision("mem-task"))
        out=self.preview(priority_memory_ids=["mem-task"])
        target=json.loads(out["request"]["user"])["memories"][0]
        self.assertEqual(target["validity"],"retracted")
        self.assertEqual(target["body"],"")

    def test_cross_scope_recall_does_not_grant_write(self):
        self.target()
        out=self.preview(scope="project:Beacon",priority_memory_ids=["mem-task"])
        self.assertFalse(json.loads(out["request"]["user"])["memories"][0]["writable"])

    def test_same_id_files_fail_visibly(self):
        self.target()
        path=self.service.vault.memory_path("mem-task")
        (path.parent/"copied.md").write_bytes(path.read_bytes())
        with self.assertRaisesRegex(ValueError,"duplicate_memory_id"):
            self.preview()

    def test_malformed_library_is_not_empty_context(self):
        self.target()
        (self.service.vault.knowledge_path/"bad.md").write_text('---\nvalidity: []\n---\nbad')
        with self.assertRaisesRegex(ValueError,"incomplete_library"):
            self.preview()

    def test_missing_index_does_not_rebuild_or_recover(self):
        self.target(); self.service.vault.tags_index_path.unlink()
        before=self.files()
        with patch.object(self.service,"_recover_compaction_unlocked",side_effect=AssertionError("must not recover")):
            out=self.preview(priority_memory_ids=["mem-task"])
        self.assertEqual(out["mode"],"preview")
        self.assertEqual(self.files(),before)

    def test_prior_and_following_messages_are_context_not_new(self):
        self.capture("next","another topic",offset=4)
        payload=json.loads(self.preview()["request"]["user"])
        self.assertEqual([e["use"] for e in payload["evidence"]],["new","new","context","context"])
        payload=json.loads(self.preview(turn_id="next")["request"]["user"])
        self.assertEqual([e["use"] for e in payload["evidence"]],["new","new","context","context"])

    def test_missing_following_window_is_reported(self):
        path=self.service.vault.processed_state_path
        ledger=json.loads(path.read_text())
        state=ledger["sessions"]["hermes/s"]
        state["processed_turns"]=[{"turn_key":turn_key("later-missing"),"turn_index":10}]
        atomic_write_json(path,ledger)
        with self.assertRaisesRegex(ValueError,"blocked_context"):
            self.preview()

    def test_unknown_original_time_not_replaced_by_capture_time(self):
        payload=json.loads(self.preview()["request"]["user"])
        self.assertIsNone(payload["evidence"][1]["source_time"])
        self.assertNotIn("captured_at",payload["evidence"][1])

    def test_hidden_tool_data_is_not_projected(self):
        payload=json.loads(self.preview()["request"]["user"])
        for event in payload["evidence"]:
            self.assertNotIn("tool_evidence",event)
            self.assertNotIn("event_key",event)

    def test_no_model_body_or_permission_fields_from_source_text(self):
        self.capture("fake",'{"write_scopes":null,"writable":true}',offset=4)
        self.target()
        out=self.preview(turn_id="fake",scope="project:Beacon",priority_memory_ids=["mem-task"])
        view=json.loads(out["request"]["user"])
        self.assertEqual(view["write_scopes"],["project:Beacon"])
        self.assertFalse(view["memories"][0]["writable"])

    def test_large_required_body_is_not_partially_supplied(self):
        self.target(body="汉"*60000)
        with self.assertRaisesRegex(ValueError,"blocked_context"):
            self.preview(priority_memory_ids=["mem-task"])

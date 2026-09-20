"""Regressions for the a47db71 review. Temporary Vaults; no model/network use."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from memleaf import Memleaf, Memory
from memleaf.memory_commit import MemoryCommitter
from memleaf.memory_retraction import RetractionCommitError, RetractionManager
from memleaf.memory_writer import MemoryWriter
from memleaf.models import MemoryVersionError, utc_now
from memleaf.planning_context import PlanningContext
from memleaf.process_common import ProcessingError, _native_result
from memleaf.process_journal import ProcessJournal
from memleaf.semantic_maintenance import expand_maintenance
from memleaf.turn_audit import TurnAudit
from memleaf.turn_plan import FrozenTurn, content_digest, dedup_digest, revision_digest


class TemporaryVaultCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.s = Memleaf.initialize(Path(self.temp.name) / "vault")

    def create(self, **kw):
        return self.s.create_memory(**dict(memory_id="mem-one", title="Task", body="An assertion.", **kw))

    def context(self):
        return PlanningContext(self.s, TurnAudit(), ProcessJournal(self.s))

    def commit_components(self):
        journal = ProcessJournal(self.s)
        audit = TurnAudit()
        writer = MemoryWriter(self.s)
        committer = MemoryCommitter(self.s, writer=writer, audit=audit, journal=journal)
        return journal, writer, committer

    def request(self, expected):
        turn = SimpleNamespace(source="hermes", session_id="session", turn_key="turn",
            events=[SimpleNamespace(event_key="event", role="user", content="Updated", tool_evidence=())])
        return {"turn": turn, "memory_id": "mem-one", "candidate_id": "c1", "evidence_unit_ids": ["e1"],
                "expected_revision": expected, "summary": {"title": "Task", "body": "Updated", "type": "fact",
                "tags": [], "scopes": ["global"], "update_memory_id": "mem-one"}}

    def validate(self, req):
        with self.s.vault.lock():
            MemoryCommitter(self.s, MemoryWriter(self.s), None, None)._validate_target_revisions_unlocked([req])

    def legacy(self):
        m = self.create(type="fact")
        raw = m.to_markdown().replace('validity: "valid"\n', '')
        path = self.s.vault.memory_path(m.memory_id, "knowledge")
        path.write_text(raw, encoding="utf-8")
        value = m.to_dict(); value.pop("validity")
        return path, revision_digest(value)


class ReferenceIsolationTests(unittest.TestCase):
    @staticmethod
    def run_disposition(entry, *, key="discard", memories=None):
        original = {"memories": [{"target": None, "scope": "global", "type": "fact",
                    "title": "Task", "body": "Meaning", "evidence": [2]}], "no_memory": [], "deferred": []}
        value = {"memories": memories or [], "discard": [], "deferred": []}; value[key] = [entry]
        diagnostics = []
        result = json.loads(expand_maintenance(json.dumps(value), (original, {1: original["memories"][0]}, {}),
                                               diagnostics=diagnostics))
        return result, diagnostics

    def test_unknown_explicit_from_is_not_evidence(self):
        result, diagnostics = self.run_disposition({"from": [2]})
        self.assertEqual(result["no_memory"], [])
        self.assertEqual(result["deferred"], [2])
        self.assertTrue(diagnostics)

    def test_unknown_bare_incoming_is_not_evidence(self):
        result, diagnostics = self.run_disposition(2)
        self.assertEqual(result["no_memory"], [])
        self.assertEqual(result["deferred"], [2])
        self.assertTrue(diagnostics)

    def test_explicit_evidence_can_settle_fragment(self):
        result, diagnostics = self.run_disposition({"evidence": [" 2 "]})
        self.assertEqual(result["no_memory"], [2])
        self.assertEqual(result["deferred"], [])
        self.assertEqual(diagnostics, [])

    def test_known_incoming_string_and_prefix_normalize(self):
        for ref in (1, "1", " d1 ", "D1", " 01 "):
            with self.subTest(ref=ref):
                result, diagnostics = self.run_disposition({"from": [ref]})
                self.assertEqual(result["no_memory"], [2])
                self.assertEqual(result["deferred"], [])
                self.assertEqual(diagnostics, [])

    def test_bad_reference_does_not_drop_valid_sibling(self):
        rows = [{"from": ["d1"], "title": "Task", "body": "Meaning", "scope": "global", "type": "fact"}]
        result, diagnostics = self.run_disposition({"from": [2]}, memories=rows)
        self.assertEqual(len(result["memories"]), 1)
        self.assertEqual(result["no_memory"], [])
        self.assertTrue(diagnostics)

    def test_container_and_boolean_references_fail_locally(self):
        for ref in (True, {}, [], "missing"):
            with self.subTest(ref=ref):
                result, diagnostics = self.run_disposition({"from": [ref]})
                self.assertEqual(result["no_memory"], [])
                self.assertEqual(result["deferred"], [2])
                self.assertTrue(diagnostics)


class SemanticDigestTests(TemporaryVaultCase):
    base = {"title": "Task", "body": "Meaning", "type": "todo", "status": "active", "scopes": ["global"]}

    def test_source_basis_does_not_change_business_digest(self):
        a = dict(self.base, field_basis={"content": {"event_key": "a"}})
        b = dict(self.base, field_basis={"content": {"event_key": "b"}})
        self.assertEqual(dedup_digest(a), dedup_digest(b))
        self.assertNotEqual(revision_digest(a), revision_digest(b))

    def test_real_business_fields_still_change_digest(self):
        for fields in ({"assignee": "user-2"}, {"waiting_on": "reply"}, {"due_text": "tomorrow"},
                       {"due_date": "2026-09-19"}, {"status": "completed"}, {"validity": "retracted"}):
            with self.subTest(fields=fields):
                self.assertNotEqual(content_digest(self.base), content_digest(dict(self.base, **fields)))

    def test_unresolved_anchor_ignores_provenance_but_keeps_calendar(self):
        a = dict(self.base, due_text="tomorrow", due_anchor={"source_time": "2026-09-17", "event_key": "a"})
        b = dict(a, due_anchor={"source_time": "2026-09-17", "event_key": "b"})
        c = dict(a, due_anchor={"source_time": "2026-09-18", "event_key": "a"})
        self.assertEqual(content_digest(a), content_digest(b))
        self.assertNotEqual(content_digest(a), content_digest(c))

    def test_resolved_deadline_ignores_different_observation(self):
        a = dict(self.base, due_date="2026-09-20", due_text="release day", due_anchor={"source_time": "2026-09-17"})
        b = dict(a, due_anchor={"source_time": "2026-09-18", "event_key": "another-source"})
        self.assertEqual(content_digest(a), content_digest(b))

    def test_distinct_named_tasks_remain_distinct(self):
        self.assertNotEqual(dedup_digest(self.base), dedup_digest(dict(self.base, title="Next cycle task")))

    def test_actual_commit_does_not_duplicate_source_only_changes(self):
        self.s.create_memory(memory_id="mem-existing", title="Deployment", body="Approval required.", type="fact",
                             field_basis={"content": {"event_key": "old"}})
        self.s.capture("hermes", "session", "turn-1", "user", "Approval required.")
        self.s.capture("hermes", "session", "turn-1", "assistant", "Noted.")
        journal, writer, committer = self.commit_components(); now = utc_now()
        snapshots, _ = journal._snapshot(source="hermes", session_id="session", now=now, cleanup_hours=24, scope=None)
        turn = snapshots[0].turn
        request = {"turn": turn, "summary": {"title": "Deployment", "body": "Approval required.", "type": "fact",
            "tags": [], "scopes": ["global"], "aliases": [], "keywords": []}, "memory_id": "mem-new",
            "candidate_id": "c1", "evidence_unit_ids": ["e1"], "conversation_title": "audit", "event_key": turn.events[0].event_key}
        result = committer._commit_success(snapshots, [request], now=now, cleanup_hours=24)
        self.assertEqual(result, [])
        self.assertEqual(request["duplicate_memory_id"], "mem-existing")
        self.assertEqual(len(self.s.vault.list_markdown("knowledge")), 1)


class RetractionRetrievalTests(TemporaryVaultCase):
    def withdrawn(self, scopes):
        self.create(type="todo", status="active", scopes=scopes)
        self.s.retract_memory("mem-one", expected_revision=self.s.memory_revision("mem-one"))

    def test_project_fallback_excludes_withdrawn_head(self):
        self.withdrawn(["project:Atlas"])
        related, _, _, _ = self.context()._related_query(SimpleNamespace(source="hermes"), {}, "Task",
                                                        explicit_scope=["project:Atlas"])
        self.assertEqual(related, [])

    def test_global_recall_excludes_withdrawn_head(self):
        self.withdrawn(["global"])
        related, _, _, _ = self.context()._related_query(SimpleNamespace(source="hermes"), {}, "Task")
        self.assertEqual(related, [])

    def test_priority_snapshot_cannot_bypass_eligibility(self):
        self.withdrawn(["project:Atlas"])
        records = self.s._read_memories_unlocked("knowledge")
        related, _, _, _ = self.context()._related_query(SimpleNamespace(source="hermes"), {}, "Task",
            explicit_scope=["project:Atlas"], priority_memory_ids=["mem-one"], priority_only=True, scope_records=records)
        self.assertEqual(related, [])

    def test_single_pass_contextual_union_excludes_withdrawn(self):
        self.withdrawn(["project:Atlas"])
        self.s.capture("hermes", "session", "t1", "user", "Atlas Task")
        self.s.capture("hermes", "session", "t1", "assistant", "Noted")
        from memleaf.inbox import parse_inbox_file
        turn = parse_inbox_file(self.s.vault.session_path("hermes", "session"))[0]
        related, *_ = self.context()._single_pass_related(turn, {}, explicit_scope=["project:Atlas"])
        self.assertFalse(any(row.get("memory_id") == "mem-one" for row in related))

    def test_withdrawn_overlay_removes_same_id(self):
        valid = {"memory_id": "mem-one", "title": "Task", "body": "Meaning", "validity": "valid"}
        withdrawn = dict(valid, validity="retracted", body="")
        self.assertEqual(PlanningContext._overlay_related([valid], [withdrawn]), [])

    def test_projection_excludes_withdrawn_and_preserves_fields(self):
        m = self.create(type="todo", status="active", assignee="person", waiting_on=None, due_text="launch")
        rows = _native_result([m]); self.assertEqual(rows[0]["validity"], "valid")
        self.assertEqual(rows[0]["assignee"], "person"); self.assertIn("waiting_on", rows[0])
        m.validity = "retracted"; m.body = ""
        self.assertEqual(_native_result([m, m.to_dict()]), [])

    def test_completed_valid_head_remains_available(self):
        self.create(type="todo", status="completed", scopes=["project:Atlas"])
        rows, _, _, _ = self.context()._related_query(SimpleNamespace(source="hermes"), {}, "Task",
                                                     explicit_scope=["project:Atlas"])
        self.assertEqual(rows[0]["memory_id"], "mem-one")
        self.assertEqual(rows[0]["status"], "completed")


class LegacyRevisionTests(TemporaryVaultCase):
    def test_unchanged_legacy_file_accepts_frozen_expected_revision(self):
        path, expected = self.legacy(); before = path.read_bytes(); req = self.request(expected)
        frozen = FrozenTurn.build(req["turn"], [req])
        restored = FrozenTurn.restore(frozen.to_dict(), req["turn"])
        self.validate(restored["requests"][0]); self.assertEqual(before, path.read_bytes())

    def test_actual_legacy_edit_still_conflicts(self):
        path, expected = self.legacy()
        path.write_text(path.read_text().replace("An assertion.", "User edit."), encoding="utf-8")
        with self.assertRaises(ProcessingError): self.validate(self.request(expected))

    def test_explicit_new_state_does_not_accept_old_format_digest(self):
        m = self.create(type="fact"); value = m.to_dict(); value.pop("validity")
        with self.assertRaises(ProcessingError): self.validate(self.request(revision_digest(value)))

    def test_legacy_token_cannot_overwrite_retraction(self):
        _, expected = self.legacy()
        self.s.retract_memory("mem-one", expected_revision=self.s.memory_revision("mem-one"))
        with self.assertRaises(ProcessingError): self.validate(self.request(expected))
        self.assertEqual(self.s.read("mem-one", include_history=True).validity, "retracted")

    def test_current_a47_revision_still_works(self):
        self.create(type="fact"); self.validate(self.request(self.s.memory_revision("mem-one")))

    def test_real_legacy_write_and_applied_retry(self):
        _, expected = self.legacy()
        self.s.capture("hermes", "session", "turn-1", "user", "Updated")
        self.s.capture("hermes", "session", "turn-1", "assistant", "Noted")
        journal, writer, committer = self.commit_components(); now = utc_now()
        snapshots, _ = journal._snapshot(source="hermes", session_id="session", now=now, cleanup_hours=24, scope=None)
        req = self.request(expected); req["turn"] = snapshots[0].turn
        req.update(conversation_title="audit", event_key=snapshots[0].turn.events[0].event_key)
        self.assertEqual(committer._commit_success(snapshots, [req], now=now, cleanup_hours=24), ["mem-one"])
        self.assertEqual(self.s.read("mem-one").body, "Updated")
        count = len(self.s.vault.list_markdown("history"))
        # The request has already been applied; validating/replaying cannot
        # manufacture another history version merely because the head is new.
        self.validate(req)
        with self.s.vault.lock(): writer.write_many_unlocked([req], now=now)
        self.assertEqual(len(self.s.vault.list_markdown("history")), count)


class ValidityTypeTests(TemporaryVaultCase):
    def test_invalid_values_are_controlled_validation_failures(self):
        for value in ([], {}, None, False, 1, "invalid"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Memory.from_mapping({"memory_id": "bad", "title": "Bad", "body": "body", "validity": value})

    def test_bad_record_does_not_crash_unrelated_reads(self):
        self.create(type="todo", status="active")
        bad = self.s.vault.memory_path("mem-bad", "knowledge")
        bad.write_text('---\nmemory_id: "mem-bad"\ntitle: "Bad"\nvalidity: []\n---\n\nBad record\n', encoding="utf-8")
        self.assertIsNotNone(self.s.read("mem-one"))
        self.assertEqual(self.s.list_todos()["results"][0]["memory_id"], "mem-one")
        self.assertEqual(self.s.rebuild_index()["knowledge"], 1)
        self.assertTrue(bad.exists())


class RetractionRecoveryTests(TemporaryVaultCase):
    def journal(self):
        return RetractionManager(self.s)._path("mem-one")

    def fail_index(self):
        self.create(tags=["prior-index"])
        expected = self.s.memory_revision("mem-one")
        with patch.object(self.s, "_rebuild_index_unlocked", side_effect=OSError("injected")):
            with self.assertRaises(RetractionCommitError) as error:
                self.s.retract_memory("mem-one", expected_revision=expected, reason="withdrawn")
        self.assertTrue(error.exception.applied); self.assertTrue(self.journal().exists())
        return expected

    def test_restart_and_original_request_resume_index_without_duplicate_history(self):
        expected = self.fail_index()
        self.s = Memleaf(self.s.vault.root)
        with patch.object(self.s, "_rebuild_index_unlocked", wraps=self.s._rebuild_index_unlocked) as rebuild:
            self.s.retract_memory("mem-one", expected_revision=expected, reason="withdrawn")
        self.assertEqual(rebuild.call_count, 1)
        self.assertEqual(len(self.s.vault.list_markdown("history")), 1)
        self.assertFalse(self.journal().exists())
        self.assertEqual(self.s.search_candidates("Task")["status"], "no_match")

    def test_current_revision_can_resume_without_rewriting_history(self):
        self.fail_index()
        expected = self.s.memory_revision("mem-one")
        self.s.retract_memory("mem-one", expected_revision=expected)
        self.assertFalse(self.journal().exists())
        self.assertEqual(len(self.s.vault.list_markdown("history")), 1)

    def test_old_success_receipt_is_idempotent_after_journal_cleanup(self):
        self.create(); expected = self.s.memory_revision("mem-one")
        first = self.s.retract_memory("mem-one", expected_revision=expected, reason="withdrawn")
        second = self.s.retract_memory("mem-one", expected_revision=expected, reason="withdrawn")
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(len(self.s.vault.list_markdown("history")), 1)

    def test_later_valid_edit_wins_over_pending_retraction(self):
        expected = self.fail_index()
        value = self.s.read("mem-one", include_history=True).to_dict()
        value.update(body="New authorized assertion.", validity="valid")
        self.s.write_memory(value)
        with self.assertRaises(MemoryVersionError):
            self.s.retract_memory("mem-one", expected_revision=expected, reason="withdrawn")
        self.assertEqual(self.s.read("mem-one").body, "New authorized assertion.")
        self.assertFalse(self.journal().exists())

    def test_later_retracted_edit_is_not_mistaken_for_original_receipt(self):
        expected = self.fail_index()
        value = self.s.read("mem-one", include_history=True).to_dict(); value["title"] = "User-edited title"
        self.s.write_memory(value)
        with self.assertRaises(MemoryVersionError):
            self.s.retract_memory("mem-one", expected_revision=expected, reason="withdrawn")
        self.assertEqual(self.s.read("mem-one", include_history=True).title, "User-edited title")

    def test_forget_cancels_pending_plaintext_and_does_not_resurrect(self):
        expected = self.fail_index()
        self.assertTrue(self.s.forget_memory("mem-one"))
        self.assertFalse(self.journal().exists())
        self.assertEqual(self.s.vault.list_markdown("history"), [])
        with self.assertRaises(ValueError):
            self.s.retract_memory("mem-one", expected_revision=expected, reason="withdrawn")
        self.assertEqual(self.s.vault.list_markdown("knowledge"), [])

    def test_journal_corruption_is_preserved_and_not_replayed(self):
        expected = self.fail_index()
        self.journal().write_text('{"schema_version":1,"payload":"bad","checksum":"bad"}', encoding="utf-8")
        before = self.journal().read_bytes()
        with self.assertRaises(ValueError):
            self.s.retract_memory("mem-one", expected_revision=expected, reason="withdrawn")
        self.assertEqual(before, self.journal().read_bytes())

    def test_serialization_is_preflighted_before_history(self):
        self.create(); expected = self.s.memory_revision("mem-one")
        with self.assertRaises(ValueError):
            self.s.retract_memory("mem-one", expected_revision=expected, reason="line one\nline two")
        self.assertEqual(self.s.vault.list_markdown("history"), [])
        self.assertFalse(self.journal().exists()); self.assertIsNotNone(self.s.read("mem-one"))

    def test_each_durable_boundary_recovers_exactly_once(self):
        import memleaf.memory_retraction as module
        for point in ("history_before", "history_after", "head_before", "head_after", "index_before", "index_after",
                      "receipt_before", "receipt_after", "prepare_before", "prepare_after"):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as root:
                service = Memleaf.initialize(Path(root) / "vault")
                service.create_memory(memory_id="mem-one", title="Task", body="Old content")
                expected = service.memory_revision("mem-one")
                owner, name = ((MemoryWriter, "_write_history") if point.startswith("history") else
                               (module, "atomic_write_text") if point.startswith("head") else
                               (service, "_rebuild_index_unlocked") if point.startswith("index") else
                               (module, "atomic_unlink") if point.startswith("receipt") else (module, "atomic_write_json"))
                original = getattr(owner, name)
                def fail(*args, **kwargs):
                    if point.endswith("after"): original(*args, **kwargs)
                    raise OSError("injected at " + point)
                with patch.object(owner, name, side_effect=fail, autospec=True):
                    with self.assertRaises(OSError): service.retract_memory("mem-one", expected_revision=expected)
                restarted = Memleaf(service.vault.root)
                restarted.retract_memory("mem-one", expected_revision=expected)
                self.assertEqual(len(restarted.vault.list_markdown("history")), 1)
                self.assertIsNone(restarted.read("mem-one"))
                self.assertFalse(RetractionManager(restarted)._path("mem-one").exists())

    def test_restored_head_can_be_retracted_with_a_new_receipt(self):
        self.create(); first_revision = self.s.memory_revision("mem-one")
        old = self.s.retract_memory("mem-one", expected_revision=first_revision, reason="old reason")
        restored = old.to_dict(); restored.update(validity="valid", body="New confirmed assertion")
        self.s.write_memory(restored)
        new_revision = self.s.memory_revision("mem-one")
        current = self.s.retract_memory("mem-one", expected_revision=new_revision)
        self.assertNotIn("retraction_reason", current.extra)
        repeated = self.s.retract_memory("mem-one", expected_revision=new_revision)
        self.assertEqual(current.to_dict(), repeated.to_dict())
        self.assertEqual(len(self.s.vault.list_markdown("history")), 2)

    def test_hard_process_exit_recovers_without_finally_blocks(self):
        code = """
from memleaf import Memleaf
import memleaf.memory_retraction as module
from memleaf.memory_writer import MemoryWriter
import sys, os
service = Memleaf(sys.argv[1])
if sys.argv[3] == 'head':
    original = module.atomic_write_text
    def stop(*args, **kwargs):
        original(*args, **kwargs)
        os._exit(23)
    module.atomic_write_text = stop
else:
    original = MemoryWriter._write_history
    def stop(*args, **kwargs):
        original(*args, **kwargs)
        os._exit(23)
    MemoryWriter._write_history = stop
service.retract_memory('mem-one', expected_revision=sys.argv[2])
"""
        for point in ("history", "head"):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as root:
                service = Memleaf.initialize(Path(root) / "vault")
                service.create_memory(memory_id="mem-one", title="Task", body="Old assertion")
                expected = service.memory_revision("mem-one")
                result = subprocess.run([sys.executable, "-c", code, str(service.vault.root), expected, point],
                                        capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 23, result.stderr)
                restarted = Memleaf(service.vault.root)
                restarted.retract_memory("mem-one", expected_revision=expected)
                self.assertIsNone(restarted.read("mem-one"))
                self.assertEqual(len(restarted.vault.list_markdown("history")), 1)
                self.assertFalse(RetractionManager(restarted)._path("mem-one").exists())

    def test_two_processes_replaying_same_authorized_request(self):
        self.create(); expected = self.s.memory_revision("mem-one")
        code = "from memleaf import Memleaf; import sys; Memleaf(sys.argv[1]).retract_memory('mem-one', expected_revision=sys.argv[2])"
        processes = [subprocess.Popen([sys.executable, "-c", code, str(self.s.vault.root), expected],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
        for process in processes:
            out, err = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, err)
        self.assertEqual(len(self.s.vault.list_markdown("history")), 1)
        self.assertIsNone(self.s.read("mem-one"))


if __name__ == "__main__":
    unittest.main()

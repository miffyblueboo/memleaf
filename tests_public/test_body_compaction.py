"""Body compaction cannot silently change the state of its source record."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from memleaf import Memleaf, Memory
from memleaf.body_compaction import (
    BODY_COMPACT_SYSTEM, parse_body_compact_output, replacement_memory,
)
from memleaf.compaction import CompactionError, Compactor
from memleaf.config import save_config
from memleaf.locking import atomic_write_json, atomic_write_text
from memleaf.validation import ModelOutputError

NOW = "2026-09-18T00:00:00Z"
LONG = "The deliverable is a design report, not implementation. " * 50
SHORT = "Deliver the design report; implementation is excluded."


def output(*rows):
    return json.dumps({"memories": list(rows)}, ensure_ascii=False)


def item(memory_id="task", body=SHORT):
    return {"source_memory_ids": [memory_id], "body": body}


class BodyCompactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "记忆 vault"
        self.s = Memleaf(self.root, clock=lambda: NOW)
        config = self.s.vault.config()
        config["process"]["memory_compact_threshold_tokens"] = 1
        config["process"]["memory_compact_candidate_ratio"] = 1.0
        save_config(self.s.vault.config_path, config)
        self.calls = []

    def create(self, memory_id="task", **overrides):
        fields = dict(memory_id=memory_id, title="Design obligation", body=LONG,
                      type="todo", status="completed", scopes=["project:Atlas"],
                      scope_source="user", tags=["design"], aliases=[], keywords=[],
                      due_date="2026-09-20", completed_at=NOW,
                      assignee="owner", waiting_on=None, due_text="by 2026-09-20",
                      due_anchor={"source_time": "2026-09-17T09:00:00+08:00"},
                      field_basis={"deadline": {"event_key": "original-deadline", "cleared": False}},
                      custom={"nested": ["kept"]}, explicit_remember=True)
        fields.update(overrides)
        if fields["type"] != "todo":
            fields["due_date"] = None
        self.s.create_memory(**fields)
        return self.load(memory_id)

    def load(self, memory_id="task"):
        path = self.s.vault.memory_path(memory_id, "knowledge")
        return Memory.from_markdown(path.read_text(encoding="utf-8"), path)

    def backend(self, raw):
        def complete(prompt, **kwargs):
            self.calls.append((prompt, kwargs))
            return raw
        class Backend:
            pass
        backend = Backend(); backend.complete = complete
        return backend

    def compact(self, *rows):
        return self.s.compact(model=self.backend(output(*rows)))

    def assert_preserved(self, before, after):
        left, right = before.to_dict(), after.to_dict()
        for mapping in (left, right):
            for field in ("body", "updated", "compacted_at", "compaction_source_ids"):
                mapping.pop(field, None)
        self.assertEqual(left, right)

    def legacy(self, source, **changes):
        fields = {k: getattr(source, k) for k in
                  ("title", "body", "tags", "type", "scopes", "scope_source", "aliases", "keywords")}
        fields.update(item(source.memory_id))
        fields.update(changes)
        return fields

    def test_minimal_body_response_preserves_closed_state_and_every_metadata(self):
        before = self.create()
        result = self.compact(item())
        self.assertEqual(result["status"], "compacted")
        after = self.load(); self.assert_preserved(before, after)
        self.assertEqual(after.status, "completed")
        self.assertEqual(after.due_date, "2026-09-20")
        self.assertEqual(after.completed_at, NOW)
        self.assertEqual(after.body, SHORT)
        self.assertEqual(result["replacements"], ["task"])
        self.assertEqual(len(self.calls), 1)

    def test_legacy_echo_without_optional_dates_cannot_clear_them(self):
        before = self.create()
        self.compact(self.legacy(before))
        self.assert_preserved(before, self.load())

    def test_legacy_identical_optional_fields_are_accepted(self):
        before = self.create()
        self.compact(self.legacy(before, status=before.status, due_date=before.due_date, completed_at=before.completed_at))
        self.assert_preserved(before, self.load())

    def test_legacy_protected_field_drift_is_rejected_before_history(self):
        before = self.create()
        changes = {"title": "Different task", "type": "fact", "scopes": ["project:Beacon"],
                   "scope_source": "model", "tags": ["changed"], "aliases": ["changed"],
                   "keywords": ["changed"], "status": "active", "due_date": "2026-10-01", "completed_at": None}
        raw = self.s.vault.memory_path("task", "knowledge").read_bytes()
        for field, value in changes.items():
            with self.subTest(field=field), self.assertRaises(CompactionError):
                self.compact(self.legacy(before, **{field: value}))
            self.assertEqual(self.s.vault.memory_path("task", "knowledge").read_bytes(), raw)
            self.assertEqual(self.s.vault.list_markdown("history"), [])

    def test_active_cancelled_fact_and_preference_remain_their_original_types(self):
        for n, (kind, status) in enumerate((("todo", "active"), ("todo", "cancelled"), ("fact", None), ("preference", None))):
            before = self.create(str(n), type=kind, status=status, completed_at=None)
            self.compact(item(str(n)))
            self.assert_preserved(before, self.load(str(n)))

    def test_explicit_clear_and_its_basis_remain_present(self):
        before = self.create(due_date=None, due_text=None, assignee=None,
                             field_basis={"deadline": {"event_key": "cancelled", "cleared": True}})
        self.compact(item()); self.assert_preserved(before, self.load())
        self.assertIsNone(self.load().extra["due_text"])
        self.assertTrue(self.load().extra["field_basis"]["deadline"]["cleared"])

    def test_source_is_deep_copied_and_prior_compaction_ancestry_kept(self):
        before = self.create(compaction_source_ids=["historical-a", "task"])
        result = replacement_memory(before, item(), now=NOW)
        result.extra["custom"]["nested"].append("mutated copy")
        self.assertEqual(before.extra["custom"], {"nested": ["kept"]})
        self.assertEqual(result.extra["compaction_source_ids"], ["historical-a", "task"])

    def test_all_original_sources_are_preserved(self):
        before = self.create(sources=[{"source": "hermes", "session_id": "s", "turn_id": "t"}])
        self.compact(item()); self.assert_preserved(before, self.load())

    def test_history_keeps_original_business_state(self):
        before = self.create(); result = self.compact(item())
        history = self.s.vault.memory_path(result["history_written"][0], "history")
        old = Memory.from_markdown(history.read_text(encoding="utf-8"))
        for field in ("body", "status", "completed_at", "due_date", "validity", "scopes"):
            self.assertEqual(getattr(old, field), getattr(before, field))
        self.assertEqual(old.extra["field_basis"], before.extra["field_basis"])

    def test_multi_source_facts_never_merge_or_remove_identity(self):
        a = self.create("a", type="fact", status=None, completed_at=None)
        self.create("b", type="fact", status=None, completed_at=None)
        with self.assertRaises(CompactionError):
            self.compact(self.legacy(a, source_memory_ids=["a", "b"]))
        self.assertEqual(len(self.s.vault.list_markdown("knowledge")), 2)
        self.assertEqual(self.s.vault.list_markdown("history"), [])

    def test_two_independent_rows_keep_two_ids(self):
        a=self.create("a"); b=self.create("b")
        result=self.compact(item("a"), item("b"))
        self.assertEqual(result["replacements"], ["a", "b"])
        self.assert_preserved(a,self.load("a")); self.assert_preserved(b,self.load("b"))

    def test_omitted_source_remains_untouched(self):
        self.create("a"); b=self.create("b")
        self.compact(item("a"))
        self.assertEqual(b.to_dict(),self.load("b").to_dict())

    def test_same_body_and_empty_response_are_noop_without_history(self):
        before = self.create()
        for rows in ((), (item(body=LONG),)):
            self.assertEqual(self.compact(*rows)["status"], "noop")
            self.assertEqual(before.to_dict(), self.load().to_dict())
            self.assertEqual(self.s.vault.list_markdown("history"), [])

    def test_longer_replacement_is_rejected(self):
        before=self.create()
        with self.assertRaises(CompactionError): self.compact(item(body=LONG*2))
        self.assertEqual(before.to_dict(), self.load().to_dict())

    def test_repeating_short_body_does_not_create_second_history(self):
        self.create(); self.compact(item()); after=self.load()
        self.assertEqual(self.compact(item())["status"], "noop")
        self.assertEqual(after.to_dict(),self.load().to_dict())
        self.assertEqual(len(self.s.vault.list_markdown("history")),1)

    def test_retracted_heads_never_enter_model_input(self):
        self.create(body="", validity="retracted")
        result=self.compact(item())
        self.assertEqual(self.calls, [])
        self.assertEqual(result["compacted"], 0)
        self.assertEqual(self.load().validity,"retracted")

    def test_duplicate_id_is_not_chosen_as_canonical(self):
        self.create()
        src=self.s.vault.memory_path("task","knowledge")
        src.with_name("separate-copy.md").write_bytes(src.read_bytes())
        with self.assertRaises(CompactionError): self.compact(item())
        self.assertEqual(self.calls, [])

    def test_duplicate_added_during_model_call_blocks_commit(self):
        self.create()
        def callback(prompt):
            src=self.s.vault.memory_path("task","knowledge")
            src.with_name("separate-copy.md").write_bytes(src.read_bytes())
            return output(item())
        with self.assertRaises(CompactionError): self.s.compact(model=callback)
        self.assertEqual(self.load().body,LONG)
        self.assertEqual(self.s.vault.list_markdown("history"),[])

    def test_context_has_responsibility_without_control_provenance(self):
        self.create(); self.compact(item())
        prompt, kwargs=self.calls[0]
        self.assertIn('"assignee":"owner"',prompt)
        self.assertIn('"status":"completed"',prompt)
        self.assertNotIn('field_basis',prompt)
        self.assertNotIn('original-deadline',prompt)
        self.assertEqual(kwargs["purpose"],"compact")
        self.assertEqual(kwargs["system"],BODY_COMPACT_SYSTEM)

    def test_external_edit_during_model_call_wins(self):
        self.create()
        def callback(prompt):
            changed=self.load(); changed.body="Newer authorized body"
            atomic_write_text(self.s.vault.memory_path(changed.memory_id,"knowledge"),changed.to_markdown())
            return output(item())
        with self.assertRaises(CompactionError): self.s.compact(model=callback)
        self.assertEqual(self.load().body,"Newer authorized body")
        self.assertEqual(self.s.vault.list_markdown("history"),[])

    def test_backend_runs_outside_file_lock(self):
        self.create()
        def callback(prompt):
            code="from pathlib import Path; from memleaf.locking import VaultLock; import sys;\nwith VaultLock(Path(sys.argv[1])): print('acquired')"
            env=dict(os.environ,PYTHONPATH=str(Path(__import__('memleaf').__file__).resolve().parent.parent))
            done=subprocess.run([sys.executable,"-c",code,str(self.root/'_state/vault.lock')],capture_output=True,text=True,env=env,timeout=10)
            self.assertEqual(done.returncode,0,done.stderr)
            return output(item())
        self.s.compact(model=callback)

    def test_index_failure_rolls_back_complete_snapshot(self):
        self.create()
        before=self.s.vault.memory_path("task","knowledge").read_bytes()
        original=self.s._rebuild_index_unlocked; calls=[]
        def fail_once():
            calls.append(1)
            if len(calls)==1: raise OSError("injected index failure")
            return original()
        with patch.object(self.s,"_rebuild_index_unlocked",side_effect=fail_once),self.assertRaises(CompactionError):
            self.compact(item())
        self.assertEqual(self.s.vault.memory_path("task","knowledge").read_bytes(),before)
        self.assertEqual(self.s.vault.list_markdown("history"),[])
        self.assertFalse(self.s.vault.compaction_journal_path.exists())
        self.compact(item());self.assertEqual(self.load().status,"completed")

    def test_legacy_multisource_rollback_still_restores_both_originals(self):
        self.create("a");self.create("b")
        originals={k:self.s.vault.memory_path(k,"knowledge").read_text(encoding="utf-8") for k in ("a","b")}
        sha=lambda text:hashlib.sha256(text.encode("utf-8")).hexdigest()
        c=Compactor(self.s); tx="legacy-rollback"
        with self.s.vault.lock():
            staging=c._staging_dir_unlocked(tx,create=True)
            for k,raw in originals.items():atomic_write_text(staging/(k+".md"),raw)
            changed=self.load("a");changed.body="Old merged body"
            raw=changed.to_markdown();atomic_write_text(self.s.vault.memory_path(changed.memory_id,"knowledge"),raw)
            self.s.vault.memory_path("b","knowledge").unlink()
            c._write_journal_unlocked({"version":1,"transaction_id":tx,"phase":"sources_removed",
                "sources":[{"memory_id":k,"sha256":sha(v),"staging_file":k+".md"} for k,v in originals.items()],
                "replacements":[{"memory_id":"a","sha256":sha(raw)}],"histories":[]})
            c._recover_pending_unlocked()
        for k,raw in originals.items():self.assertEqual(self.s.vault.memory_path(k,"knowledge").read_text(encoding="utf-8"),raw)
        self.assertFalse(self.s.vault.compaction_journal_path.exists())

    def test_actual_process_exit_after_replacement_recovers_without_new_model(self):
        self.create()
        before=self.s.vault.memory_path("task","knowledge").read_bytes()
        code='''import os, sys, json
from unittest.mock import patch
from memleaf import Memleaf
from memleaf import compaction
s=Memleaf(sys.argv[1]); real=compaction.atomic_write_text
def crash(path,text):
    real(path,text)
    if path.parent==s.vault.root/'knowledge': os._exit(31)
with patch.object(compaction,'atomic_write_text',side_effect=crash):
    s.compact(model=lambda prompt:json.dumps({'memories':[{'source_memory_ids':['task'],'body':'Shorter body.'}]}))
'''
        env=dict(os.environ,PYTHONPATH=str(Path(__import__('memleaf').__file__).resolve().parent.parent))
        done=subprocess.run([sys.executable,"-c",code,str(self.root)],capture_output=True,env=env,timeout=20)
        self.assertEqual(done.returncode,31,done.stderr)
        self.assertTrue(self.s.vault.compaction_journal_path.exists())
        with self.s._mutation_boundary(): pass
        self.assertEqual(self.s.vault.memory_path("task","knowledge").read_bytes(),before)
        self.assertEqual(self.s.vault.list_markdown("history"),[])
        self.compact(item());self.assertEqual(self.load().status,"completed")


class BodyProtocolTests(unittest.TestCase):
    def test_empty_and_minimal_output(self):
        self.assertEqual(parse_body_compact_output(output(),["a"]),{"memories":[]})
        self.assertEqual(parse_body_compact_output(output(item("a")),["a"])["memories"],[item("a")])

    def test_invalid_references_and_containers(self):
        rows=[None,[],{},item("unknown"),{"source_memory_ids":"a","body":"b"},
              {"source_memory_ids":["a","b"],"body":"b"},
              {"source_memory_ids":[True],"body":"b"},
              {"source_memory_ids":["a"],"body":" "},
              {"source_memory_ids":["a"],"body":None},
              dict(item("a"),status="active")]
        for row in rows:
            with self.subTest(row=row),self.assertRaises(ModelOutputError):parse_body_compact_output(output(row),["a","b"])

    def test_duplicate_source_is_not_last_wins(self):
        with self.assertRaises(ModelOutputError):parse_body_compact_output(output(item("a"),item("a")),["a"])

    def test_unknown_top_level_keys_and_broken_json(self):
        for raw in ("[]", "{", '{"memories":null}', '{"memories":[],"extra":true}'):
            with self.subTest(raw=raw),self.assertRaises(ModelOutputError):parse_body_compact_output(raw,["a"])

    def test_direct_replacement_rejects_new_identity_or_retracted_source(self):
        source=Memory(memory_id="a",title="A",body="Body")
        with self.assertRaises(ModelOutputError):replacement_memory(source,item("b"),now=NOW)
        source.validity="retracted";source.body=""
        with self.assertRaises(ModelOutputError):replacement_memory(source,item("a"),now=NOW)


if __name__ == "__main__":
    unittest.main()

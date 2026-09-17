"""Synthetic native comparison contract tests; no external model or user files."""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

from incremental_test_support import IncrementalFixture
from test_incremental_execution import Backend, output
from memleaf.config import save_config
from memleaf.incremental_commit import IncrementalCommitError
from memleaf.incremental_native import read_comparison
from memleaf.incremental_preview import prepare_incremental
from memleaf.incremental_protocol import PlanningSnapshot, compile_incremental
from memleaf.native_index import _markdown_segments


class NativeFixture(IncrementalFixture):
    def configure(self, *, agent="hermes", share=False, enabled=True, text="# Atlas task\nDeliver the report."):
        path = Path(self.temp.name) / "native.md"
        path.write_text(text, encoding="utf-8")
        config = self.s.vault.config()
        config["native_sources"] = {"notes": {"path": str(path), "agent": agent, "share": share, "enabled": enabled}}
        save_config(self.s.vault.config_path, config)
        return path

    def preview(self, **kwargs):
        return self.s.preview_incremental(source="hermes", session_id="s", turn_id="t", **kwargs)

    def snapshot(self, **kwargs):
        return prepare_incremental(self.s, source="hermes", session_id="s", turn_id="t", **kwargs)

    def no_change(self, ref="m1"):
        return {"action": "NO_CHANGE", "target": ref, "evidence": ["e1"]}

    def execute(self, backend=None, **kwargs):
        return self.s.run_incremental(source="hermes", session_id="s", turn_id="t", backend=backend, **kwargs)

    def vault_bytes(self):
        return {str(p.relative_to(self.s.vault.root)): p.read_bytes()
                for p in self.s.vault.root.rglob("*") if p.is_file() and p.name != ".lock"}


class NativeReadTests(NativeFixture):
    def test_own_private_native_is_compared_without_second_injection(self):
        self.configure()
        request = self.preview(); inputs = json.loads(request["request"]["user"])
        self.assertEqual(len(inputs["memories"]), 1)
        self.assertTrue(inputs["memories"][0]["native"])
        self.assertFalse(inputs["memories"][0]["writable"])
        self.assertEqual(request["native_comparison"]["status"], "available")
        self.assertNotIn("native-", request["request"]["user"])
        self.assertNotIn(self.temp.name, request["request"]["user"])

    def test_foreign_shared_source_is_comparable(self):
        self.configure(agent="codex", share=True)
        self.assertEqual(self.preview()["native_comparison"]["selected_fragments"], 1)

    def test_foreign_private_source_is_not_opened(self):
        path = self.configure(agent="codex", share=False); path.unlink()
        with patch("memleaf.incremental_native._read_file", side_effect=AssertionError("private source read")):
            self.assertEqual(self.preview()["native_comparison"]["status"], "no_eligible_sources")

    def test_disabled_source_is_not_opened(self):
        path = self.configure(enabled=False); path.unlink()
        with patch("memleaf.incremental_native._read_file", side_effect=AssertionError("disabled source read")):
            self.assertEqual(self.preview()["native_comparison"]["selected_fragments"], 0)

    def test_missing_source_blocks_dispatch_not_local_reads(self):
        self.target(); path = self.configure(); path.unlink(); backend = Backend()
        with self.assertRaisesRegex(ValueError, "native_source_missing"):
            self.execute(backend)
        self.assertEqual(backend.calls, [])
        self.assertIsNotNone(self.s.read("mem-old"))

    def test_invalid_utf8_is_not_no_match(self):
        path = self.configure(); path.write_bytes(b"\xff")
        with self.assertRaisesRegex(ValueError, "native_source_invalid_utf8"):
            self.preview()

    def test_oversize_file_is_not_silently_truncated(self):
        self.configure()
        with patch("memleaf.incremental_native.MAX_NATIVE_BYTES", 5):
            with self.assertRaisesRegex(ValueError, "native_source_too_large"):
                self.preview()

    def test_nonregular_path_is_rejected(self):
        path = self.configure(); other = Path(self.temp.name) / "other.md"
        path.rename(other); path.symlink_to(other)
        with self.assertRaisesRegex(ValueError, "native_source_unsafe"):
            self.preview()

    def test_preview_is_read_only_even_with_corrupt_derived_index(self):
        self.configure()
        self.s.vault.native_index_path.write_text("broken index", encoding="utf-8")
        before = self.vault_bytes()
        self.preview(); self.preview()
        self.assertEqual(before, self.vault_bytes())

    def test_ids_match_existing_native_fragment_contract(self):
        path = self.configure()
        actual = read_comparison(self.s, "hermes")
        expected = _markdown_segments("notes", path.read_text())[0]["native_id"]
        self.assertEqual(list(actual.memories), [expected])

    def test_file_hash_catches_same_size_same_mtime_edit(self):
        path = self.configure(); request = self.preview(); old = path.stat()
        path.write_text(path.read_text().replace("report", "result")); os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
        with self.assertRaisesRegex(ValueError, "stale_planning_snapshot"):
            self.s.preview_incremental(source="hermes", session_id="s", turn_id="t",
                expected_snapshot=request["snapshot_id"], response=output(self.no_change(), self.no_memory()))

    def test_share_revoke_invalidates_old_response(self):
        self.configure(agent="codex", share=True)
        request = self.preview()
        config = self.s.vault.config(); config["native_sources"]["notes"]["share"] = False
        save_config(self.s.vault.config_path, config)
        with self.assertRaisesRegex(ValueError, "stale_planning_snapshot"):
            self.s.apply_incremental(source="hermes", session_id="s", turn_id="t", intent_id="n1",
                expected_snapshot=request["snapshot_id"], response=output(self.no_change(), self.no_memory()))
        self.assertEqual(self.s.vault.list_markdown("knowledge"), [])

    def test_no_match_and_empty_native_have_available_status(self):
        path = self.configure(text="# unrelated\nZebra migration distances.")
        self.assertEqual(self.preview()["native_comparison"], {"status":"available", "selected_fragments":0,
                                                              "selection":"bounded_candidates", "read_only":True})
        path.write_text("")
        self.assertEqual(self.preview()["native_comparison"]["status"], "available")

    def test_candidate_budget_pins_native_and_keeps_local_lane(self):
        self.configure(); self.target()
        for i in range(5): self.target(f"mem-extra-{i}")
        data = json.loads(self.preview(candidate_limit=2)["request"]["user"])
        self.assertEqual(len(data["memories"]), 2)
        self.assertEqual(sum(m.get("native", False) for m in data["memories"]), 1)
        native_id = next(iter(read_comparison(self.s, "hermes").memories))
        data = json.loads(self.preview(priority_memory_ids=[native_id], candidate_limit=1)["request"]["user"])
        self.assertTrue(data["memories"][0]["native"])

    def test_native_local_identity_collision_is_not_a_writable_alias(self):
        self.configure(); identity = next(iter(read_comparison(self.s, "hermes").memories))
        self.target(identity)
        with self.assertRaisesRegex(ValueError, "duplicate_memory_id"):
            self.preview()

    def test_large_required_fragment_blocks_instead_of_truncating(self):
        self.configure(text="# Atlas task\n" + "Atlas " * 24000)
        identity = next(iter(read_comparison(self.s, "hermes").memories))
        with self.assertRaisesRegex(ValueError, "blocked_context"):
            self.preview(priority_memory_ids=[identity])

    def test_snapshot_rejects_writable_native_flag(self):
        self.configure(); snapshot = self.snapshot(); state = snapshot.state()
        native = read_comparison(self.s, "hermes")
        identity = next(iter(native.memories))
        with self.assertRaisesRegex(ValueError, "native_target_must_be_read_only"):
            PlanningSnapshot.build(evidence=state["evidence"], targets={"m1":native.memories[identity]},
                native_targets={"m1":native.bindings[identity]}, native_guard=native.guard)

    def test_native_updates_are_local_errors_not_remote_writes(self):
        path = self.configure(); before = path.read_bytes()
        for fields in ({"body":"changed"}, {"validity":"retracted"}, {"scope":"unscoped"}):
            with self.subTest(fields=fields):
                result = compile_incremental(output(self.update(**fields), self.no_memory()), self.snapshot())
                self.assertIn("read_only_target", [i["code"] for i in result["issues"]])
                self.assertFalse(any(op["action"] == "UPDATE" for op in result["operations"]))
        self.assertEqual(before, path.read_bytes())

    def test_digest_protects_native_provenance_not_only_rendered_body(self):
        path = self.configure(); one = self.snapshot()
        config = self.s.vault.config(); config["native_sources"]["notes"]["agent"] = "codex"
        config["native_sources"]["notes"]["share"] = True; save_config(self.s.vault.config_path, config)
        self.assertNotEqual(one.snapshot_id, self.snapshot().snapshot_id)
        self.assertEqual(path.read_text(), "# Atlas task\nDeliver the report.")


class NativeCommitTests(NativeFixture):
    def test_one_call_native_no_change_settles_without_creating_local_fact(self):
        path = self.configure(); original = path.read_bytes(); backend = Backend(output(self.no_change(), self.no_memory()))
        result = self.execute(backend)
        self.assertEqual(result["execution_status"], "completed")
        self.assertEqual(result["commit"]["counts"]["no_change"], 1)
        self.assertTrue(result["commit"]["operations"][0]["native"])
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(self.s.vault.list_markdown("knowledge"), [])
        self.assertEqual(self.hist(), [])
        self.assertEqual(path.read_bytes(), original)
        self.assertNotIn("native_comparison_not_integrated", result["limitations"])
        payload = next(iter(self.ledger()["incremental_commits"].values()))["payload"]
        self.assertNotIn("Deliver the report.", payload)

    def test_normal_local_create_with_native_context_never_shadows(self):
        path = self.configure(); old = path.read_bytes()
        before = self.s.vault.native_index_path.read_bytes() if self.s.vault.native_index_path.exists() else None
        result = self.execute(Backend(output(self.create(), self.no_memory())))
        self.assertEqual(result["commit"]["counts"]["committed"], 1)
        self.assertEqual(path.read_bytes(), old)
        after = self.s.vault.native_index_path.read_bytes() if self.s.vault.native_index_path.exists() else None
        self.assertEqual(before, after)
        self.assertEqual(result["native_comparison"]["status"], "available")

    def test_native_update_does_not_stop_independent_local_group(self):
        path = self.configure(); self.target(); before = path.read_bytes()
        snap = self.snapshot(); native_ref = next(r for r,t in snap.state()["targets"].items() if "native" in t)
        local_ref = next(r for r,t in snap.state()["targets"].items() if "native" not in t)
        result = self.execute(Backend(output(
            {"action":"UPDATE","target":native_ref,"evidence":["e2"],"patch":{"body":"bad"}},
            {"action":"UPDATE","target":local_ref,"evidence":["e1"],"patch":{"status":"completed"}})))
        self.assertEqual(result["execution_status"], "completed_with_unresolved")
        self.assertEqual(self.s.read("mem-old").status, "completed")
        self.assertEqual(path.read_bytes(), before)

    def test_native_change_during_model_call_fences_the_response(self):
        path = self.configure()
        def change():
            path.write_text("# Atlas task\nThe obligation has changed.")
            return output(self.no_change(), self.no_memory())
        result = self.execute(Backend(change))
        self.assertEqual(result["execution_status"], "blocked")
        self.assertEqual(self.s.vault.list_markdown("knowledge"), [])
        self.assertIsNone(result["commit"])

    def test_guard_applies_even_when_changed_file_had_no_selected_fragment(self):
        path = self.configure(text="# Other\nCompletely unrelated content.")
        def change():
            path.write_text("# Atlas task\nDeliver the report.")
            return output(self.create(), self.no_memory())
        result = self.execute(Backend(change))
        self.assertEqual(result["execution_status"], "blocked")
        self.assertEqual(self.s.vault.list_markdown("knowledge"), [])

    def test_frozen_native_no_change_recovery_without_model(self):
        self.configure(); params = self.request([self.no_change(), self.no_memory()])
        import memleaf.incremental_commit as commit
        with patch.object(commit, "_resume_unlocked", side_effect=OSError("after frozen decision")):
            with self.assertRaises(OSError): self.s.apply_incremental(**params)
        work = self.works()[0]
        result = self.s.resume_incremental(work["work_id"])
        self.assertEqual(result["execution_status"], "completed")
        self.assertEqual(result["counts"]["no_change"], 1)
        self.assertEqual(result["model_calls"], 0)

    def test_native_change_after_freeze_blocks_recovery_not_other_reads(self):
        path = self.configure(); self.target()
        snap = self.snapshot(); ref = next(r for r,t in snap.state()["targets"].items() if "native" in t)
        params = self.request([self.no_change(ref), self.no_memory()])
        with patch("memleaf.incremental_commit._resume_unlocked", side_effect=OSError("frozen")):
            with self.assertRaises(OSError): self.s.apply_incremental(**params)
        path.write_text("# Atlas task\nChanged.")
        result = self.s.resume_incremental(self.works()[0]["work_id"])
        self.assertEqual(result["execution_status"], "completed_with_unresolved")
        self.assertTrue(all(op["code"] == "native_context_changed" for op in result["operations"]))
        self.assertIsNotNone(self.s.read("mem-old"))

    def test_native_added_after_old_unguarded_freeze_is_not_ignored(self):
        params = self.request([self.create(), self.no_memory()])
        with patch("memleaf.incremental_commit._resume_unlocked", side_effect=OSError("frozen")):
            with self.assertRaises(OSError): self.s.apply_incremental(**params)
        self.configure()
        final = self.s.resume_incremental(self.works()[0]["work_id"])
        self.assertEqual(final["execution_status"], "completed_with_unresolved")
        self.assertEqual(self.s.vault.list_markdown("knowledge"), [])

    def test_applied_local_write_is_not_undone_if_native_changes_during_index_recovery(self):
        path = self.configure()
        params = self.request([self.create(), self.no_memory()])
        with patch.object(self.s, "_rebuild_index_unlocked", side_effect=OSError("index")):
            with self.assertRaises(IncrementalCommitError): self.s.apply_incremental(**params)
        ids = self.s.vault.list_markdown("knowledge")
        self.assertEqual(len(ids), 1)
        path.write_text("# Atlas task\nChanged later.")
        final = self.s.resume_incremental(self.works()[0]["work_id"])
        self.assertEqual(final["execution_status"], "completed")
        self.assertEqual(self.s.vault.list_markdown("knowledge"), ids)
        self.assertEqual(len(self.hist()), 0)

    def test_completed_receipt_needs_no_native_or_model_re_read(self):
        path = self.configure()
        result = self.execute(Backend(output(self.no_change(), self.no_memory())))
        path.unlink()
        final = self.s.resume_incremental_run(result["run_id"])
        self.assertEqual(final["execution_status"], "completed")
        self.assertEqual(final["model_calls_this_invocation"], 0)
        self.assertEqual(final["native_comparison"], result["native_comparison"])


class NativeBoundaryTests(NativeFixture):
    def test_multiple_fragments_read_one_file_once_per_snapshot_pass(self):
        path = self.configure(text="\n".join(f"# Atlas {i}\nDeliver report {i}." for i in range(10)))
        import memleaf.incremental_native as native
        with patch.object(native, "_read_file", wraps=native._read_file) as reader:
            self.preview()
        self.assertEqual(reader.call_count, 1)
        self.assertEqual(reader.call_args.args[0], path)

    def test_share_revoked_after_frozen_decision_blocks_native_settlement(self):
        self.configure(agent="codex", share=True)
        params = self.request([self.no_change(), self.no_memory()])
        with patch("memleaf.incremental_commit._resume_unlocked", side_effect=OSError("frozen")):
            with self.assertRaises(OSError): self.s.apply_incremental(**params)
        config = self.s.vault.config(); config["native_sources"]["notes"]["share"] = False
        save_config(self.s.vault.config_path, config)
        final = self.s.resume_incremental(self.works()[0]["work_id"])
        self.assertEqual(final["counts"]["no_change"], 0)
        self.assertEqual(final["operations"][0]["code"], "native_context_changed")

    def test_native_guard_does_not_authorize_a_forged_local_writer_operation(self):
        self.configure()
        params = self.request([self.no_change(), self.no_memory()])
        with patch("memleaf.incremental_commit._resume_unlocked", side_effect=OSError("frozen")):
            with self.assertRaises(OSError): self.s.apply_incremental(**params)
        work = self.works()[0]; native = work["operations"][0]
        native["action"] = "UPDATE"; native["after"] = "forged"; native["replacement_revision"] = "forged"
        from memleaf.incremental_journal import save_work
        with self.s.vault.lock(), self.assertRaisesRegex(ValueError, "native_target_must_be_read_only"):
            save_work(self.s, self.ledger(), work)

    def test_native_content_binding_cannot_be_reassigned_to_other_body(self):
        self.configure(); comparison = read_comparison(self.s, "hermes")
        identity = next(iter(comparison.memories)); memory = comparison.memories[identity]
        memory.body = "unsupported replacement"
        with self.assertRaisesRegex(ValueError, "invalid_native_content"):
            PlanningSnapshot.build(evidence=self.snapshot().state()["evidence"], targets={"m1":memory},
                writable={"m1":False}, native_targets={"m1":comparison.bindings[identity]},
                native_guard=comparison.guard)

    def test_same_visible_native_body_at_new_locator_is_a_new_snapshot(self):
        path = self.configure(); old = self.snapshot()
        path.write_text("# Other\nUnrelated.\n" + path.read_text())
        self.assertNotEqual(old.snapshot_id, self.snapshot().snapshot_id)

    def test_foreign_private_edits_do_not_invalidate_unrelated_work(self):
        path = self.configure(agent="codex", share=False)
        params = self.request([self.create(), self.no_memory()])
        path.write_text("Never authorized for Hermes")
        final = self.s.apply_incremental(**params)
        self.assertEqual(final["execution_status"], "completed")
        self.assertEqual(path.read_text(), "Never authorized for Hermes")

    def test_native_is_never_counted_as_a_committed_local_memory(self):
        self.configure(); result = self.execute(Backend(output(self.no_change(), self.no_memory())))
        entry = self.ledger()["sessions"]["hermes/s"]["processed_turns"][0]
        self.assertEqual(entry["memory_ids"], [])
        self.assertEqual(result["commit"]["counts"]["committed"], 0)

    def test_pending_runtime_keeps_prior_context_from_due_cleanup(self):
        self.configure()
        self.execute(Backend(output(self.no_memory("e1"), self.no_memory())))
        self.capture("next", seq=3)
        pending = self.s.run_incremental(source="hermes", session_id="s", turn_id="next", backend=None)
        from memleaf.process_journal import ProcessJournal
        from memleaf.index import turn_key
        from memleaf.incremental_journal import protected_turn_keys, owned_turn_keys
        state = self.ledger()
        self.assertIn(turn_key("t"), protected_turn_keys(state, "hermes", "s"))
        self.assertIn(turn_key("next"), owned_turn_keys(state, "hermes", "s"))
        with self.s.vault.lock():
            cleaned = ProcessJournal(self.s)._cleanup_due_unlocked(state, "2030-01-01T00:00:00Z", 24)
        self.assertEqual(cleaned, 0)
        result = self.s.resume_incremental_run(pending["run_id"], backend=Backend(output(self.no_change(), self.no_memory())))
        self.assertEqual(result["execution_status"], "completed")
        self.assertEqual(protected_turn_keys(self.ledger(), "hermes", "s"), set())

    def test_commit_only_recovery_also_protects_context_not_just_selected_turn(self):
        self.capture("next", seq=3)
        params = self.request([self.create(), self.no_memory()], turn_id="next")
        with patch("memleaf.incremental_commit._resume_unlocked", side_effect=OSError("frozen")):
            with self.assertRaises(OSError): self.s.apply_incremental(**params)
        from memleaf.incremental_journal import protected_turn_keys, owned_turn_keys
        from memleaf.index import turn_key
        self.assertIn(turn_key("t"), protected_turn_keys(self.ledger(), "hermes", "s"))
        self.assertNotIn(turn_key("t"), owned_turn_keys(self.ledger(), "hermes", "s"))

    def test_lost_context_locator_is_conservative_not_an_empty_dependency(self):
        self.capture("next", seq=3)
        pending = self.s.run_incremental(source="hermes", session_id="s", turn_id="next")
        from memleaf.incremental_journal import protected_turn_keys
        from memleaf.index import turn_key
        state = self.ledger(); state["events"] = {}
        self.assertEqual(protected_turn_keys(state, "hermes", "s"), {turn_key("t"), turn_key("next")})

    def test_configured_facade_uses_same_native_run_and_no_change_contract(self):
        self.configure(); backend = Backend(output(self.no_change(), self.no_memory()))
        first = self.s.process_incremental(source="hermes", session_id="s", turn_id="t", model=backend)
        again = self.s.process_incremental(source="hermes", session_id="s", turn_id="t")
        self.assertEqual(first["run_id"], again["run_id"])
        self.assertEqual(first["commit"]["counts"]["no_change"], 1)
        self.assertEqual(len(backend.calls), 1)

    def test_native_response_survives_process_exit_without_redispatch(self):
        import subprocess
        import sys
        path = self.configure(); original = path.read_bytes()
        pending = self.execute()
        script = r'''
import os, sys
from memleaf import Memleaf
import memleaf.incremental_execution as execution
save = execution.save_run
def stop(service, processed, run):
    save(service, processed, run)
    if run['status'] == 'response_ready': os._exit(73)
execution.save_run = stop
class Backend:
    single_pass_safe = True
    def complete(self, *args, **kwargs): return sys.argv[3]
Memleaf(sys.argv[1]).resume_incremental_run(sys.argv[2], backend=Backend())
'''
        child = subprocess.run([sys.executable, "-c", script, str(self.s.vault.root), pending["run_id"],
                                output(self.no_change(), self.no_memory())], capture_output=True, text=True, timeout=15)
        self.assertEqual(child.returncode, 73, child.stderr)
        from memleaf import Memleaf
        final = Memleaf(self.s.vault.root).resume_incremental_run(pending["run_id"])
        self.assertEqual(final["execution_status"], "completed")
        self.assertEqual(final["model_calls_this_invocation"], 0)
        self.assertEqual(final["commit"]["counts"]["no_change"], 1)
        self.assertEqual(path.read_bytes(), original)

    def test_native_text_segmentation_reuses_existing_chunk_identity(self):
        from memleaf.native_index import _text_segments
        path = self.configure(text="\n".join("Atlas line " + str(i) for i in range(100)))
        config = self.s.vault.config(); config["native_sources"]["notes"]["format"] = "text"
        save_config(self.s.vault.config_path, config)
        actual = read_comparison(self.s, "hermes")
        expected = _text_segments("notes", path.read_text())
        self.assertEqual(set(actual.memories), {segment["native_id"] for segment in expected})
        self.assertEqual(len(actual.memories), 2)

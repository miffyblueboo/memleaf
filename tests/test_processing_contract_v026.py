"""Adversarial protocol tests: responses are deliberately NOT fixture-enriched."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from memleaf import Memleaf
from memleaf.admission import analyze_turn_evidence, admission_reason, validate_bindings, summary_evidence
from memleaf.inbox import InboxEvent, InboxTurn
from memleaf.inspection import audit_vault, preview_process, InspectionError
from memleaf.provenance import normalize_tool_evidence, observation_records, read_tool_evidence, refers_to_vault
from memleaf.turn_plan import CandidatePlan, FrozenTurn, TurnPlan, dedup_digest, input_digest, revision_digest
from memleaf.validation import ModelOutputError
from tests.test_general_evidence_admission import Backend, candidate, summary
from tests.test_hermes_provider import load_provider_module
from memleaf.index import event_key


def units_for(text, *, assistant="Noted.", tool=None):
    return analyze_turn_evidence([dict(role="user", event_key="u", content=text),
        dict(role="assistant", event_key="a", content=assistant, tool_evidence=tool or [])])


def bind(c, unit, **overrides):
    claim = dict(unit_id=unit.unit_id, quote=unit.text, role="assertion")
    claim.update(overrides)
    return [dict(candidate_id=c["candidate_id"], claims=[claim])]


class BindingContractTests(unittest.TestCase):
    def test_example_is_local_not_whole_turn(self):
        units = units_for("Orion 已经采用 PostgreSQL。请举一个连接配置的例子。")
        actual = next(u for u in units if "已经采用" in u.text)
        self.assertEqual(actual.origin, "user_assertion")
        self.assertTrue(actual.can_support)

    def test_unrelated_events_do_not_change_unit_ids(self):
        e = dict(role="user", content="Orion uses PostgreSQL.", event_key="u")
        one = analyze_turn_evidence([e])
        many = analyze_turn_evidence([dict(role="assistant",content="Unrelated",event_key="other"), e])
        self.assertEqual(one[0].unit_id, many[1].unit_id)

    def test_unicode_offsets_roundtrip(self):
        text = "🚀 Orion：数据库是 PostgreSQL。还有什么风险？\n> JDK 17"
        for unit in analyze_turn_evidence([dict(role="user", content=text, event_key="u")]):
            self.assertEqual(text[unit.start:unit.end], unit.text)

    def test_real_code_block_is_bindable_source_not_assistant_authority(self):
        units = units_for("这是已经批准的真实配置：\n```\nOrion uses PostgreSQL.\n```")
        unit = next(u for u in units if u.text == "Orion uses PostgreSQL.")
        c = candidate("c", "u", unit.text)
        c["_evidence_bindings"] = validate_bindings(bind(c, unit, role="source_excerpt"), units, [c])["c"]
        self.assertIsNone(admission_reason(c, units)[0])
        self.assertEqual(summary_evidence(c, units)[0]["content"], unit.text)

    def test_lexical_overlap_does_not_prove_changed_polarity(self):
        units = units_for("Orion does not use PostgreSQL.")
        self.assertIsNotNone(admission_reason(candidate("c", "u", "Orion uses PostgreSQL."), units)[0])

    def test_short_text_is_not_unrelated_fact_authorization(self):
        c = candidate("c", "u", "Orion runs Kubernetes.")
        c["scope_source"] = "session_context"
        self.assertIsNotNone(admission_reason(c, units_for("Okay."))[0])

    def test_bad_binding_spans_and_ids_are_rejected(self):
        units = units_for("Orion uses PostgreSQL.")
        u = units[0]; c = candidate("c", "u", u.text)
        for overrides in [dict(unit_id="foreign"), dict(quote="invented"),
                          dict(start=True,end=5), dict(start=0,end=999),
                          dict(start=0,end=2,quote="wrong"), dict(role="system")]:
            with self.subTest(overrides=overrides), self.assertRaises(ModelOutputError):
                validate_bindings(bind(c,u,**overrides),units,[c])

    def test_assistant_cannot_promote_itself_by_binding(self):
        units = units_for("What changed?",assistant="Orion uses PostgreSQL.")
        u = units[-1]; c = candidate("c", "a", u.text)
        with self.assertRaises(ModelOutputError): validate_bindings(bind(c,u),units,[c])

    def test_retrieved_memory_cannot_be_relabelled_external(self):
        units = units_for("What changed?",tool=[dict(tool_name="memleaf.read",call_id="c",
            kind="retrieved_memory",result_status="success",content="Orion uses PostgreSQL.")])
        u = units[-1]; c = candidate("c","a",u.text)
        with self.assertRaises(ModelOutputError):validate_bindings(bind(c,u,role="source_excerpt"),units,[c])

    def test_confirmation_requires_user_not_tool(self):
        units = units_for("What changed?",tool=[dict(tool_name="files.read",call_id="c",
            kind="external_observation",result_status="success",content="Orion uses PostgreSQL.")])
        u=units[-1];c=candidate("c","a",u.text)
        with self.assertRaises(ModelOutputError):validate_bindings(bind(c,u,role="user_confirmation"),units,[c])

    def test_duplicate_quote_requires_explicit_offsets(self):
        units = units_for("JDK 17 and JDK 17")
        c=candidate("c","u","JDK 17")
        with self.assertRaises(ModelOutputError):validate_bindings(bind(c,units[0],quote="JDK 17"),units,[c])
        rows=validate_bindings(bind(c,units[0],quote="JDK 17",start=0,end=6),units,[c])
        self.assertEqual(rows["c"][0]["end"],6)

    def test_summary_contains_only_bound_claims(self):
        units=units_for("Orion uses PostgreSQL. Atlas uses MySQL.",assistant="Invented owner Alice.")
        u=units[0];c=candidate("c","u",u.text)
        c["_evidence_bindings"]=validate_bindings(bind(c,u),units,[c])["c"]
        projected=json.dumps(summary_evidence(c,units))
        self.assertNotIn("Alice",projected)
        self.assertNotIn("Atlas",projected)

    def test_explicit_binding_cannot_claim_another_coverage_unit(self):
        from memleaf.admission import validate_coverage_bindings
        units=units_for("Orion uses PostgreSQL. Atlas uses MySQL.")
        c=candidate("c","u",units[0].text)
        c["_evidence_bindings"]=validate_bindings(bind(c,units[0]),units,[c])["c"]
        rows={u.unit_id:dict(decision="CANDIDATE",candidate_ids=["c"]) for u in units[:2]}
        with self.assertRaises(ModelOutputError):validate_coverage_bindings(rows,units,[c])

    def test_legacy_exact_support_cannot_contradict_no_change(self):
        from memleaf.admission import validate_coverage_bindings
        units=units_for("Orion uses PostgreSQL.")
        c=candidate("c","u",units[0].text)
        rows={units[0].unit_id:dict(decision="NO_CHANGE",reason="query_only")}
        with self.assertRaises(ModelOutputError):validate_coverage_bindings(rows,units,[c])


class FrozenPlanTests(unittest.TestCase):
    def setUp(self):
        e=InboxEvent("hermes","s","a"*64,1,"user","b"*64,"Orion uses PostgreSQL.")
        a=InboxEvent("hermes","s","a"*64,1,"assistant","c"*64,"Noted.")
        self.turn=InboxTurn("hermes","s","a"*64,1,(e,a))
        self.req=dict(turn=self.turn,candidate_id="c",memory_id="mem-one",evidence_unit_ids=["unit"],
            summary=summary("b"*64,"Orion uses PostgreSQL."))

    def test_freezes_full_payload_not_only_digest(self):
        frozen=FrozenTurn.build(self.turn,[self.req])
        self.req["summary"]["body"]="MUTATED"
        one=FrozenTurn.restore(frozen.to_dict(),self.turn)
        self.assertEqual(one["requests"][0]["summary"]["body"],"Orion uses PostgreSQL.")
        one["requests"][0]["summary"]["tags"].append("changed")
        two=FrozenTurn.restore(frozen.to_dict(),self.turn)
        self.assertNotIn("changed",two["requests"][0]["summary"]["tags"])

    def test_corrupt_payload_does_not_replay(self):
        stored=FrozenTurn.build(self.turn,[self.req]).to_dict();stored["payload"]+=" "
        with self.assertRaises(ModelOutputError):FrozenTurn.restore(stored,self.turn)

    def test_changed_input_or_foreign_turn_rejected(self):
        stored=FrozenTurn.build(self.turn,[self.req]).to_dict()
        for other in [replace(self.turn,session_id="other"),
                      replace(self.turn,events=(replace(self.turn.events[0],content="changed"),self.turn.events[1]))]:
            with self.subTest(other=other),self.assertRaises(ModelOutputError):FrozenTurn.restore(stored,other)

    def test_model_candidate_rename_does_not_change_update_operation(self):
        self.req["summary"]["update_memory_id"]="mem-target"
        one=TurnPlan.from_requests([self.req]).candidates[0].operation_id
        self.req["candidate_id"]="a-new-model-name"
        two=TurnPlan.from_requests([self.req]).candidates[0].operation_id
        self.assertEqual(one,two)

    def test_candidate_id_is_not_a_filesystem_path(self):
        self.req["candidate_id"]="gate/part-1"
        stored=FrozenTurn.build(self.turn,[self.req]).to_dict()
        self.assertEqual(FrozenTurn.restore(stored,self.turn)["requests"][0]["candidate_id"],"gate/part-1")

    def test_memory_id_cannot_escape_vault(self):
        self.req["memory_id"]="../foreign"
        stored=FrozenTurn.build(self.turn,[self.req]).to_dict()
        with self.assertRaises(ModelOutputError):FrozenTurn.restore(stored,self.turn)

    def test_one_mutation_per_target(self):
        self.req["summary"]["update_memory_id"]="mem-target"
        other=deepcopy({k:v for k,v in self.req.items() if k!="turn"});other["turn"]=self.turn
        other["candidate_id"]="second";other["summary"]["body"]="Other fact."
        with self.assertRaises(ModelOutputError):TurnPlan.from_requests([self.req,other])

    def test_automatic_write_requires_admitted_evidence(self):
        self.req["evidence_unit_ids"]=[]
        with self.assertRaises(ModelOutputError):TurnPlan.from_requests([self.req])

    def test_scope_retirement_is_journalled(self):
        self.req["scope_correction"]=dict(target_memory_id="wrong",survivor_memory_id="right")
        op=TurnPlan.from_requests([self.req]).candidates[0].to_dict()
        self.assertEqual(op["kind"],"scope_retirement")
        self.assertEqual(op["disposition"],"UPDATE")

    def test_explicit_duplicate_is_metadata_update_not_create(self):
        self.req.update(duplicate_memory_id="existing",explicit_remember=True)
        op=TurnPlan.from_requests([self.req]).candidates[0].to_dict()
        self.assertEqual(op["kind"],"metadata_merge")
        self.assertEqual(op["disposition"],"UPDATE")

    def test_independent_titles_not_deduplicated(self):
        a=self.req["summary"];b={**a,"title":"Another named task"}
        self.assertNotEqual(dedup_digest(a),dedup_digest(b))

    def test_revision_ignores_hits_but_not_sources(self):
        a={**self.req["summary"],"hit_count":1,"last_hit_at":"a"}
        self.assertEqual(revision_digest(a),revision_digest({**a,"hit_count":9,"last_hit_at":"b"}))
        self.assertNotEqual(revision_digest(a),revision_digest({**a,"sources":[]}))

    def test_empty_turn_decisions_can_be_saved(self):
        frozen=FrozenTurn.build(self.turn,[],evidence=[dict(unit_id="unit",decision="NO_CHANGE")])
        restored=FrozenTurn.restore(frozen.to_dict(),self.turn)
        self.assertEqual(restored["requests"],[])
        self.assertEqual(len(restored["evidence_dispositions"]),1)


class ProvenanceContractTests(unittest.TestCase):
    def test_structured_content_not_duplicated_by_text_mirror(self):
        rows=observation_records("tool.read","c",dict(structuredContent={"fact":"Orion uses JDK 17"},
            content=[dict(type="text",text="Unrelated text mirror")]))
        self.assertEqual(len(rows),1);self.assertNotIn("mirror",rows[0]["content"])

    def test_large_collection_keeps_complete_records_and_omission_count(self):
        payload={"project":"Orion","records":[{"record_id":str(i),"body":"x"*250} for i in range(20)]}
        rows=observation_records("tool.read","c",payload)
        self.assertEqual(len(rows),8)
        self.assertEqual(rows[-1]["omitted_count"],"13")
        self.assertTrue(all(row["completeness"]=="complete" for row in rows[:-1]))
        self.assertTrue(all("Orion" in row["content"] for row in rows[:-1]))

    def test_oversized_prose_is_never_complete(self):
        row=observation_records("files.read","c","x"*5000)[0]
        self.assertEqual(row["execution_status"],"success")
        self.assertEqual(row["completeness"],"partial")
        self.assertEqual(row["result_status"],"truncated")

    def test_error_result_has_no_external_content_authority(self):
        rows=observation_records("tool.read","c",dict(isError=True,content="Could not query the record"))
        units=units_for("What changed?",tool=rows)
        self.assertFalse(any(u.origin=="external_observation" for u in units))

    def test_digest_recomputed_after_redaction(self):
        row=normalize_tool_evidence([dict(tool_name="a",call_id="c",kind="external_observation",
            content="api_key=secret-value-test",result_digest="forged")])[0]
        self.assertNotIn("secret-value-test",row["content"])
        self.assertNotEqual(row["result_digest"],"forged")

    def test_malformed_record_is_visible_but_untrusted(self):
        rows=read_tool_evidence([{"content":42}])
        self.assertEqual(rows[0]["completeness"],"missing")
        self.assertEqual(rows[0]["kind"],"unknown")

    def test_reader_accounts_all_overflow_records(self):
        rows=read_tool_evidence([dict(tool_name="a",call_id=str(i),content=f"fact {i}") for i in range(30)])
        self.assertEqual(rows[-1]["omitted_count"],"23")

    def test_invalid_omission_counter_is_rejected(self):
        with self.assertRaises(ValueError):normalize_tool_evidence([dict(omitted_count="not-a-number")])

    def test_direct_vault_paths_not_shell_substrings(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(refers_to_vault({"path":str(Path(tmp)/"knowledge"/"a.md")},tmp))
            self.assertFalse(refers_to_vault({"command":f"cat {tmp}/knowledge/a.md"},tmp))
            self.assertFalse(refers_to_vault({"path":tmp+"-other/a.md"},tmp))
        self.assertTrue(refers_to_vault({"file":"f:/MEMLEAF/vault/knowledge/a.md"},"F:\\memleaf\\vault"))

    def test_explicit_readback_identity_overrides_tool_name(self):
        rows=observation_records("files.read","c","Old memory",source_kind="retrieved_memory")
        self.assertEqual(rows[0]["kind"],"retrieved_memory")


class InspectionContractTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/"vault";self.core=Memleaf(self.path)

    def snapshot(self):
        return {p.relative_to(self.path).as_posix():p.read_bytes() for p in self.path.rglob("*") if p.is_file()}

    def test_missing_vault_is_not_created(self):
        target=Path(self.temp.name)/"missing"
        with self.assertRaises(InspectionError):audit_vault(target)
        self.assertFalse(target.exists())

    def test_audit_reports_exact_duplicates_without_writes(self):
        for mid in ["a","b"]:
            self.core.create_memory(memory_id=mid,title="Orion",body="Orion uses PostgreSQL.",type="fact",scopes=["project:Orion"])
        before=self.snapshot();result=audit_vault(self.path)
        self.assertEqual(self.snapshot(),before)
        self.assertTrue(any(i["kind"]=="identical_active_payloads" for i in result["issues"]))
        self.assertEqual(result["producing_version"],"not_inferred")

    def test_preview_runs_same_pipeline_on_copy_only(self):
        self.core.capture("hermes","s","t","user","Orion uses PostgreSQL.",event_id="u")
        self.core.capture("hermes","s","t","assistant","Noted.",event_id="a")
        c=candidate("c",event_key("u"),"Orion uses PostgreSQL.")
        backend=Backend([c],{"c":summary(event_key("u"),c["memory"])})
        before=self.snapshot();result=preview_process(self.path,model=backend)
        self.assertEqual(self.snapshot(),before)
        self.assertEqual(result["result"]["memories_written"],1)
        self.assertTrue(result["source_unchanged"])
        self.assertFalse(result["apply_supported"])
        self.assertTrue(result["changes"])

    def test_query_does_not_trigger_compaction_writes(self):
        from memleaf.processing import Processor
        self.core.capture("hermes","s","t","user","List current tasks.",event_id="u")
        self.core.capture("hermes","s","t","assistant","No new facts.",event_id="a")
        with patch.object(Processor,"_auto_compact",side_effect=AssertionError("query attempted maintenance")):
            result=self.core.process(model=Backend([]))
        self.assertEqual(result["memories_written"],0)
        self.assertEqual(result["compaction"]["reason"],"no_memory_changes")

    def test_audit_refuses_symlinked_children(self):
        other=Path(self.temp.name)/"other.md";other.write_text("x")
        try:(self.path/"knowledge"/"linked.md").symlink_to(other)
        except (OSError,NotImplementedError):self.skipTest("symlink creation not available")
        with self.assertRaises(InspectionError):audit_vault(self.path)

    def test_concurrent_snapshot_change_is_reported(self):
        from memleaf import inspection
        original=inspection._snapshot;n=0
        def changes(root):
            nonlocal n
            n+=1;state=original(root)
            if n>1:state["knowledge/concurrent.md"]=b"changed"
            return state
        with patch.object(inspection,"_snapshot",changes),self.assertRaises(InspectionError):audit_vault(self.path)


if __name__=="__main__":unittest.main()

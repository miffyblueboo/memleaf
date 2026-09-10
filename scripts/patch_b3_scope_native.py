from __future__ import annotations

from pathlib import Path

ROOT = Path.cwd()

# --- Single-pass schema: native shadows, scope operations, explicit scope correction. ---
plan_path = ROOT / "src/memleaf/single_pass_plan.py"
text = plan_path.read_text(encoding="utf-8")
old_fields = '''_MEMORY_FIELDS = frozenset({
    "title", "body", "tags", "aliases", "keywords", "status", "completed_at", "due_date"
})'''
new_fields = '''_MEMORY_FIELDS = frozenset({
    "title", "body", "tags", "aliases", "keywords", "status", "completed_at", "due_date",
    "shadow_native_ids", "scope_operations",
})'''
if text.count(old_fields) != 1:
    raise SystemExit("B3 memory field anchor missing")
text = text.replace(old_fields, new_fields, 1)

old_content = '''CONTENT
CREATE supplies type, scopes, scope_source and memory. UPDATE supplies target_memory_id and memory; Core inherits immutable type/scopes from the target. memory contains title, body, tags and only optional aliases, keywords, status, completed_at, due_date. Do not emit type/scopes/sources/update_memory_id inside memory; Core supplies deterministic metadata and source references after validating evidence. Omission from current evidence is not retraction or completion. For an UPDATE, preserve still-valid target content unless current evidence supersedes it.
'''
new_content = '''CONTENT
CREATE supplies type, scopes, scope_source and memory. UPDATE normally supplies target_memory_id and memory; Core inherits immutable type/scopes from the target. Only when CURRENT_EVIDENCE explicitly corrects a prior project/Scope attribution may UPDATE additionally supply scopes and scope_source together; Core must independently authorize that correction or reject it. memory contains title and body plus only optional tags, aliases, keywords, status, completed_at, due_date, shadow_native_ids and scope_operations. shadow_native_ids may copy only supplied NATIVE_MEMORY_CATALOG IDs when current evidence supersedes that native content; use no_memory reason native_already_covered when unchanged native content already covers the fact. scope_operations are allowed only under the existing memleaf Scope contract. Do not emit type/scopes/sources/update_memory_id inside memory; Core supplies deterministic metadata and source references after validating evidence. Omission from current evidence is not retraction or completion. For an UPDATE, preserve still-valid target content unless current evidence supersedes it.
'''
if text.count(old_content) != 1:
    raise SystemExit("B3 CONTENT system anchor missing")
text = text.replace(old_content, new_content, 1)

old_decisions = '''    decision_fields = {
        "CREATE": common | {"type", "scopes", "scope_source", "memory"},
        "UPDATE": common | {"target_memory_id", "memory"},
        "NO_CHANGE": common | {"target_memory_id"},
        "DEFERRED": common | {"reason"},
    }
'''
new_decisions = '''    decision_fields = {
        "CREATE": common | {"type", "scopes", "scope_source", "memory"},
        "UPDATE": common | {"target_memory_id", "memory"},
        "NO_CHANGE": common | {"target_memory_id"},
        "DEFERRED": common | {"reason"},
    }
    update_scope_fields = frozenset({"scopes", "scope_source"})
'''
if text.count(old_decisions) != 1:
    raise SystemExit("B3 decision fields anchor missing")
text = text.replace(old_decisions, new_decisions, 1)

old_shape = '''        if set(raw_item) != decision_fields[decision]:
            detail = "unknown_fields" if set(raw_item) - decision_fields[decision] else "missing_fields"
            raise ModelOutputError("B3 item fields do not match decision", validation_detail=detail)
        if decision in {"CREATE", "UPDATE", "NO_CHANGE"} and not lookup_complete:
'''
new_shape = '''        actual_fields = set(raw_item)
        allowed_fields = decision_fields[decision]
        if decision == "UPDATE":
            extra_scope_fields = actual_fields & update_scope_fields
            if extra_scope_fields and extra_scope_fields != update_scope_fields:
                raise ModelOutputError(
                    "B3 UPDATE scope correction requires scopes and scope_source together",
                    validation_detail="missing_fields",
                )
            allowed_fields = allowed_fields | update_scope_fields
        required_fields = decision_fields[decision]
        if not required_fields.issubset(actual_fields) or actual_fields - allowed_fields:
            detail = "unknown_fields" if actual_fields - allowed_fields else "missing_fields"
            raise ModelOutputError("B3 item fields do not match decision", validation_detail=detail)
        if decision in {"CREATE", "UPDATE", "NO_CHANGE"} and not lookup_complete:
'''
if text.count(old_shape) != 1:
    raise SystemExit("B3 item shape anchor missing")
text = text.replace(old_shape, new_shape, 1)

old_update = '''        elif decision in {"UPDATE", "NO_CHANGE"}:
            canonical, target_record = _canonical_target(item.get("target_memory_id"), local_by_key)
            target_key = canonical.casefold()
            if target_key in used_targets:
                raise ModelOutputError("B3 target referenced more than once", validation_detail="duplicate_update_target")
            used_targets.add(target_key)
            item["target_memory_id"] = canonical
            if decision == "UPDATE":
                item["memory"] = _memory_object(item.get("memory"))
'''
new_update = '''        elif decision in {"UPDATE", "NO_CHANGE"}:
            canonical, target_record = _canonical_target(item.get("target_memory_id"), local_by_key)
            target_key = canonical.casefold()
            if target_key in used_targets:
                raise ModelOutputError("B3 target referenced more than once", validation_detail="duplicate_update_target")
            used_targets.add(target_key)
            item["target_memory_id"] = canonical
            if decision == "UPDATE":
                item["memory"] = _memory_object(item.get("memory"))
                if "scopes" in item:
                    scopes = item.get("scopes")
                    if not isinstance(scopes, list) or not scopes or not all(
                        isinstance(scope, str) and scope for scope in scopes
                    ):
                        raise ModelOutputError("B3 UPDATE scopes are invalid", validation_detail="invalid_scope")
                    if item.get("scope_source") not in SCOPE_SOURCES:
                        raise ModelOutputError(
                            "B3 UPDATE scope_source is invalid",
                            validation_detail="invalid_scope_source",
                        )
'''
if text.count(old_update) != 1:
    raise SystemExit("B3 update normalization anchor missing")
text = text.replace(old_update, new_update, 1)
plan_path.write_text(text, encoding="utf-8")

# --- PlanningContext: prefetch both explicitly named project scopes before the single call. ---
ctx_path = ROOT / "src/memleaf/planning_context.py"
ctx = ctx_path.read_text(encoding="utf-8")
insert_at = ctx.index("\n\n    def _single_pass_related(")
helper = r'''

    def _single_pass_scope_correction_context(
        self,
        turn: InboxTurn,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Return bounded local context for an explicit two-project correction.

        This is only a retrieval expansion.  It does not decide which project is
        old/new or which memory is the target; the single model call proposes
        that decision and ``_scope_correction_plan`` remains the final Core
        authorization boundary.
        """

        user_text = " ".join(
            event.content for event in turn.events
            if event.role == "user" and isinstance(event.content, str)
        ).strip()
        if not user_text or not _SCOPE_CORRECTION_MARKER_RE.search(user_text):
            return [], True
        try:
            with self.service.vault.lock():
                config = self.service.vault.config()
                scopes = config.get("scopes", {}) if isinstance(config, Mapping) else {}
                if not isinstance(scopes, Mapping):
                    return [], False
                mentioned = [
                    scope for scope in scopes
                    if isinstance(scope, str)
                    and scope.startswith("project:")
                    and self._scope_terms_present(user_text, scope, config)
                ]
                mentioned = list(dict.fromkeys(mentioned))
                if len(mentioned) != 2:
                    return [], True
                records = self.service._read_memories_unlocked("knowledge")
                values = [
                    record.memory.to_dict()
                    for record in records
                    if any(filter_by_scope([record.memory], [scope], config) for scope in mentioned)
                ]
        except (OSError, UnicodeError, ValueError, TypeError):
            return [], False
        return self._bound_related_with_status(values)
'''
ctx = ctx[:insert_at] + helper + ctx[insert_at:]
old_result = '''        related, scope_background, native_refs, scope_fallback, bound_complete = self._related_query(
            turn,
            state,
            query,
            explicit_scope,
            overlay=overlay,
            strict_relevance=not scoped_reply_context,
            native_query=visible,
            return_bound_status=True,
        )
        fallback_ambiguous = bool(
'''
new_result = '''        related, scope_background, native_refs, scope_fallback, bound_complete = self._related_query(
            turn,
            state,
            query,
            explicit_scope,
            overlay=overlay,
            strict_relevance=not scoped_reply_context,
            native_query=visible,
            return_bound_status=True,
        )
        correction_rows, correction_complete = self._single_pass_scope_correction_context(turn)
        if correction_rows:
            existing_ids = {
                item.get("memory_id").casefold()
                for item in related
                if isinstance(item, Mapping)
                and isinstance(item.get("memory_id"), str)
                and item.get("native") is not True
            }
            combined = list(related)
            combined.extend(
                row for row in correction_rows
                if isinstance(row.get("memory_id"), str)
                and row["memory_id"].casefold() not in existing_ids
            )
            related, combined_complete = self._bound_related_with_status(combined)
            bound_complete = bool(bound_complete and correction_complete and combined_complete)
        elif not correction_complete:
            bound_complete = False
        fallback_ambiguous = bool(
'''
if ctx.count(old_result) != 1:
    raise SystemExit("B3 single-pass retrieval result anchor missing")
ctx = ctx.replace(old_result, new_result, 1)
ctx_path.write_text(ctx, encoding="utf-8")

# --- Automatic planner: authorize scope correction and pass native/scope operation fields through existing parser. ---
planner_path = ROOT / "src/memleaf/single_pass_memory_planner.py"
planner = planner_path.read_text(encoding="utf-8")
old_revisions = '''        target_revisions: dict[str, str] = {}
        for item in local_related:
'''
new_revisions = '''        target_revisions: dict[str, str] = {}
        scope_correction_plans: dict[str, dict[str, Any]] = {}
        for item in local_related:
'''
if planner.count(old_revisions) != 1:
    raise SystemExit("B3 revisions anchor missing")
planner = planner.replace(old_revisions, new_revisions, 1)

old_target_scope = '''            if decision == "UPDATE":
                if target_memory is None:
                    raise ModelOutputError("B3 update target disappeared", validation_detail="invalid_update_target")
                memory_type = target_memory.type
                scopes = list(target_memory.scopes)
                scope_source = target_memory.scope_source
            else:
                memory_type = decision_context.get("type")
                scopes = list(decision_context.get("scopes", []))
                scope_source = decision_context.get("scope_source")
'''
new_target_scope = '''            if decision == "UPDATE":
                if target_memory is None:
                    raise ModelOutputError("B3 update target disappeared", validation_detail="invalid_update_target")
                memory_type = target_memory.type
                if "scopes" in decision_context:
                    scopes = list(decision_context.get("scopes", []))
                    scope_source = decision_context.get("scope_source")
                else:
                    scopes = list(target_memory.scopes)
                    scope_source = target_memory.scope_source
            else:
                memory_type = decision_context.get("type")
                scopes = list(decision_context.get("scopes", []))
                scope_source = decision_context.get("scope_source")
'''
if planner.count(old_target_scope) != 1:
    raise SystemExit("B3 target scope anchor missing")
planner = planner.replace(old_target_scope, new_target_scope, 1)

old_grounding = '''            if decision == "CREATE" and not _model_project_scope_is_source_grounded(
                candidate,
                planning_units,
                validation_scope_registry,
                authorized_project_scopes,
            ):
                raise ModelOutputError(
                    "B3 project scope is not grounded by claimed source",
                    validation_detail="scope_not_grounded",
                )

            admitted_events = summary_evidence(candidate, planning_units, events=events)
'''
new_grounding = '''            if decision in {"CREATE", "UPDATE"} and not _model_project_scope_is_source_grounded(
                candidate,
                planning_units,
                validation_scope_registry,
                authorized_project_scopes,
            ):
                raise ModelOutputError(
                    "B3 project scope is not grounded by claimed source",
                    validation_detail="scope_not_grounded",
                )
            if (
                decision == "UPDATE"
                and target_memory is not None
                and [scope.casefold() for scope in scopes]
                    != [scope.casefold() for scope in target_memory.scopes]
            ):
                correction_plan = self.inputs._scope_correction_plan(
                    candidate,
                    turn,
                    self.service.vault.config(),
                )
                if (
                    not isinstance(correction_plan, Mapping)
                    or correction_plan.get("ambiguous")
                    or correction_plan.get("unresolved")
                    or not isinstance(correction_plan.get("target_memory_id"), str)
                    or correction_plan["target_memory_id"].casefold() != target_memory.memory_id.casefold()
                    or not isinstance(correction_plan.get("new_scope"), str)
                    or correction_plan["new_scope"].casefold() not in {
                        scope.casefold() for scope in scopes if isinstance(scope, str)
                    }
                ):
                    raise ModelOutputError(
                        "B3 cross-scope UPDATE is not authorized by explicit correction evidence",
                        validation_detail="scope_drift",
                    )
                scope_correction_plans[candidate_id] = dict(correction_plan)

            admitted_events = summary_evidence(candidate, planning_units, events=events)
'''
if planner.count(old_grounding) != 1:
    raise SystemExit("B3 grounding anchor missing")
planner = planner.replace(old_grounding, new_grounding, 1)

old_request = '''                request = self._request(
                    summary,
                    turn,
                    candidate_id=candidate_id,
                    conversation_title=title,
                    native_refs=native_refs,
                )
                request["evidence_unit_ids"] = unit_ids
                target = summary.get("update_memory_id")
'''
new_request = '''                correction_plan = scope_correction_plans.get(candidate_id)
                if (
                    isinstance(correction_plan, Mapping)
                    and isinstance(correction_plan.get("survivor_memory_id"), str)
                    and correction_plan.get("survivor_memory_id")
                ):
                    request = self.inputs._scope_correction_request(
                        candidate,
                        turn,
                        correction_plan,
                        conversation_title=title,
                        native_refs=native_refs,
                    )
                    request["evidence_unit_ids"] = unit_ids
                    requests.append(request)
                    survivor = self.inputs._active_memory_by_id(correction_plan["survivor_memory_id"])
                    self.audit._record_disposition(
                        turn_ref,
                        candidate,
                        "UPDATE",
                        memory_id=survivor.memory_id if survivor is not None else correction_plan["survivor_memory_id"],
                    )
                    if survivor is not None:
                        for value in survivor.scopes:
                            if value not in observed_scopes:
                                observed_scopes.append(value)
                    continue
                request = self._request(
                    summary,
                    turn,
                    candidate_id=candidate_id,
                    conversation_title=title,
                    native_refs=native_refs,
                )
                request["evidence_unit_ids"] = unit_ids
                target = summary.get("update_memory_id")
'''
if planner.count(old_request) != 1:
    raise SystemExit("B3 request anchor missing")
planner = planner.replace(old_request, new_request, 1)
planner_path.write_text(planner, encoding="utf-8")

# --- Tests ---
test_path = ROOT / "tests/test_b3_single_pass_plan.py"
test = test_path.read_text(encoding="utf-8")
insert = '''    def test_evidence_coverage_must_be_complete_and_disjoint(self):
'''
addition = '''    def test_update_scope_correction_fields_must_be_paired(self):
        evidence = [unit("u1", "Move Alpha memory to Beta.")]
        for extra in ({"scopes": ["project:Beta"]}, {"scope_source": "model"}):
            raw = json.dumps({
                "protocol_version": PROTOCOL_VERSION,
                "items": [{
                    "candidate_id": "c1",
                    "decision": "UPDATE",
                    "target_memory_id": "m1",
                    "evidence": [claim("u1", "Move Alpha memory to Beta.")],
                    "memory": memory(),
                    **extra,
                }],
                "no_memory": [],
            })
            with self.assertRaises(ModelOutputError):
                parse_single_pass_output(
                    raw,
                    evidence_units=evidence,
                    local_memories=[local("m1")],
                    lookup_complete=True,
                    validate_memory=validator,
                )

    def test_memory_allows_native_shadow_and_scope_operations(self):
        evidence = [unit("u1", "New current state.")]
        raw = json.dumps({
            "protocol_version": PROTOCOL_VERSION,
            "items": [{
                "candidate_id": "c1",
                "decision": "CREATE",
                "type": "fact",
                "scopes": ["global"],
                "scope_source": "model",
                "evidence": [claim("u1", "New current state.")],
                "memory": {
                    "title": "Current state",
                    "body": "New current state.",
                    "shadow_native_ids": ["native-1"],
                    "scope_operations": [],
                },
            }],
            "no_memory": [],
        })
        result = parse_single_pass_output(
            raw,
            evidence_units=evidence,
            local_memories=[],
            lookup_complete=True,
            validate_memory=validator,
        )
        self.assertEqual(result["items"][0]["memory"]["shadow_native_ids"], ["native-1"])
        self.assertEqual(result["items"][0]["memory"]["scope_operations"], [])

'''
if insert not in test:
    raise SystemExit("B3 protocol test insertion anchor missing")
test = test.replace(insert, addition + insert, 1)
test_path.write_text(test, encoding="utf-8")

ctx_test_path = ROOT / "tests/test_b3_planning_context.py"
ctx_test = ctx_test_path.read_text(encoding="utf-8")
# Only unit-test the no-marker fast path here; existing v0.2.3 tests exercise the real correction scanner.
insert_ctx = '''    def test_single_pass_create_requires_complete_unambiguous_context(self):
'''
addition_ctx = '''    def test_scope_correction_prefetch_is_noop_without_explicit_marker(self):
        turn = SimpleNamespace(events=[SimpleNamespace(role="user", content="Alpha ordinary update")])
        context = object.__new__(PlanningContext)
        rows, complete = context._single_pass_scope_correction_context(turn)
        self.assertEqual(rows, [])
        self.assertTrue(complete)

'''
if insert_ctx not in ctx_test:
    raise SystemExit("B3 context test insertion anchor missing")
ctx_test = ctx_test.replace(insert_ctx, addition_ctx + insert_ctx, 1)
ctx_test_path.write_text(ctx_test, encoding="utf-8")

planner_test_path = ROOT / "tests/test_b3_single_pass_memory_planner.py"
ptest = planner_test_path.read_text(encoding="utf-8")
# Extend fake Inputs with deterministic scope-correction support for planner integration.
old_fake = '''    def _active_memory_by_id(self, memory_id):
        if self.target is not None and isinstance(memory_id, str) and memory_id.casefold() == self.target.memory_id.casefold():
            return self.target
        return None
'''
new_fake = '''    def _active_memory_by_id(self, memory_id):
        if self.target is not None and isinstance(memory_id, str) and memory_id.casefold() == self.target.memory_id.casefold():
            return self.target
        return None
    def _scope_correction_plan(self, candidate, turn, config):
        if self.target is None or candidate.get("update_memory_id") != self.target.memory_id:
            return None
        scopes = candidate.get("scopes", [])
        if scopes == list(self.target.scopes):
            return None
        return {
            "target_memory_id": self.target.memory_id,
            "old_scope": self.target.scopes[0],
            "new_scope": scopes[0],
            "survivor_memory_id": None,
            "ambiguous": False,
            "unresolved": False,
        }
'''
if ptest.count(old_fake) != 1:
    raise SystemExit("B3 fake inputs anchor missing")
ptest = ptest.replace(old_fake, new_fake, 1)

insert_ptest = '''    def test_no_change_and_query_make_no_requests(self):
'''
addition_ptest = '''    def test_update_may_apply_core_authorized_scope_correction_in_same_call(self):
        target = active("m-old", "双人复核", scope="project:Old", memory_type="project")
        related = [target.to_dict()]
        def response(prompt):
            payload = json.loads(prompt.split("B3_INPUT\\n",1)[1].split("\\nReturn",1)[0])
            assistant_uid = payload["current_evidence"][1]["unit_id"]
            return {
                "protocol_version": PROTOCOL_VERSION,
                "items": [{
                    "candidate_id": "c1",
                    "decision": "UPDATE",
                    "target_memory_id": "m-old",
                    "scopes": ["project:New"],
                    "scope_source": "model",
                    "evidence": [item_claim(prompt, "New流程要求仍是双人复核，之前归错到Old。")],
                    "memory": {"title": "流程", "body": "New流程要求仍是双人复核。"},
                }],
                "no_memory": [{"unit_id": assistant_uid, "reason": "assistant_restatement"}],
            }
        planner, _, model = self.planner(response, related=related, target=target)
        requests, scopes = planner._collect_turn_outputs(
            "backend",
            turn("New流程要求仍是双人复核，之前归错到Old。"),
            {},
            scope=["project:New"],
        )
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(requests[0]["summary"]["scopes"], ["project:New"])
        self.assertEqual(requests[0]["summary"]["update_memory_id"], "m-old")
        self.assertIn("project:New", scopes)

    def test_create_passes_native_shadow_and_scope_operations_through_core_parser(self):
        native_id = "native-1"
        related = [{
            "native": True,
            "native_id": native_id,
            "native_source_id": "source-1",
            "source": "native",
            "content": "legacy state",
            "title": "Legacy",
            "body": "legacy state",
            "scopes": ["global"],
        }]
        def response(prompt):
            payload = json.loads(prompt.split("B3_INPUT\\n",1)[1].split("\\nReturn",1)[0])
            assistant_uid = payload["current_evidence"][1]["unit_id"]
            return {
                "protocol_version": PROTOCOL_VERSION,
                "items": [{
                    "candidate_id": "c1",
                    "decision": "CREATE",
                    "type": "fact",
                    "scopes": ["global"],
                    "scope_source": "model",
                    "evidence": [item_claim(prompt, "legacy state is replaced")],
                    "memory": {
                        "title": "Current state",
                        "body": "legacy state is replaced",
                        "shadow_native_ids": [native_id],
                        "scope_operations": [],
                    },
                }],
                "no_memory": [{"unit_id": assistant_uid, "reason": "assistant_restatement"}],
            }
        planner, _, model = self.planner(response, related=related)
        requests, _ = planner._collect_turn_outputs("backend", turn("legacy state is replaced"), {})
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(requests[0]["summary"]["shadow_native_ids"], [native_id])
        self.assertEqual(requests[0]["summary"]["scope_operations"], [])
        self.assertEqual(requests[0]["native_refs"], [{"source_id": "source-1", "native_id": native_id}])

'''
if insert_ptest not in ptest:
    raise SystemExit("B3 planner test insertion anchor missing")
ptest = ptest.replace(insert_ptest, addition_ptest + insert_ptest, 1)
planner_test_path.write_text(ptest, encoding="utf-8")

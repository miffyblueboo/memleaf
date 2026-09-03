from __future__ import annotations

from pathlib import Path

path = Path("src/memleaf/processing.py")
text = path.read_text(encoding="utf-8")
start = text.index("    def _scope_correction_plan(\n")
end = text.index("    def _scope_correction_request(\n", start)
replacement = '''    def _scope_correction_plan(
        self,
        candidate: Mapping[str, Any],
        turn: InboxTurn,
        config: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Authorize one explicit cross-project correction without guessing.

        The current user turn must name exactly two configured project scopes
        under explicit correction wording.  A model-provided target is checked
        against that evidence; when it is omitted, Core may recover exactly one
        same-type, same-topic active memory from the explicitly named old
        scope.  Zero or multiple matches stay deferred rather than becoming a
        cross-scope CREATE.
        """

        if candidate.get("worth") is not True or not isinstance(candidate.get("type"), str):
            return None
        new_projects = [
            value for value in candidate.get("scopes", [])
            if isinstance(value, str) and value.startswith("project:")
        ]
        if len(new_projects) != 1:
            return None
        new_scope = new_projects[0]
        user_text = " ".join(
            event.content for event in turn.events
            if event.role == "user" and isinstance(event.content, str)
        ).strip()
        if not user_text or not _SCOPE_CORRECTION_MARKER_RE.search(user_text):
            return None
        scopes = config.get("scopes", {}) if isinstance(config.get("scopes", {}), Mapping) else {}
        mentioned = [
            scope for scope in scopes
            if isinstance(scope, str)
            and scope.startswith("project:")
            and self._scope_terms_present(user_text, scope, config)
        ]
        mentioned = list(dict.fromkeys(mentioned))
        if len(mentioned) != 2 or all(scope.casefold() != new_scope.casefold() for scope in mentioned):
            return None
        old_scope = next(scope for scope in mentioned if scope.casefold() != new_scope.casefold())

        topic = str(candidate.get("memory") or "")
        removable_terms: list[str] = []
        for scope in (old_scope, new_scope):
            removable_terms.extend((scope, scope.partition(":")[2]))
            node = scopes.get(scope)
            if isinstance(node, Mapping) and isinstance(node.get("aliases"), list):
                removable_terms.extend(item for item in node["aliases"] if isinstance(item, str))
        for term in sorted({item for item in removable_terms if item}, key=len, reverse=True):
            topic = re.sub(re.escape(term), " ", topic, flags=re.IGNORECASE)
        topic = re.sub(r"[\\s:：，,；;。.!！?？()（）\\[\\]【】_-]+", " ", topic).strip()
        if len(normalize_term(topic)) < 4:
            return {
                "target_memory_id": None,
                "old_scope": old_scope,
                "new_scope": new_scope,
                "survivor_memory_id": None,
                "ambiguous": True,
                "unresolved": True,
            }

        try:
            with self.service.vault.lock():
                records = self.service._read_memories_unlocked("knowledge")
        except (OSError, UnicodeError, ValueError, TypeError):
            return None
        eligible_old: list[Memory] = []
        eligible_new: list[Memory] = []
        for record in records:
            memory = record.memory
            if memory.type != candidate.get("type"):
                continue
            if filter_by_scope([record], [old_scope], config) and candidate_matches_query(memory, topic):
                eligible_old.append(memory)
            if filter_by_scope([record], [new_scope], config) and candidate_matches_query(memory, topic):
                eligible_new.append(memory)

        target_id = candidate.get("update_memory_id")
        target: Memory | None = None
        if isinstance(target_id, str) and target_id:
            selected = self._active_memory_by_id(target_id)
            if (
                selected is not None
                and selected.type == candidate.get("type")
                and any(memory.memory_id.casefold() == selected.memory_id.casefold() for memory in eligible_old)
            ):
                target = selected
            else:
                return {
                    "target_memory_id": None,
                    "old_scope": old_scope,
                    "new_scope": new_scope,
                    "survivor_memory_id": None,
                    "ambiguous": True,
                    "unresolved": True,
                }
        elif len(eligible_old) == 1:
            target = eligible_old[0]
        else:
            return {
                "target_memory_id": None,
                "old_scope": old_scope,
                "new_scope": new_scope,
                "survivor_memory_id": None,
                "ambiguous": True,
                "unresolved": True,
            }

        survivors = [
            memory for memory in eligible_new
            if memory.memory_id.casefold() != target.memory_id.casefold()
        ]
        return {
            "target_memory_id": target.memory_id,
            "old_scope": old_scope,
            "new_scope": new_scope,
            "survivor_memory_id": survivors[0].memory_id if len(survivors) == 1 else None,
            "ambiguous": len(survivors) > 1,
            "unresolved": False,
        }

'''
path.write_text(text[:start] + replacement + text[end:], encoding="utf-8")

# Avoid writing a None update target into a parsed candidate; unresolved
# correction plans are retained only to force candidate-local defer.
path = Path("src/memleaf/processing.py")
text = path.read_text(encoding="utf-8")
old = '''                if plan is not None:
                    item.pop("duplicate_memory_id", None)
                    item["duplicate"] = False
                    item["update_memory_id"] = plan["target_memory_id"]
                    scope_correction_plans[str(item["candidate_id"]).casefold()] = plan
'''
new = '''                if plan is not None:
                    item.pop("duplicate_memory_id", None)
                    item["duplicate"] = False
                    if isinstance(plan.get("target_memory_id"), str):
                        item["update_memory_id"] = plan["target_memory_id"]
                    else:
                        item.pop("update_memory_id", None)
                    scope_correction_plans[str(item["candidate_id"]).casefold()] = plan
'''
if text.count(old) != 1:
    raise SystemExit(f"processing correction staging block occurrences={text.count(old)}")
path.write_text(text.replace(old, new, 1), encoding="utf-8")

# Distinguish unresolved from multi-survivor ambiguity in audit diagnostics.
path = Path("src/memleaf/processing.py")
text = path.read_text(encoding="utf-8")
old = '''            if correction_plan is not None and correction_plan.get("ambiguous"):
                self._defer_candidate(
                    turn_ref,
                    candidate,
                    "scope_correction_ambiguous",
                    scopes=candidate.get("scopes"),
                )
                continue
'''
new = '''            if correction_plan is not None and correction_plan.get("ambiguous"):
                self._defer_candidate(
                    turn_ref,
                    candidate,
                    (
                        "scope_correction_unresolved"
                        if correction_plan.get("unresolved")
                        else "scope_correction_ambiguous"
                    ),
                    scopes=candidate.get("scopes"),
                )
                continue
'''
if text.count(old) != 1:
    raise SystemExit(f"processing defer block occurrences={text.count(old)}")
path.write_text(text.replace(old, new, 1), encoding="utf-8")

# Extend the regression: the model may omit update_memory_id and Core must
# still recover exactly one explicitly named old-scope target.
path = Path("tests/test_v023_scope_correction.py")
text = path.read_text(encoding="utf-8")
marker = '''    def test_existing_correct_survivor_retires_wrong_active_to_history(self) -> None:
'''
insert = '''    def test_explicit_scope_correction_recovers_unique_old_target_when_model_omits_id(self) -> None:
        old = self._memory("mem-targetless", "project:兴银理财", "流程要求使用双人复核")
        turn = self._turn("之前归错客户了，不是兴银理财，是鑫元基金；流程要求仍是使用双人复核。")
        candidate = {
            "candidate_id": "targetless", "memory": "鑫元基金流程要求使用双人复核", "worth": True,
            "duplicate": False, "type": "project", "scopes": ["project:鑫元基金"],
            "scope_source": "model", "evidence_event_ids": list(turn.event_keys),
        }
        plan = Processor(self.service)._scope_correction_plan(candidate, turn, self.service.vault.config())
        self.assertIsNotNone(plan)
        self.assertEqual(old.memory_id, plan["target_memory_id"])
        self.assertFalse(plan["ambiguous"])
        self.assertFalse(plan["unresolved"])

        self._memory("mem-targetless-2", "project:兴银理财", "流程要求使用双人复核并留痕")
        ambiguous = Processor(self.service)._scope_correction_plan(candidate, turn, self.service.vault.config())
        self.assertIsNotNone(ambiguous)
        self.assertTrue(ambiguous["ambiguous"])
        self.assertTrue(ambiguous["unresolved"])
        self.assertIsNone(ambiguous["target_memory_id"])

'''
if text.count(marker) != 1:
    raise SystemExit("test insertion marker invalid")
path.write_text(text.replace(marker, insert + marker, 1), encoding="utf-8")
print("targetless scope correction patch applied")

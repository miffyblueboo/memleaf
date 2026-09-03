from __future__ import annotations

from pathlib import Path
import re


def read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def write(path: str, text: str) -> None:
    Path(path).write_text(text, encoding="utf-8")


def replace_once(path: str, old: str, new: str) -> None:
    text = read(path)
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected one occurrence, found {count}: {old[:120]!r}")
    write(path, text.replace(old, new, 1))


def insert_before(path: str, marker: str, content: str) -> None:
    text = read(path)
    if content.strip() in text:
        return
    if text.count(marker) != 1:
        raise SystemExit(f"{path}: insertion marker count != 1: {marker[:120]!r}")
    write(path, text.replace(marker, content + marker, 1))


# ---------------------------------------------------------------------------
# Scope identifiers: bounded private evidence mapping, never Scope Map output.
# ---------------------------------------------------------------------------
replace_once(
    "src/memleaf/scope_state.py",
    '_NODE_FIELDS = frozenset(("aliases", "paths", "parent", "children"))\n',
    '_NODE_FIELDS = frozenset(("aliases", "paths", "identifiers", "parent", "children"))\n'
    '_DOMAIN_IDENTIFIER = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\\.)+[a-z]{2,63}$", re.IGNORECASE)\n',
)
insert_before(
    "src/memleaf/scope_state.py",
    "\ndef _validate_node(scope: str, raw: Any) -> dict[str, Any]:\n",
    '''\n\ndef _domain_identifier_list(value: Any) -> list[str]:
    values = _string_list(value, "identifiers")
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        domain = item.strip().casefold().lstrip("@")
        if not _DOMAIN_IDENTIFIER.fullmatch(domain):
            raise ScopeError("invalid scope identifiers")
        if domain not in seen:
            seen.add(domain)
            result.append(domain)
    return result
''',
)
replace_once(
    "src/memleaf/scope_state.py",
    '    paths = _string_list(node["paths"], "paths") if "paths" in node else []\n',
    '    paths = _string_list(node["paths"], "paths") if "paths" in node else []\n'
    '    identifiers = _domain_identifier_list(node["identifiers"]) if "identifiers" in node else []\n',
)
replace_once(
    "src/memleaf/scope_state.py",
    '    if "paths" in node:\n        node["paths"] = paths\n',
    '    if "paths" in node:\n        node["paths"] = paths\n'
    '    if "identifiers" in node:\n        node["identifiers"] = identifiers\n',
)
insert_before(
    "src/memleaf/scope_state.py",
    "\ndef _resolved_path(value: str | Path, *, base_dir: Path | None = None) -> Path:\n",
    '''\n\ndef project_scopes_for_domains(domains: Iterable[str], config: Mapping[str, Any]) -> list[str]:
    """Resolve bounded mail-domain evidence to configured project scopes.

    Identifiers are private configuration metadata. They are intentionally not
    projected into the Scope Map or model prompts.
    """

    registry = validate_scope_registry(config.get("scopes", {}) if isinstance(config, Mapping) else {})
    normalized: set[str] = set()
    for raw in domains:
        if not isinstance(raw, str):
            continue
        value = raw.strip().casefold().lstrip("@")
        if _DOMAIN_IDENTIFIER.fullmatch(value):
            normalized.add(value)
    matches: set[str] = set()
    for scope, node in registry.items():
        if not scope.startswith("project:"):
            continue
        identifiers = node.get("identifiers", [])
        if not isinstance(identifiers, list):
            continue
        for identifier in identifiers:
            if not isinstance(identifier, str):
                continue
            key = identifier.casefold()
            if any(domain == key or domain.endswith("." + key) for domain in normalized):
                matches.add(scope)
                break
    return sorted(matches, key=str.casefold)
''',
)
replace_once(
    "src/memleaf/scope_state.py",
    '    "project_scope_matches_text",\n',
    '    "project_scope_matches_text",\n    "project_scopes_for_domains",\n',
)

# ---------------------------------------------------------------------------
# Capture only bounded host-sanitized mail metadata in event metadata.
# ---------------------------------------------------------------------------
replace_once(
    "src/memleaf/capture.py",
    "from typing import Optional\n",
    "from typing import Any, Mapping, Optional\n",
)
insert_before(
    "src/memleaf/capture.py",
    "\ndef _event_id(source: str, session_id: str, turn_id: str, role: str, event_id: Optional[str]) -> str:\n",
    '''\n\n_MAIL_EVIDENCE_FIELDS = frozenset({"message_id", "subject", "sender", "domain"})
_MAIL_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\\.)+[a-z]{2,63}$", re.IGNORECASE)
_MAX_MAIL_EVIDENCE_ITEMS = 8
_MAX_MAIL_EVIDENCE_TEXT = 320


def _normalize_tool_evidence(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError("tool evidence must be a list")
    result: list[dict[str, str]] = []
    seen: set[tuple[tuple[str, str], ...]] = set()
    for raw in list(value)[:_MAX_MAIL_EVIDENCE_ITEMS]:
        if not isinstance(raw, Mapping) or set(raw) - _MAIL_EVIDENCE_FIELDS:
            raise ValueError("invalid tool evidence record")
        item: dict[str, str] = {}
        for key in _MAIL_EVIDENCE_FIELDS:
            field = raw.get(key)
            if field is None:
                continue
            if not isinstance(field, str) or not field.strip() or any(ch in field for ch in "\\x00\\r\\n"):
                raise ValueError("invalid tool evidence field")
            text = redact_text(field.strip())[:_MAX_MAIL_EVIDENCE_TEXT]
            if key == "domain":
                text = text.casefold().lstrip("@")
                if not _MAIL_DOMAIN_RE.fullmatch(text):
                    raise ValueError("invalid tool evidence domain")
            item[key] = text
        if not item:
            continue
        fingerprint = tuple(sorted(item.items()))
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        result.append(item)
    return result
''',
)
replace_once(
    "src/memleaf/capture.py",
    "    turn_index: int,\n) -> str:\n",
    "    turn_index: int,\n    tool_evidence: list[dict[str, str]] | None = None,\n) -> str:\n",
)
replace_once(
    "src/memleaf/capture.py",
    '        "timestamp": timestamp,\n    }\n',
    '        "timestamp": timestamp,\n    }\n    if tool_evidence:\n        metadata["tool_evidence"] = [dict(item) for item in tool_evidence]\n',
)
replace_once(
    "src/memleaf/capture.py",
    "    visible: bool = True,\n) -> CaptureResult:\n",
    "    visible: bool = True,\n    tool_evidence: Any = None,\n) -> CaptureResult:\n",
)
replace_once(
    "src/memleaf/capture.py",
    "    safe_content = escape_event_markers(redact_text(content))\n",
    "    safe_content = escape_event_markers(redact_text(content))\n    safe_tool_evidence = _normalize_tool_evidence(tool_evidence)\n",
)
replace_once(
    "src/memleaf/capture.py",
    "            turn_index,\n        )\n",
    "            turn_index,\n            safe_tool_evidence,\n        )\n",
)

# Inbox parser carries the bounded metadata without making it business text.
replace_once(
    "src/memleaf/inbox.py",
    "from typing import Any, Iterable, Optional\n",
    "from typing import Any, Iterable, Mapping, Optional\n",
)
replace_once(
    "src/memleaf/inbox.py",
    "    timestamp: Optional[str] = None\n    legacy: bool = False\n",
    "    timestamp: Optional[str] = None\n    tool_evidence: tuple[dict[str, str], ...] = ()\n    legacy: bool = False\n",
)
insert_before(
    "src/memleaf/inbox.py",
    "\ndef parse_inbox_text(\n",
    '''\n\ndef _bounded_tool_evidence(value: Any) -> tuple[dict[str, str], ...]:
    if not isinstance(value, list):
        return ()
    allowed = {"message_id", "subject", "sender", "domain"}
    result: list[dict[str, str]] = []
    for raw in value[:8]:
        if not isinstance(raw, Mapping) or set(raw) - allowed:
            continue
        item = {
            str(key): str(field)
            for key, field in raw.items()
            if key in allowed
            and isinstance(field, str)
            and field
            and "\\x00" not in field
            and "\\r" not in field
            and "\\n" not in field
            and len(field) <= 320
        }
        if item:
            result.append(item)
    return tuple(result)
''',
)
# Both malformed-groupable and valid event constructors preserve only bounded evidence.
replace_once(
    "src/memleaf/inbox.py",
    '                    timestamp=metadata.get("timestamp") if isinstance(metadata.get("timestamp"), str) else None,\n                    legacy=not groupable,\n',
    '                    timestamp=metadata.get("timestamp") if isinstance(metadata.get("timestamp"), str) else None,\n                    tool_evidence=_bounded_tool_evidence(metadata.get("tool_evidence")),\n                    legacy=not groupable,\n',
)
replace_once(
    "src/memleaf/inbox.py",
    '                timestamp=metadata.get("timestamp") if isinstance(metadata.get("timestamp"), str) else None,\n            )\n',
    '                timestamp=metadata.get("timestamp") if isinstance(metadata.get("timestamp"), str) else None,\n                tool_evidence=_bounded_tool_evidence(metadata.get("tool_evidence")),\n            )\n',
)

# Public Python capture accepts host-sanitized evidence, but ordinary callers remain unchanged.
replace_once(
    "src/memleaf/service.py",
    "        visible: bool = True,\n    ) -> CaptureResult:\n",
    "        visible: bool = True,\n        tool_evidence: Any = None,\n    ) -> CaptureResult:\n",
)
replace_once(
    "src/memleaf/service.py",
    "            visible=visible,\n        )\n",
    "            visible=visible,\n            tool_evidence=tool_evidence,\n        )\n",
)

# MCP capture schema accepts only the four bounded metadata fields; no new tool is added.
replace_once(
    "src/memleaf/mcp_server.py",
    '                "visible": {"type": "boolean"},\n',
    '                "visible": {"type": "boolean"},\n'
    '                "tool_evidence": {\n'
    '                    "type": "array",\n'
    '                    "items": _object_schema(\n'
    '                        {\n'
    '                            "message_id": {"type": "string"},\n'
    '                            "subject": {"type": "string"},\n'
    '                            "sender": {"type": "string"},\n'
    '                            "domain": {"type": "string"},\n'
    '                        }\n'
    '                    ),\n'
    '                },\n',
)

# ---------------------------------------------------------------------------
# Safe same-project mixed future-use splitter.
# ---------------------------------------------------------------------------
insert_before(
    "src/memleaf/validation.py",
    "\ndef is_aggregate_operational_text(value: Any) -> bool:\n",
    '''\n\ndef split_mixed_future_use_text(value: Any) -> list[tuple[str, str]] | None:
    """Safely split one same-scope durable rule/plan plus unfinished action.

    Every clause must classify exactly once. Ambiguous or unclassified text
    returns ``None`` so the existing deferred-candidate safety boundary stays
    intact.
    """

    if not isinstance(value, str):
        return None
    clauses = _split_future_use_clauses(value)
    if len(clauses) < 2:
        return None
    result: list[tuple[str, str]] = []
    durable_count = 0
    todo_count = 0
    for clause in clauses:
        folded = clause.casefold()
        durable = is_project_plan_text(clause) or any(marker in folded for marker in _PROJECT_RULE_MARKERS)
        todo = (
            (bool(_CALENDAR_TEXT.search(clause)) and any(marker in folded for marker in _DATED_TODO_MARKERS))
            or is_actionable_todo_text(clause)
        ) and not is_project_plan_text(clause)
        if durable == todo:
            return None
        if todo:
            result.append((clause, "todo"))
            todo_count += 1
        else:
            result.append((clause, "project" if is_project_plan_text(clause) else "fact"))
            durable_count += 1
    if not durable_count or not todo_count:
        return None
    return result
''',
)
# Export helper if validation defines __all__; harmless if absent.
validation = read("src/memleaf/validation.py")
if '"split_mixed_future_use_text"' not in validation and "__all__ = [" in validation:
    validation = validation.replace("__all__ = [", '__all__ = [\n    "split_mixed_future_use_text",', 1)
    write("src/memleaf/validation.py", validation)

# ---------------------------------------------------------------------------
# Processor: evidence conflicts, strict scope correction transaction, mixed split.
# ---------------------------------------------------------------------------
replace_once(
    "src/memleaf/processing.py",
    "    normalize_scopes,\n    register_scope_nodes,\n",
    "    normalize_scopes,\n    project_scopes_for_domains,\n    register_scope_nodes,\n",
)
replace_once(
    "src/memleaf/processing.py",
    "    is_project_plan_text,\n)\n",
    "    is_project_plan_text,\n    split_mixed_future_use_text,\n)\n",
)
insert_before(
    "src/memleaf/processing.py",
    "\ndef _completion_match_is_declarative(folded: str, match: re.Match[str]) -> bool:\n",
    '''\n\n_SCOPE_CORRECTION_MARKER_RE = re.compile(
    r"(?:不是|并非|不属于|归错|归属错误|错误归属|应属于|应该属于|改归|改为|纠正为|"
    r"wrong\\s+(?:project|scope)|belongs?\\s+to|correct\\s+(?:project|scope))",
    re.IGNORECASE,
)
''',
)
# Insert Processor methods immediately before _target_relation.
insert_before(
    "src/memleaf/processing.py",
    "    def _target_relation(\n",
    '''    def _turn_evidence_project_scope(
        self,
        turn: InboxTurn,
        config: Mapping[str, Any],
    ) -> str | None:
        domains: list[str] = []
        for event in turn.events:
            for item in getattr(event, "tool_evidence", ()):
                if isinstance(item, Mapping) and isinstance(item.get("domain"), str):
                    domains.append(item["domain"])
        matches = project_scopes_for_domains(domains, config)
        return matches[0] if len(matches) == 1 else None

    def _scope_evidence_conflict(
        self,
        candidate: Mapping[str, Any],
        turn: InboxTurn,
        config: Mapping[str, Any],
    ) -> bool:
        evidence_scope = self._turn_evidence_project_scope(turn, config)
        if evidence_scope is None or candidate.get("worth") is not True:
            return False
        selected = [
            value
            for value in candidate.get("scopes", [])
            if isinstance(value, str) and value.startswith("project:")
        ]
        return bool(selected) and all(value.casefold() != evidence_scope.casefold() for value in selected)

    @staticmethod
    def _scope_terms_present(text: str, scope: str, config: Mapping[str, Any]) -> bool:
        terms = [scope, scope.partition(":")[2]]
        node = config.get("scopes", {}).get(scope) if isinstance(config.get("scopes", {}), Mapping) else None
        if isinstance(node, Mapping) and isinstance(node.get("aliases"), list):
            terms.extend(item for item in node["aliases"] if isinstance(item, str))
        folded = text.casefold()
        return any(term.casefold() in folded for term in terms if term)

    def _scope_correction_plan(
        self,
        candidate: Mapping[str, Any],
        turn: InboxTurn,
        config: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Authorize exactly one explicit cross-project correction transaction."""

        target_id = candidate.get("update_memory_id")
        if candidate.get("worth") is not True or not isinstance(target_id, str) or not target_id:
            return None
        target = self._active_memory_by_id(target_id)
        if target is None or target.type != candidate.get("type"):
            return None
        new_projects = [
            value for value in candidate.get("scopes", [])
            if isinstance(value, str) and value.startswith("project:")
        ]
        old_projects = [value for value in target.scopes if isinstance(value, str) and value.startswith("project:")]
        if len(new_projects) != 1 or len(old_projects) != 1:
            return None
        old_scope, new_scope = old_projects[0], new_projects[0]
        if old_scope.casefold() == new_scope.casefold():
            return None
        user_text = " ".join(
            event.content for event in turn.events
            if event.role == "user" and isinstance(event.content, str)
        ).strip()
        if not user_text or not _SCOPE_CORRECTION_MARKER_RE.search(user_text):
            return None
        if not self._scope_terms_present(user_text, old_scope, config):
            return None
        if not self._scope_terms_present(user_text, new_scope, config):
            return None

        survivor_ids: list[str] = []
        try:
            with self.service.vault.lock():
                records = self.service._read_memories_unlocked("knowledge")
        except (OSError, UnicodeError, ValueError, TypeError):
            return None
        for record in records:
            memory = record.memory
            if memory.memory_id.casefold() == target.memory_id.casefold() or memory.type != target.type:
                continue
            if not filter_by_scope([record], [new_scope], config):
                continue
            if candidate_matches_query(memory, str(candidate.get("memory", ""))):
                survivor_ids.append(memory.memory_id)
        survivor_ids = list(dict.fromkeys(survivor_ids))
        return {
            "target_memory_id": target.memory_id,
            "old_scope": old_scope,
            "new_scope": new_scope,
            "survivor_memory_id": survivor_ids[0] if len(survivor_ids) == 1 else None,
            "ambiguous": len(survivor_ids) > 1,
        }

    def _scope_correction_request(
        self,
        candidate: Mapping[str, Any],
        turn: InboxTurn,
        plan: Mapping[str, Any],
        *,
        conversation_title: str,
        native_refs: Iterable[Mapping[str, Any]],
    ) -> dict[str, Any]:
        survivor_id = plan.get("survivor_memory_id")
        survivor = self._active_memory_by_id(survivor_id)
        if survivor is None:
            raise ProcessingError("scope correction survivor disappeared")
        summary = {
            "title": survivor.title,
            "body": survivor.body,
            "tags": list(survivor.tags),
            "type": survivor.type,
            "scopes": list(survivor.scopes),
            "scope_source": survivor.scope_source,
            "aliases": list(survivor.aliases),
            "keywords": list(survivor.keywords),
            "sources": [],
            "scope_operations": [],
            "status": survivor.status,
            "completed_at": survivor.completed_at,
            "due_date": survivor.due_date,
        }
        return {
            "summary": summary,
            "turn": turn,
            "candidate_id": str(candidate["candidate_id"]),
            "memory_id": survivor.memory_id,
            "event_key": turn.event_keys[0] if turn.event_keys else "",
            "turn_id": "",
            "conversation_title": conversation_title,
            "explicit_remember": False,
            "native_refs": [dict(item) for item in native_refs if isinstance(item, Mapping)],
            "scope_correction": dict(plan),
        }

''',
)
# Add state dictionary.
replace_once(
    "src/memleaf/processing.py",
    "        candidate_level_target_ids: set[str] = set()\n",
    "        candidate_level_target_ids: set[str] = set()\n        scope_correction_plans: dict[str, dict[str, Any]] = {}\n",
)
replace_once(
    "src/memleaf/processing.py",
    "            candidate_level_target_ids.clear()\n",
    "            candidate_level_target_ids.clear()\n            scope_correction_plans.clear()\n",
)
# Before target validation, mark evidence conflicts and authorize explicit corrections.
insert_before(
    "src/memleaf/processing.py",
    "            invalid_targets: dict[str, set[str]] = {}\n",
    '''            prepared_candidates: list[dict[str, Any]] = []
            for candidate in parsed["candidates"]:
                item = dict(candidate)
                if self._scope_evidence_conflict(item, turn, validation_scope_registry):
                    item["_defer_reason"] = "scope_conflict"
                plan = self._scope_correction_plan(item, turn, validation_scope_registry)
                if plan is not None:
                    item.pop("duplicate_memory_id", None)
                    item["duplicate"] = False
                    item["update_memory_id"] = plan["target_memory_id"]
                    scope_correction_plans[str(item["candidate_id"]).casefold()] = plan
                prepared_candidates.append(item)
            parsed = dict(parsed)
            parsed["candidates"] = prepared_candidates

''',
)
# Target relation: explicit correction bypasses ordinary cross-scope relation only.
replace_once(
    "src/memleaf/processing.py",
    "                relation = self._target_relation(\n                    candidate,\n                    turn=turn,\n                    scope_directory=scope_directory,\n                    scope_directory_complete=scope_directory_complete,\n                )\n",
    "                correction = scope_correction_plans.get(candidate_id)\n"
    "                relation = (\n"
    "                    _TARGET_SAME_USE\n"
    "                    if correction is not None and not correction.get(\"ambiguous\")\n"
    "                    else self._target_relation(\n"
    "                        candidate,\n"
    "                        turn=turn,\n"
    "                        scope_directory=scope_directory,\n"
    "                        scope_directory_complete=scope_directory_complete,\n"
    "                    )\n"
    "                )\n",
)
# Do not detach authorized corrections on final retry.
replace_once(
    "src/memleaf/processing.py",
    "                    fields = invalid_targets.get(candidate[\"candidate_id\"].casefold())\n                    mismatch = candidate[\"candidate_id\"].casefold() in type_mismatches\n",
    "                    candidate_key = candidate[\"candidate_id\"].casefold()\n                    fields = invalid_targets.get(candidate_key)\n                    mismatch = candidate_key in type_mismatches\n                    if candidate_key in scope_correction_plans:\n                        fields = None\n                        mismatch = False\n",
)
# Replace final mixed defer block with safe same-project split first.
old_mixed = '''            if gate_attempt_count >= 3:
                # Mixed future-use is still a hard safety boundary.  After
                # the bounded correction attempts, retain only the affected
                # candidate for deferred retry so valid siblings can commit;
                # no mixed body can reach summarize or persistence.
                marked_candidates: list[dict[str, Any]] = []
                for candidate in parsed["candidates"]:
                    item = dict(candidate)
                    if item.get("worth") and is_mixed_future_use_text(item.get("memory")):
                        item["_defer_reason"] = "mixed_future_use"
                    marked_candidates.append(item)
                parsed = dict(parsed)
                parsed["candidates"] = marked_candidates
'''
new_mixed = '''            if gate_attempt_count >= 3:
                marked_candidates: list[dict[str, Any]] = []
                for candidate in parsed["candidates"]:
                    item = dict(candidate)
                    if item.get("worth") and is_mixed_future_use_text(item.get("memory")):
                        split = split_mixed_future_use_text(item.get("memory"))
                        if split is None:
                            item["_defer_reason"] = "mixed_future_use"
                            marked_candidates.append(item)
                            continue
                        base_id = str(item.get("candidate_id", "candidate"))
                        for index, (fragment, fragment_type) in enumerate(split, start=1):
                            child = dict(item)
                            digest = hashlib.sha256(fragment.encode("utf-8")).hexdigest()[:8]
                            child["candidate_id"] = f"{base_id}:future-{index}-{digest}"
                            child["memory"] = fragment
                            child["type"] = fragment_type
                            child["duplicate"] = False
                            child.pop("duplicate_memory_id", None)
                            child.pop("update_memory_id", None)
                            child.pop("_defer_reason", None)
                            marked_candidates.append(child)
                        continue
                    marked_candidates.append(item)
                parsed = dict(parsed)
                parsed["candidates"] = marked_candidates
'''
replace_once("src/memleaf/processing.py", old_mixed, new_mixed)

# Candidate loop obtains correction and handles all defer reasons safely.
replace_once(
    "src/memleaf/processing.py",
    "            recovery = recovery_by_candidate.get(candidate_id_key)\n            detached_update_target_id = detached_update_target_ids.get(candidate_id_key)\n            defer_reason = candidate.get(\"_defer_reason\")\n            if isinstance(defer_reason, str) and defer_reason == \"mixed_future_use\":\n",
    "            recovery = recovery_by_candidate.get(candidate_id_key)\n            correction_plan = scope_correction_plans.get(candidate_id_key)\n            detached_update_target_id = detached_update_target_ids.get(candidate_id_key)\n            defer_reason = candidate.get(\"_defer_reason\")\n            if isinstance(defer_reason, str) and defer_reason in {\"mixed_future_use\", \"scope_conflict\"}:\n",
)
# Ambiguous explicit correction must defer, never detach/create.
insert_before(
    "src/memleaf/processing.py",
    "            if candidate.get(\"worth\") and (\n",
    '''            if correction_plan is not None and correction_plan.get("ambiguous"):
                self._defer_candidate(
                    turn_ref,
                    candidate,
                    "scope_correction_ambiguous",
                    scopes=candidate.get("scopes"),
                )
                continue
''',
)
# Scope-directory/ambiguity guards must not block an explicitly authorized old target.
replace_once(
    "src/memleaf/processing.py",
    "                and detached_update_target_id is None\n                and (\n",
    "                and detached_update_target_id is None\n                and correction_plan is None\n                and (\n",
)
replace_once(
    "src/memleaf/processing.py",
    "                and detached_update_target_id is None\n            ):\n                self._defer_candidate(\n                    turn_ref,\n                    candidate,\n                    \"related_ambiguous\",\n",
    "                and detached_update_target_id is None\n                and correction_plan is None\n            ):\n                self._defer_candidate(\n                    turn_ref,\n                    candidate,\n                    \"related_ambiguous\",\n",
)
# Ensure old target/survivor are in candidate_related and do not let inference replace correction target.
insert_before(
    "src/memleaf/processing.py",
    "            if recovery is not None:\n",
    '''            if correction_plan is not None:
                priority_ids = [
                    correction_plan.get("target_memory_id"),
                    correction_plan.get("survivor_memory_id"),
                ]
                for priority_id in reversed(priority_ids):
                    memory = self._active_memory_by_id(priority_id)
                    if memory is None:
                        continue
                    if not any(
                        isinstance(item, Mapping)
                        and isinstance(item.get("memory_id"), str)
                        and item["memory_id"].casefold() == memory.memory_id.casefold()
                        for item in candidate_related
                    ):
                        candidate_related.insert(0, memory.to_dict())
''',
)
replace_once(
    "src/memleaf/processing.py",
    "            if not force_create:\n                candidate = self._infer_update_target(candidate, candidate_related)\n",
    "            if not force_create and correction_plan is None:\n                candidate = self._infer_update_target(candidate, candidate_related)\n",
)
# Second relation check must honor correction transaction.
replace_once(
    "src/memleaf/processing.py",
    "                relation = self._target_relation(\n                    candidate,\n                    turn=turn,\n                    scope_directory=(\n                        scope_directory if detached_update_target_id is None else None\n                    ),\n                    scope_directory_complete=scope_directory_complete,\n                )\n",
    "                relation = (\n"
    "                    _TARGET_SAME_USE\n"
    "                    if correction_plan is not None and not correction_plan.get(\"ambiguous\")\n"
    "                    else self._target_relation(\n"
    "                        candidate,\n"
    "                        turn=turn,\n"
    "                        scope_directory=(\n"
    "                            scope_directory if detached_update_target_id is None else None\n"
    "                        ),\n"
    "                        scope_directory_complete=scope_directory_complete,\n"
    "                    )\n"
    "                )\n",
)
# If a correct active survivor already exists, retire wrong target without rewriting survivor.
insert_before(
    "src/memleaf/processing.py",
    "            candidate_native_ids = [item[\"native_id\"] for item in candidate_native_refs]\n",
    '''            if correction_plan is not None and correction_plan.get("survivor_memory_id"):
                requests.append(
                    self._scope_correction_request(
                        candidate,
                        turn,
                        correction_plan,
                        conversation_title=title,
                        native_refs=candidate_native_refs,
                    )
                )
                for observed_scope in candidate.get("scopes", []):
                    if isinstance(observed_scope, str) and observed_scope != "unscoped" and observed_scope not in observed_scopes:
                        observed_scopes.append(observed_scope)
                continue
''',
)
# Mark normal in-place correction request so writer labels history.
replace_once(
    "src/memleaf/processing.py",
    "            current_turn_request_ids.add(pending_request[\"memory_id\"].casefold())\n",
    "            if correction_plan is not None:\n                pending_request[\"scope_correction\"] = dict(correction_plan)\n            current_turn_request_ids.add(pending_request[\"memory_id\"].casefold())\n",
)

# ---------------------------------------------------------------------------
# Writer: archive invalidated wrong active and optionally retire to survivor.
# ---------------------------------------------------------------------------
replace_once(
    "src/memleaf/memory_writer.py",
    "    def _write_history(self, old: Memory, *, superseded_by: str, archived_at: str) -> str:\n",
    "    def _write_history(\n        self,\n        old: Memory,\n        *,\n        superseded_by: str,\n        archived_at: str,\n        invalidated_reason: str | None = None,\n    ) -> str:\n",
)
replace_once(
    "src/memleaf/memory_writer.py",
    "        extra.update(\n            {\n                \"active_memory_id\": old.memory_id,\n                \"superseded_by\": superseded_by,\n                \"archived_at\": archived_at,\n            }\n        )\n",
    "        extra.update(\n            {\n                \"active_memory_id\": old.memory_id,\n                \"superseded_by\": superseded_by,\n                \"archived_at\": archived_at,\n            }\n        )\n        if invalidated_reason is not None:\n            extra[\"invalidated_reason\"] = invalidated_reason\n",
)
# Preflight survivor correction before ordinary target logic.
insert_before(
    "src/memleaf/memory_writer.py",
    "            duplicate_id = request.get(\"duplicate_memory_id\")\n",
    '''            correction = request.get("scope_correction")
            if isinstance(correction, Mapping) and correction.get("survivor_memory_id"):
                target_id = correction.get("target_memory_id")
                survivor_id = correction.get("survivor_memory_id")
                if not isinstance(target_id, str) or not isinstance(survivor_id, str) or target_id == survivor_id:
                    raise self._preflight_error("invalid scope correction targets")
                target_record = active.get(target_id)
                survivor_record = active.get(survivor_id)
                if target_record is None or survivor_record is None:
                    raise self._preflight_error("scope correction target is not active")
                target_memory = getattr(target_record, "memory", target_record)
                survivor_memory = getattr(survivor_record, "memory", survivor_record)
                if not isinstance(target_memory, Memory) or not isinstance(survivor_memory, Memory):
                    raise self._preflight_error("scope correction memory is invalid")
                if target_memory.type != survivor_memory.type or survivor_memory.type != request["summary"].get("type"):
                    raise self._preflight_error("scope correction type mismatch")
                if request.get("memory_id") != survivor_id:
                    raise self._preflight_error("scope correction survivor id mismatch")
                active.pop(target_id, None)
                continue
''',
)
# Special survivor retirement before duplicate/update handling.
insert_before(
    "src/memleaf/memory_writer.py",
    "        duplicate_id = request.get(\"duplicate_memory_id\")\n",
    '''        correction = request.get("scope_correction")
        if isinstance(correction, Mapping) and correction.get("survivor_memory_id"):
            target_id = correction.get("target_memory_id")
            survivor_id = correction.get("survivor_memory_id")
            target_record = active.get(target_id)
            survivor_record = active.get(survivor_id)
            if target_record is None or survivor_record is None:
                raise ModelOutputError("scope correction target is not active")
            target = getattr(target_record, "memory", target_record)
            survivor = getattr(survivor_record, "memory", survivor_record)
            if not isinstance(target, Memory) or not isinstance(survivor, Memory):
                raise ModelOutputError("scope correction memory is invalid")
            self._write_history(
                target,
                superseded_by=survivor.memory_id,
                archived_at=now,
                invalidated_reason="scope_correction",
            )
            path = self.service.vault.memory_path(target.memory_id, "knowledge")
            if path.is_symlink():
                raise ModelOutputError("unsafe knowledge path")
            if path.exists():
                path.unlink()
            if isinstance(active, dict):
                active.pop(target.memory_id, None)
            return survivor
''',
)
replace_once(
    "src/memleaf/memory_writer.py",
    "            self._write_history(existing, superseded_by=desired.memory_id, archived_at=now)\n",
    "            correction = request.get(\"scope_correction\")\n"
    "            invalidated_reason = \"scope_correction\" if isinstance(correction, Mapping) else None\n"
    "            self._write_history(\n"
    "                existing,\n"
    "                superseded_by=desired.memory_id,\n"
    "                archived_at=now,\n"
    "                invalidated_reason=invalidated_reason,\n"
    "            )\n",
)

# ---------------------------------------------------------------------------
# Hermes: bounded mail evidence extraction from public tool messages only.
# ---------------------------------------------------------------------------
insert_before(
    "src/memleaf/hermes_provider/__init__.py",
    "\n\nclass _MCPToolError(RuntimeError):\n",
    '''\n\n_MAX_MAIL_EVIDENCE_ITEMS = 8
_MAIL_ADDRESS_RE = re.compile(r"(?i)([A-Z0-9._%+-]+@([A-Z0-9.-]+\\.[A-Z]{2,63}))")
_MAIL_TOOL_RE = re.compile(r"(?:gmail|outlook|mail|email)", re.IGNORECASE)
''',
)
# Insert helper before provider class (use unique marker).
marker = "\n\nclass MemleafMemoryProvider(MemoryProvider):\n"
insert_before(
    "src/memleaf/hermes_provider/__init__.py",
    marker,
    '''\n\ndef _bounded_mail_evidence(messages: Optional[List[Dict[str, Any]]]) -> list[dict[str, str]]:
    """Extract only message id/subject/sender/domain from visible mail tool results."""

    calls = _visible_tool_calls(messages)
    results = _visible_tool_results(messages)
    used: set[int] = set()
    output: list[dict[str, str]] = []
    seen: set[tuple[tuple[str, str], ...]] = set()

    def add_record(mapping: Mapping[str, Any]) -> None:
        if len(output) >= _MAX_MAIL_EVIDENCE_ITEMS:
            return
        lowered = {str(key).casefold(): value for key, value in mapping.items()}
        message_id = next((lowered.get(key) for key in ("message_id", "messageid", "id") if isinstance(lowered.get(key), (str, int))), None)
        subject = next((lowered.get(key) for key in ("subject", "title") if isinstance(lowered.get(key), str)), None)
        sender = next((lowered.get(key) for key in ("sender", "from", "from_address", "from_email", "email") if isinstance(lowered.get(key), str)), None)
        domain = next((lowered.get(key) for key in ("domain", "sender_domain") if isinstance(lowered.get(key), str)), None)
        if isinstance(sender, str):
            match = _MAIL_ADDRESS_RE.search(sender)
            if match and not domain:
                domain = match.group(2)
        item: dict[str, str] = {}
        for key, value, limit in (
            ("message_id", message_id, 160),
            ("subject", subject, 320),
            ("sender", sender, 320),
            ("domain", domain, 253),
        ):
            if value is None:
                continue
            text = str(value).strip()
            if not text or any(ch in text for ch in "\\x00\\r\\n"):
                continue
            if key == "domain":
                text = text.casefold().lstrip("@")
                if not re.fullmatch(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\\.)+[a-z]{2,63}", text):
                    continue
            item[key] = text[:limit]
        if "domain" not in item:
            return
        fingerprint = tuple(sorted(item.items()))
        if fingerprint not in seen:
            seen.add(fingerprint)
            output.append(item)

    def visit(value: Any, depth: int = 0) -> None:
        if depth > 4 or len(output) >= _MAX_MAIL_EVIDENCE_ITEMS:
            return
        decoded = _decode_hermes_tool_result(value)
        if decoded is _MISSING_TOOL_RESULT:
            return
        if decoded is not value:
            visit(decoded, depth + 1)
            return
        if isinstance(value, Mapping):
            add_record(value)
            for child in value.values():
                if isinstance(child, (Mapping, list, tuple)):
                    visit(child, depth + 1)
        elif isinstance(value, (list, tuple)):
            for child in value:
                visit(child, depth + 1)

    ordinal = 0
    for call in calls:
        name = call.get("name")
        if not isinstance(name, str) or not _MAIL_TOOL_RE.search(name):
            continue
        ordinal += 1
        payload = _tool_result_for_call(call, calls, results, used)
        if payload is not _CALL_FAILED:
            visit(payload)
        if len(output) >= _MAX_MAIL_EVIDENCE_ITEMS:
            break
    return output
''',
)
replace_once(
    "src/memleaf/hermes_provider/__init__.py",
    "    def _capture_visible(self, *, session_id: str, turn_id: str, role: str, content: str) -> bool:\n",
    "    def _capture_visible(\n        self,\n        *,\n        session_id: str,\n        turn_id: str,\n        role: str,\n        content: str,\n        tool_evidence: Optional[List[Dict[str, str]]] = None,\n    ) -> bool:\n",
)
replace_once(
    "src/memleaf/hermes_provider/__init__.py",
    '                "visible": True,\n            },\n',
    '                "visible": True,\n                **({"tool_evidence": tool_evidence} if tool_evidence else {}),\n            },\n',
)
# Compute once and attach only to assistant event.
replace_once(
    "src/memleaf/hermes_provider/__init__.py",
    "                lineage_ready = self._retry_pending_lineage(effective_session)\n                for role, content in visible_events:\n",
    "                lineage_ready = self._retry_pending_lineage(effective_session)\n"
    "                mail_evidence = _bounded_mail_evidence(messages)\n"
    "                for role, content in visible_events:\n",
)
replace_once(
    "src/memleaf/hermes_provider/__init__.py",
    "                        role=role,\n                        content=content,\n                    ):\n",
    "                        role=role,\n                        content=content,\n                        tool_evidence=mail_evidence if role == \"assistant\" else None,\n                    ):\n",
)

# ---------------------------------------------------------------------------
# Regression tests for the new deterministic boundaries.
# ---------------------------------------------------------------------------
Path("tests/test_v023_scope_correction.py").write_text(r'''from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from memleaf import Memleaf
from memleaf.inbox import parse_inbox_text
from memleaf.memory_writer import MemoryWriter
from memleaf.models import Memory
from memleaf.processing import Processor
from memleaf.scope_maintenance import scope_registry_projection
from memleaf.scope_state import project_scopes_for_domains, validate_scope_registry
from memleaf.validation import split_mixed_future_use_text


class V023ScopeCorrectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="memleaf-v023-")
        self.root = Path(self.temp.name)
        self.service = Memleaf.initialize(self.root)
        config = self.service.vault.config()
        config["scopes"] = {
            "project:兴银理财": {"aliases": ["兴银"], "identifiers": ["cibwm.com"]},
            "project:鑫元基金": {"aliases": ["鑫元"], "identifiers": ["xyamc.com"]},
            "project:金元顺安": {"aliases": ["金元"], "identifiers": ["jysa.com"]},
        }
        from memleaf.config import save_config
        save_config(self.service.vault.config_path, config)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _memory(self, memory_id: str, scope: str, body: str, *, title: str = "流程要求") -> Memory:
        return self.service.create_memory(
            memory_id=memory_id,
            title=title,
            body=body,
            type="project",
            scopes=[scope],
            tags=["流程"],
        )

    def _turn(self, user: str):
        self.service.capture("hermes", "s", "t", "user", user, event_id="u")
        self.service.capture("hermes", "s", "t", "assistant", "收到", event_id="a")
        text = self.service.vault.session_path("hermes", "s").read_text(encoding="utf-8")
        return next(turn for turn in parse_inbox_text(text, source="hermes", session_id="s") if turn.complete)

    def test_scope_identifiers_are_private_and_domain_resolves_project(self) -> None:
        config = self.service.vault.config()
        registry = validate_scope_registry(config["scopes"])
        self.assertEqual(["xyamc.com"], registry["project:鑫元基金"]["identifiers"])
        self.assertEqual(["project:鑫元基金"], project_scopes_for_domains(["mail.xyamc.com"], config))
        projection = scope_registry_projection(config)
        self.assertTrue(all("identifiers" not in item for item in projection))

    def test_bounded_tool_evidence_round_trips_without_becoming_content(self) -> None:
        self.service.capture("hermes", "mail", "t1", "user", "看一下邮件", event_id="u1")
        self.service.capture(
            "hermes", "mail", "t1", "assistant", "邮件已检查", event_id="a1",
            tool_evidence=[{"message_id": "42", "subject": "流程", "sender": "x <a@xyamc.com>", "domain": "xyamc.com"}],
        )
        path = self.service.vault.session_path("hermes", "mail")
        text = path.read_text(encoding="utf-8")
        self.assertNotIn("a@xyamc.com", "邮件已检查")
        turn = next(turn for turn in parse_inbox_text(text, source="hermes", session_id="mail") if turn.complete)
        assistant = next(event for event in turn.events if event.role == "assistant")
        self.assertEqual("xyamc.com", assistant.tool_evidence[0]["domain"])
        self.assertEqual("邮件已检查", assistant.content)

    def test_explicit_scope_correction_authorizes_old_target_only(self) -> None:
        old = self._memory("mem-wrong", "project:兴银理财", "流程要求使用双人复核")
        turn = self._turn("之前归错客户了，不是兴银理财，是鑫元基金；流程要求仍是使用双人复核。")
        candidate = {
            "candidate_id": "c1", "memory": "鑫元基金流程要求使用双人复核", "worth": True,
            "duplicate": False, "type": "project", "scopes": ["project:鑫元基金"],
            "scope_source": "model", "update_memory_id": old.memory_id, "evidence_event_ids": list(turn.event_keys),
        }
        processor = Processor(self.service)
        plan = processor._scope_correction_plan(candidate, turn, self.service.vault.config())
        self.assertIsNotNone(plan)
        self.assertEqual(old.memory_id, plan["target_memory_id"])
        self.assertEqual("project:鑫元基金", plan["new_scope"])

        no_correction = self._turn("鑫元基金流程要求使用双人复核。")
        self.assertIsNone(processor._scope_correction_plan(candidate, no_correction, self.service.vault.config()))
        self.assertEqual("NOT_RELATED", processor._target_relation(candidate, turn=no_correction))

    def test_existing_correct_survivor_retires_wrong_active_to_history(self) -> None:
        wrong = self._memory("mem-wrong2", "project:兴银理财", "流程要求使用双人复核")
        correct = self._memory("mem-correct2", "project:鑫元基金", "鑫元基金流程要求使用双人复核")
        turn = self._turn("之前归错客户了，不是兴银理财，是鑫元基金；流程要求使用双人复核。")
        request = {
            "summary": {
                "title": correct.title, "body": correct.body, "tags": list(correct.tags), "type": correct.type,
                "scopes": list(correct.scopes), "scope_source": "model", "sources": [], "scope_operations": [],
            },
            "turn": turn, "candidate_id": "c2", "memory_id": correct.memory_id,
            "event_key": turn.event_keys[0], "turn_id": "", "conversation_title": "test", "explicit_remember": False,
            "native_refs": [],
            "scope_correction": {
                "target_memory_id": wrong.memory_id, "survivor_memory_id": correct.memory_id,
                "old_scope": "project:兴银理财", "new_scope": "project:鑫元基金", "ambiguous": False,
            },
        }
        with self.service.vault.lock():
            written = MemoryWriter(self.service).write_many_unlocked([request], now="2026-09-03T08:00:00Z")
            self.service._rebuild_index_unlocked()
        self.assertEqual(correct.memory_id, written[0].memory_id)
        active_ids = {m.memory_id for m in self.service.search("流程", include_history=False)}
        self.assertNotIn(wrong.memory_id, active_ids)
        self.assertIn(correct.memory_id, active_ids)
        history = [r.memory for r in self.service._read_memories_unlocked("history")]
        archived = next(item for item in history if item.extra.get("active_memory_id") == wrong.memory_id)
        self.assertEqual("scope_correction", archived.extra.get("invalidated_reason"))
        self.assertEqual(correct.memory_id, archived.extra.get("superseded_by"))

    def test_same_project_mixed_future_use_splits_only_when_every_clause_is_classifiable(self) -> None:
        split = split_mixed_future_use_text(
            "金元顺安实施计划要求后续交付都保留回滚方案；同时需要按6点调整实施计划并提交反馈"
        )
        self.assertIsNotNone(split)
        self.assertEqual({"project", "todo"}, {kind for _text, kind in split})
        self.assertIsNone(split_mixed_future_use_text("金元顺安实施计划后续按规范执行；另外还有一些事情"))

    def test_unique_mail_domain_conflict_is_detected_without_scope_map_exposure(self) -> None:
        self.service.capture("hermes", "evidence", "t2", "user", "处理这封邮件", event_id="eu")
        self.service.capture(
            "hermes", "evidence", "t2", "assistant", "已查看", event_id="ea",
            tool_evidence=[{"message_id": "99", "sender": "pm@xyamc.com", "domain": "xyamc.com"}],
        )
        text = self.service.vault.session_path("hermes", "evidence").read_text(encoding="utf-8")
        turn = next(turn for turn in parse_inbox_text(text, source="hermes", session_id="evidence") if turn.complete)
        candidate = {
            "candidate_id": "c3", "memory": "兴银理财流程调整", "worth": True,
            "duplicate": False, "type": "project", "scopes": ["project:兴银理财"], "scope_source": "model",
        }
        processor = Processor(self.service)
        self.assertEqual("project:鑫元基金", processor._turn_evidence_project_scope(turn, self.service.vault.config()))
        self.assertTrue(processor._scope_evidence_conflict(candidate, turn, self.service.vault.config()))


if __name__ == "__main__":
    unittest.main()
''', encoding="utf-8")

print("v0.2.23 core patch applied")

"""Compile host proposals into immutable, model-free memory changes.

The caller owns authentication, snapshot identity, source capture, budgets and
durable execution. This module checks physical evidence and read qualifications;
it does not claim to prove that a quote implies an authored fact, or that a
compaction is semantically equivalent. No caller-provided metadata is trusted as
an authorization or operation receipt.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
from typing import Any, Callable, Mapping

from .host_v2_common import V2Error, revision
from .incremental_dates import selected_calendar
from .models import Memory, utc_now
from .source_policy import merge_memory_provenance, merge_sources

ASSERTIONS = {"user_fact", "user_decision", "user_preference", "assistant_report"}
TYPES = {"fact", "todo", "preference", "project", "event", "identity", "other"}
FIELDS = {"title", "scopes", "tags", "aliases", "keywords", "status", "actionable",
          "assignee", "waiting_on", "due_date", "due_text", "completed_at"}
TASK_FIELDS = {"status", "assignee", "waiting_on", "due_date", "due_text", "completed_at"}
NULLABLE = {"assignee", "waiting_on", "due_date", "due_text", "completed_at"}
SENSITIVE = TASK_FIELDS | {"actionable", "validity"}
SET_FIELDS = {"tags", "aliases", "keywords", "scopes"}
SCALARS = {"title", "type", "status", "actionable", "assignee", "waiting_on",
           "due_date", "due_text", "completed_at"}
PROVENANCE_EXTRA = {"field_basis", "source_count", "source_digest", "sources_omitted",
                    "source_count_is_upper_bound", "explicit_remember", "v2_operation_id",
                    "incremental_operation_id", "explicit_operation_id", "explicit_update_operation_id",
                    "source", "v2_merge_provenance"}


def _fail(code: str, message: str) -> None:
    raise V2Error(code, message)


def _keys(value: Any, required: set[str], allowed: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - allowed:
        _fail("INVALID_SCHEMA", "proposal fields do not match this action")
    return value


def _text(value: Any, maximum: int, *, blank: bool = False) -> str:
    if not isinstance(value, str) or not value or (not blank and not value.strip()) or len(value) > maximum:
        _fail("INVALID_SCHEMA", "invalid bounded text")
    return value


def _refs(value: Any, maximum: int, *, empty: bool = False) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum or (not value and not empty):
        _fail("INVALID_SCHEMA", "invalid reference list")
    result = [_text(ref, 256) for ref in value]
    if len(result) != len(set(result)):
        _fail("INVALID_SCHEMA", "duplicate reference")
    return result


def _source(ref: str, snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
    sources = snapshot.get("sources", {})
    source = sources.get(ref) if isinstance(sources, Mapping) else None
    if not isinstance(source, Mapping):
        _fail("INVALID_EVIDENCE", "source reference is not in this snapshot")
    if source.get("role") not in {"user", "assistant"} or source.get("recording_allowed") is False:
        _fail("SOURCE_NOT_ADMISSIBLE", "source is not an allowed visible message")
    if not isinstance(source.get("text"), str):
        _fail("INVALID_EVIDENCE", "source text is unavailable")
    return source


def _evidence(value: Any, snapshot: Mapping[str, Any], *, user: bool = False,
              assertion: str | None = None) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 8:
        _fail("INVALID_EVIDENCE", "at least one bounded evidence binding is required")
    result = []
    for binding in value:
        _keys(binding, {"source", "quote"}, {"source", "quote", "occurrence"})
        ref, quote = _text(binding["source"], 256), _text(binding["quote"], 1000)
        source = _source(ref, snapshot)
        positions, offset = [], 0
        while True:
            found = source["text"].find(quote, offset)
            if found < 0:
                break
            positions.append(found)
            offset = found + 1
        occurrence = binding.get("occurrence")
        if occurrence is None:
            if len(positions) != 1:
                _fail("INVALID_EVIDENCE", "quote must identify one source occurrence")
            occurrence = 0
        if type(occurrence) is not int or occurrence < 0 or occurrence >= len(positions):
            _fail("INVALID_EVIDENCE", "quote occurrence is absent")
        result.append({"source": ref, "quote": quote, "occurrence": occurrence,
                       "start": positions[occurrence], "end": positions[occurrence] + len(quote),
                       "source_id": source.get("source_id"), "revision": source.get("revision"),
                       "role": source["role"],
                       "trust_level": source.get("trust_level", source.get("trust"))})
    roles = {binding["role"] for binding in result}
    if user and "user" not in roles:
        _fail("INVALID_EVIDENCE", "operation requires a current user source")
    if assertion is not None:
        if not isinstance(assertion, str) or assertion not in ASSERTIONS:
            _fail("INVALID_SCHEMA", "unknown assertion kind")
        expected = "assistant" if assertion == "assistant_report" else "user"
        if expected not in roles:
            _fail("INVALID_EVIDENCE", "assertion kind is not supported by its source role")
    return result


def _scope(scopes: Any, snapshot: Mapping[str, Any]) -> list[str]:
    values = _refs(scopes, 4)
    allowed = snapshot.get("write_scopes", [])
    if not isinstance(allowed, (list, tuple, set)) or not set(values) <= set(allowed):
        _fail("SCOPE_DENIED", "proposal scope is outside the granted write scopes")
    return values


def _fields(value: Any, snapshot: Mapping[str, Any], *, create: bool = False) -> dict[str, Any]:
    allowed = FIELDS | ({"type", "body"} if create else set())
    required = {"type", "title", "body", "scopes"} if create else set()
    _keys(value, required, allowed)
    if not value:
        _fail("INVALID_SCHEMA", "empty field update")
    fields = deepcopy(value)
    for name, current in fields.items():
        if current is None:
            if name not in NULLABLE:
                _fail("INVALID_SCHEMA", "this field cannot be cleared with null")
            continue
        if name == "type":
            if not isinstance(current, str) or current not in TYPES:
                _fail("INVALID_SCHEMA", "unsupported memory type")
        elif name == "status":
            if not isinstance(current, str) or current not in {"active", "completed", "cancelled"}:
                _fail("INVALID_SCHEMA", "unsupported action status")
        elif name == "actionable":
            if type(current) is not bool:
                _fail("INVALID_SCHEMA", "actionable must be boolean")
        elif name in SET_FIELDS:
            _refs(current, 4 if name == "scopes" else 16, empty=name != "scopes")
            limit = 120 if name in {"aliases", "scopes"} else 64
            for element in current:
                _text(element, limit)
            if name == "scopes":
                _scope(current, snapshot)
        elif name == "due_date":
            try:
                if not isinstance(current, str) or date.fromisoformat(current).isoformat() != current:
                    raise ValueError
            except ValueError:
                _fail("INVALID_SCHEMA", "due_date must be a real ISO calendar date")
        elif name == "completed_at":
            try:
                _text(current, 64)
                parsed = datetime.fromisoformat(current.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError
            except ValueError:
                _fail("INVALID_SCHEMA", "completed_at requires a timezone")
        else:
            _text(current, 8000 if name == "body" else 256 if name in {"due_text", "waiting_on"} else 120)
    return fields


def _target(ref: str, snapshot: Mapping[str, Any], *, write: bool = True,
            full: bool = False) -> tuple[dict[str, Any], Memory]:
    target = snapshot.get("targets", {}).get(ref)
    if not isinstance(target, Mapping) or not isinstance(target.get("memory"), Mapping):
        _fail("TARGET_UNAVAILABLE", "target reference is not in this snapshot")
    if target.get("read_fields") is not True:
        _fail("READ_NOT_QUALIFIED", "target fields and revision must be read before use")
    try:
        memory = Memory.from_mapping(deepcopy(target["memory"]))
    except (ValueError, TypeError):
        _fail("STATE_CORRUPT", "snapshot contains an invalid memory")
    if target.get("revision") != revision(memory):
        _fail("SNAPSHOT_STALE", "target content does not match its protected revision")
    if write:
        if target.get("writable") is not True or target.get("native") is True or target.get("area", "knowledge") != "knowledge":
            _fail("TARGET_READ_ONLY", "target is not a writable knowledge memory")
        _scope(memory.scopes, snapshot)
    if full:
        _full_fragments(target, memory.body)
    return dict(target), memory


def _full_fragments(target: Mapping[str, Any], body: str) -> list[dict[str, Any]]:
    fragments = target.get("fragments", [])
    read = set(target.get("read_fragments", []))
    if not isinstance(fragments, list):
        _fail("STATE_CORRUPT", "snapshot fragment directory is invalid")
    offset, seen = 0, set()
    for fragment in fragments:
        if not isinstance(fragment, dict):
            _fail("STATE_CORRUPT", "invalid snapshot fragment")
        fid, start, end = fragment.get("fragment_id"), fragment.get("start"), fragment.get("end")
        if (not isinstance(fid, str) or fid in seen or type(start) is not int or type(end) is not int
                or start != offset or not start < end <= len(body) or fragment.get("text") != body[start:end]):
            _fail("STATE_CORRUPT", "fragment directory does not cover its protected body")
        if fid not in read:
            _fail("READ_NOT_QUALIFIED", "all basis fragments must be fully read")
        seen.add(fid)
        offset = end
    if offset != len(body):
        _fail("READ_NOT_QUALIFIED", "full target basis has not been read")
    return fragments


def _field_basis(item: Mapping[str, Any], fields: Mapping[str, Any], evidence: list[dict[str, Any]],
                 snapshot: Mapping[str, Any], *, user: bool = False) -> dict[str, Any]:
    supplied = item.get("field_evidence", [])
    if not isinstance(supplied, list) or len(supplied) > 16:
        _fail("INVALID_SCHEMA", "invalid field evidence directory")
    narrower = {}
    for row in supplied:
        _keys(row, {"field", "evidence"}, {"field", "evidence"})
        name = row["field"]
        if not isinstance(name, str) or name not in fields or name in narrower:
            _fail("INVALID_EVIDENCE", "field evidence must name one changed field")
        narrower[name] = _evidence(row["evidence"], snapshot, user=user,
                                   assertion=item.get("assertion_kind"))
    result = {}
    for name, value in fields.items():
        selected = narrower.get(name, evidence)
        if user and not any(binding["role"] == "user" for binding in selected):
            _fail("INVALID_EVIDENCE", "field decision requires user evidence")
        # Literal ownership and date fields have a deterministic check in
        # addition to the host's semantic responsibility. Lifecycle enums and
        # free-form authored content cannot be proved through keyword matching.
        if value is not None and name in {"assignee", "waiting_on", "due_text", "completed_at"}:
            self_assignment = (name in {"assignee", "waiting_on"} and value == "user"
                               and any(binding["role"] == "user" for binding in selected))
            if not self_assignment and not any(value in binding["quote"] for binding in selected):
                _fail("INVALID_EVIDENCE", "field value is not present in its selected evidence")
        if name == "due_date" and value is not None:
            supported = False
            for binding in selected:
                source = _source(binding["source"], snapshot)
                expression = fields.get("due_text") or binding["quote"]
                trusted_time = (source.get("source_time")
                                if source.get("trust_level", source.get("trust")) in {"host_bound", "user_confirmed"}
                                else None)
                try:
                    calendar = selected_calendar(expression, {"text": binding["quote"], "source_time": trusted_time})
                    supported = supported or calendar["date"] == value
                except ValueError:
                    continue
            if not supported:
                _fail("INVALID_EVIDENCE", "due_date lacks a supported source calendar expression")
        result[name] = {"protocol": "memleaf-host-v2", "bindings": deepcopy(selected)}
    return result


def _task_state(value: dict[str, Any], *, old: Mapping[str, Any] | None,
                fields: Mapping[str, Any], transition: str, evidence: list[dict[str, Any]]) -> None:
    actionable = value.get("type") == "todo" or value.get("actionable") is True
    if actionable and value.get("status") is None:
        _fail("INVALID_SCHEMA", "action memories require an explicit status")
    if not actionable:
        checked = value if old is None else fields
        if any(checked.get(name) is not None for name in TASK_FIELDS):
            _fail("INVALID_SCHEMA", "new task fields require todo or actionable=true")
    if old is not None and old.get("status") in {"completed", "cancelled"} and value.get("status") == "active":
        if transition != "reopen" or not any(binding["role"] == "user" for binding in evidence):
            _fail("INVALID_EVIDENCE", "reopening requires transition=reopen and user evidence")
    elif transition == "reopen":
        _fail("INVALID_SCHEMA", "reopen must change a closed action to active")
    if value.get("completed_at") is not None and value.get("status") != "completed":
        _fail("INVALID_SCHEMA", "completed_at requires completed status or explicit clearing")


def _attach_sources(value: dict[str, Any], evidence: list[dict[str, Any]], snapshot: Mapping[str, Any],
                    basis: Mapping[str, Any]) -> None:
    metadata = []
    for ref in dict.fromkeys(binding["source"] for binding in evidence):
        source = _source(ref, snapshot)
        metadata.append({key: deepcopy(source[key]) for key in (
            "source_id", "revision", "role", "source_time", "trust_level", "trust", "session_id", "turn_key"
        ) if source.get(key) is not None})
    retained, counters = merge_sources(value.get("sources", []), metadata, extra=value)
    value["sources"] = retained
    value.update(counters)
    if basis:
        previous = value.get("field_basis", {})
        if not isinstance(previous, dict):
            _fail("TARGET_METADATA_CONFLICT", "existing field provenance is not a mapping")
        value["field_basis"] = {**deepcopy(previous), **deepcopy(basis)}


def _include_field_evidence(evidence: list[dict[str, Any]], basis: Mapping[str, Any]) -> None:
    for value in basis.values():
        for binding in value["bindings"]:
            if binding not in evidence:
                evidence.append(deepcopy(binding))


def _change(ref: str | None, before: Memory | None, value: dict[str, Any],
            expected_revision: str | None) -> dict[str, Any]:
    try:
        after = Memory.from_mapping(value)
    except (ValueError, TypeError):
        _fail("INVALID_SCHEMA", "compiled memory violates the memory contract")
    return {"memory_id": after.memory_id, "target_ref": ref,
            "before": before.to_dict() if before is not None else None,
            "after": after.to_dict(), "expected_revision": expected_revision}


def _patch_body(patches: Any, target: Mapping[str, Any], memory: Memory,
                snapshot: Mapping[str, Any], *, assertion: str) -> tuple[str, list[list[int]], list[dict[str, Any]]]:
    if not isinstance(patches, list) or not 1 <= len(patches) <= 16:
        _fail("INVALID_SCHEMA", "body patches must be a bounded nonempty array")
    body = memory.body
    fragments = {row.get("fragment_id"): row for row in target.get("fragments", []) if isinstance(row, dict)}
    read = set(target.get("read_fragments", []))
    prior_map = snapshot.get("removed_ranges", {})
    prior = prior_map.get(memory.memory_id, []) if isinstance(prior_map, Mapping) else []
    base_body = target.get("original_body", body)
    if prior and base_body != body:
        _fail("SNAPSHOT_STALE", "cumulative patch coordinates cannot be safely mapped to the refreshed body")
    replacements, appends, evidence = [], [], []
    for patch in patches:
        if not isinstance(patch, dict):
            _fail("INVALID_SCHEMA", "invalid body patch")
        op = patch.get("op")
        required = {"op", "evidence", "new_text"} if op == "append" else {"op", "evidence", "fragment", "old_text"}
        if op == "replace":
            required.add("new_text")
        if not isinstance(op, str) or op not in {"append", "replace", "retract"}:
            _fail("INVALID_SCHEMA", "unsupported body patch operation")
        _keys(patch, required, required)
        evidence.extend(_evidence(patch["evidence"], snapshot, user=op == "retract", assertion=assertion))
        new_text = "" if op == "retract" else _text(patch["new_text"], 4000)
        if op == "append":
            appends.append(new_text)
            continue
        fragment_id = _text(patch["fragment"], 256)
        fragment = fragments.get(fragment_id)
        if fragment is None or fragment_id not in read:
            _fail("READ_NOT_QUALIFIED", "edit anchor must be a fully read current fragment")
        start, end = fragment.get("start"), fragment.get("end")
        if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(body)
                or fragment.get("text") != body[start:end]):
            _fail("STATE_CORRUPT", "edit fragment does not match the protected body")
        old_text = _text(patch["old_text"], 1000, blank=True)
        first = fragment["text"].find(old_text)
        if first < 0 or fragment["text"].find(old_text, first + 1) >= 0:
            _fail("PATCH_NOT_UNIQUE", "old_text must occur exactly once within its fragment")
        replacements.append((start + first, start + first + len(old_text), new_text))
    replacements.sort(key=lambda row: row[0])
    if any(right[0] < left[1] for left, right in zip(replacements, replacements[1:])):
        _fail("PATCH_OVERLAP", "patches overlap in their original body basis")
    removed = []
    for row in prior:
        if not isinstance(row, (list, tuple)) or len(row) != 2 or not all(type(v) is int for v in row) or not 0 <= row[0] < row[1] <= len(body):
            _fail("STATE_CORRUPT", "invalid cumulative removed range")
        removed.append(list(row))
    removed.extend([[start, end] for start, end, _ in replacements])
    union = []
    for start, end in sorted(removed):
        if union and start <= union[-1][1]:
            union[-1][1] = max(union[-1][1], end)
        else:
            union.append([start, end])
    if union and all(character.isspace() or any(start <= i < end for start, end in union)
                     for i, character in enumerate(body)):
        _fail("FULL_REWRITE_REQUIRES_MAINTENANCE", "patches would cumulatively replace or remove all original content")
    result, offset = [], 0
    for start, end, replacement in replacements:
        result.extend([body[offset:start], replacement])
        offset = end
    result.append(body[offset:])
    new_body = "".join(result)
    for appended in appends:
        new_body += ("\n\n" if new_body else "") + appended
    if not new_body.strip():
        _fail("FULL_REWRITE_REQUIRES_MAINTENANCE", "an active memory cannot have empty body")
    if len(new_body) > 8000 and len(new_body) > len(body):
        _fail("INPUT_TOO_LARGE", "body patch grows an over-limit memory")
    return new_body, union, evidence


def compile_item(item: Any, snapshot: Mapping[str, Any], *,
                 approval_check: Callable[[str, dict[str, Any]], bool] | None = None) -> dict[str, Any]:
    """Validate one snapshot-bound item and return serializable frozen changes.

    ``approval_check`` receives ``(approval_ref, exact_plan)`` and must be an
    owner-side verifier. Returning true validates an existing owner approval;
    this compiler never issues approvals. IDs/timestamps are generated once by
    Core and the returned payload must be persisted before any business write.
    """
    _keys(item, {"item_id", "action"}, {"item_id", "action", "assertion_kind", "memory", "evidence",
          "field_evidence", "update_kind", "target", "fields", "body_patch", "transition", "body",
          "duplicates", "body_mode", "resolutions", "approval_ref", "new_body", "mapping",
          "source_refs", "target_refs", "reason", "need"})
    identifier, action = _text(item["item_id"], 256), item["action"]
    plan = {"item_id": identifier, "action": action, "changes": [], "source_refs": [],
            "target_refs": [], "removed_ranges": {}}
    evidence: list[dict[str, Any]] = []
    now = utc_now()
    if action == "CREATE":
        _keys(item, {"item_id", "action", "assertion_kind", "memory", "evidence"},
              {"item_id", "action", "assertion_kind", "memory", "evidence", "field_evidence"})
        if not isinstance(item["assertion_kind"], str) or item["assertion_kind"] not in ASSERTIONS:
            _fail("INVALID_SCHEMA", "unknown assertion kind")
        evidence = _evidence(item["evidence"], snapshot, assertion=item["assertion_kind"])
        fields = _fields(item["memory"], snapshot, create=True)
        _task_state(fields, old=None, fields=fields, transition="normal", evidence=evidence)
        basis = _field_basis(item, fields, evidence, snapshot)
        _include_field_evidence(evidence, basis)
        value = Memory.new(**fields).to_dict()
        if snapshot.get("purpose") == "explicit":
            value["explicit_remember"] = True
        _attach_sources(value, evidence, snapshot, basis)
        plan["changes"].append(_change(None, None, value, None))
    elif action == "UPDATE":
        kind = item.get("update_kind")
        required = {"item_id", "action", "update_kind", "target", "evidence"}
        allowed = set(required)
        if kind == "patch":
            required.add("assertion_kind")
            allowed |= {"assertion_kind", "fields", "body_patch", "field_evidence", "transition"}
        elif kind == "restore":
            required.add("body")
            allowed |= {"body", "fields"}
        elif kind != "retract_memory":
            _fail("ACTION_NOT_SUPPORTED", "unsupported update kind")
        _keys(item, required, allowed)
        if kind == "patch" and (not isinstance(item["assertion_kind"], str) or item["assertion_kind"] not in ASSERTIONS):
            _fail("INVALID_SCHEMA", "unknown assertion kind")
        evidence = _evidence(item["evidence"], snapshot, user=kind in {"restore", "retract_memory"},
                             assertion=item.get("assertion_kind"))
        ref = _text(item["target"], 256)
        target, before = _target(ref, snapshot)
        value = before.to_dict()
        fields = _fields(item["fields"], snapshot) if "fields" in item else {}
        if kind == "patch":
            if before.validity != "valid":
                _fail("INVALID_SCHEMA", "retracted memory requires explicit restore")
            if not fields and "body_patch" not in item:
                _fail("INVALID_SCHEMA", "update must change fields or body")
            if not isinstance(item.get("transition", "normal"), str) or item.get("transition", "normal") not in {"normal", "reopen"}:
                _fail("INVALID_SCHEMA", "unknown action transition")
            if "body_patch" in item:
                value["body"], ranges, patch_evidence = _patch_body(item["body_patch"], target, before, snapshot,
                                                                  assertion=item["assertion_kind"])
                plan["removed_ranges"][before.memory_id] = ranges
                evidence.extend(patch_evidence)
                fields["body"] = value["body"]
        elif kind == "retract_memory":
            if before.validity != "valid":
                _fail("INVALID_SCHEMA", "memory is already retracted")
            fields = {"body": "", "validity": "retracted"}
        else:
            if before.validity != "retracted":
                _fail("INVALID_SCHEMA", "restore requires a retracted memory")
            if before.extra.get("merged_into") is not None:
                _fail("INVALID_SCHEMA", "merged alias cannot be restored as an independent head")
            fields.update(body=_text(item["body"], 8000), validity="valid")
        basis = _field_basis(item, fields, evidence, snapshot, user=kind in {"restore", "retract_memory"})
        _include_field_evidence(evidence, basis)
        value.update(fields)
        if kind != "retract_memory":
            _task_state(value, old=before.to_dict(), fields=fields,
                        transition=item.get("transition", "normal"), evidence=evidence)
        value["updated"] = now
        _attach_sources(value, evidence, snapshot, basis)
        plan["changes"].append(_change(ref, before, value, target["revision"]))
        plan["target_refs"] = [ref]
    elif action == "MERGE":
        _keys(item, {"item_id", "action", "target", "duplicates", "body_mode", "resolutions"},
              {"item_id", "action", "target", "duplicates", "body_mode", "resolutions", "approval_ref"})
        if item["body_mode"] != "preserve_union":
            _fail("INVALID_SCHEMA", "merge must preserve the body union")
        ref = _text(item["target"], 256)
        refs = [ref, *_refs(item["duplicates"], 7)]
        if len(refs) != len(set(refs)):
            _fail("INVALID_SCHEMA", "merge participants must be distinct")
        participants = {current: _target(current, snapshot, full=True) for current in refs}
        memories = [participants[current][1] for current in refs]
        if len({memory.memory_id for memory in memories}) != len(memories):
            _fail("ITEM_SET_CHANGED", "merge participants resolve to the same memory identity")
        if any(memory.validity != "valid" for memory in memories):
            _fail("INVALID_SCHEMA", "merge participants must be active memory heads")
        resolutions = item["resolutions"]
        if not isinstance(resolutions, list) or len(resolutions) > 10:
            _fail("INVALID_SCHEMA", "invalid scalar conflict resolutions")
        selected, resolution_basis = {}, {}
        for row in resolutions:
            _keys(row, {"field", "keep_from", "evidence"}, {"field", "keep_from", "evidence"})
            name, keep = row["field"], row["keep_from"]
            if not isinstance(name, str) or not isinstance(keep, str) or name not in SCALARS or name in selected or keep not in participants:
                _fail("INVALID_SCHEMA", "resolution must select one actual participant field")
            bindings = _evidence(row["evidence"], snapshot, user=True)
            evidence.extend(bindings)
            selected[name] = participants[keep][1].to_dict().get(name)
            resolution_basis[name] = {"protocol": "memleaf-host-v2", "bindings": bindings}
        before = memories[0]
        value = before.to_dict()
        for name in SCALARS:
            options = [memory.to_dict().get(name) for memory in memories]
            if any(option != options[0] for option in options[1:]):
                if name not in selected:
                    _fail("FIELD_CONFLICT", "merge contains an unresolved scalar conflict")
                value[name] = deepcopy(selected[name])
            elif name in selected:
                _fail("INVALID_SCHEMA", "resolution does not correspond to a scalar conflict")
        for name in SET_FIELDS:
            value[name] = list(dict.fromkeys(element for memory in memories for element in memory.to_dict().get(name, [])))
        _scope(value["scopes"], snapshot)
        extra_keys = set().union(*(memory.extra.keys() for memory in memories)) - PROVENANCE_EXTRA - TASK_FIELDS
        for name in extra_keys:
            present = [memory.extra[name] for memory in memories if name in memory.extra]
            if any(option != present[0] for option in present[1:]):
                _fail("TARGET_METADATA_CONFLICT", "merge contains conflicting custom metadata")
            value[name] = deepcopy(present[0])
        # Exact whole bodies are the largest safe deduplication units. Keeping
        # every differing body avoids interpreting text boundaries as facts.
        value["body"] = "\n\n".join(dict.fromkeys(memory.body for memory in memories))
        if len(value["body"]) > 8000:
            _fail("INPUT_TOO_LARGE", "merged body exceeds the full-body limit")
        value["sources"], counters = merge_memory_provenance(memories)
        value.update(counters)
        # Keep established per-head provenance, including older bounded field
        # bases, without treating Core operation markers as user metadata.
        lineage = deepcopy(value.get("v2_merge_provenance", {}))
        if not isinstance(lineage, dict):
            _fail("TARGET_METADATA_CONFLICT", "existing merge provenance is not a mapping")
        for memory in memories:
            inherited = memory.extra.get("v2_merge_provenance", {})
            if not isinstance(inherited, dict):
                _fail("TARGET_METADATA_CONFLICT", "existing merge provenance is not a mapping")
            for old_id, old_basis in inherited.items():
                if old_id in lineage and lineage[old_id] != old_basis:
                    _fail("TARGET_METADATA_CONFLICT", "merge has conflicting inherited provenance")
                lineage[old_id] = deepcopy(old_basis)
            lineage[memory.memory_id] = {name: deepcopy(memory.extra[name])
                                         for name in ("field_basis", "source") if name in memory.extra}
        if len(lineage) > 128:
            _fail("CAPACITY_EXCEEDED", "merged provenance exceeds its bounded lineage capacity")
        value["v2_merge_provenance"] = lineage
        if any(memory.extra.get("explicit_remember") for memory in memories):
            value["explicit_remember"] = True
        value["updated"] = now
        _task_state(value, old=None, fields={}, transition="normal", evidence=evidence)
        _attach_sources(value, evidence, snapshot, resolution_basis)
        plan["changes"].append(_change(ref, before, value, participants[ref][0]["revision"]))
        for duplicate in refs[1:]:
            old = participants[duplicate][1]
            retired = old.to_dict()
            retired.update(body="", validity="retracted", merged_into=before.memory_id, updated=now)
            plan["changes"].append(_change(duplicate, old, retired, participants[duplicate][0]["revision"]))
        plan["target_refs"] = refs
        plan["dependency_group"] = [memory.memory_id for memory in memories]
    elif action == "COMPACT":
        _keys(item, {"item_id", "action", "target", "new_body", "mapping", "approval_ref"},
              {"item_id", "action", "target", "new_body", "mapping", "approval_ref"})
        if snapshot.get("purpose") != "maintenance":
            _fail("MAINTENANCE_AUTH_REQUIRED", "compaction requires a maintenance work")
        ref = _text(item["target"], 256)
        target, before = _target(ref, snapshot, full=True)
        if before.validity != "valid":
            _fail("INVALID_SCHEMA", "compaction requires a valid current head")
        body = _text(item["new_body"], 8000)
        mapping = item["mapping"]
        if not isinstance(mapping, list) or not 1 <= len(mapping) <= 128:
            _fail("INVALID_SCHEMA", "invalid compaction map")
        expected = {row["fragment_id"] for row in _full_fragments(target, before.body)}
        seen, mapped = set(), []
        for row in mapping:
            _keys(row, {"old_fragment", "disposition", "new_quote"}, {"old_fragment", "disposition", "new_quote"})
            old, quote = row["old_fragment"], _text(row["new_quote"], 1000)
            if (not isinstance(old, str) or old not in expected or old in seen
                    or not isinstance(row["disposition"], str) or row["disposition"] not in {"retain", "coalesce"}):
                _fail("INVALID_COVERAGE", "mapping must cover each original fragment exactly once")
            seen.add(old)
            offset = body.find(quote)
            if offset < 0:
                _fail("INVALID_EVIDENCE", "mapped quote does not occur in the proposed body")
            if body.find(quote, offset + 1) >= 0:
                _fail("INVALID_EVIDENCE", "mapped quote does not identify a unique new-body span")
            mapped.append((offset, offset + len(quote)))
        if seen != expected:
            _fail("INVALID_COVERAGE", "mapping omits original fragments")
        if any(not character.isspace() and not any(start <= i < end for start, end in mapped)
               for i, character in enumerate(body)):
            _fail("INVALID_COVERAGE", "new body contains text outside mapped quote spans")
        approval = _text(item["approval_ref"], 256)
        exact = {"action": "COMPACT", "target": before.memory_id, "revision": target["revision"],
                 "new_body": body, "mapping": deepcopy(mapping)}
        if approval_check is None or approval_check(approval, deepcopy(exact)) is not True:
            _fail("MAINTENANCE_AUTH_REQUIRED", "owner approval must match the exact body, mapping and revision")
        value = before.to_dict()
        value.update(body=body, updated=now)
        plan["changes"].append(_change(ref, before, value, target["revision"]))
        plan["target_refs"] = [ref]
        plan["approval_plan"] = exact
        plan["approval_ref"] = approval
    elif action == "NO_CHANGE":
        _keys(item, {"item_id", "action", "target", "evidence"}, {"item_id", "action", "target", "evidence"})
        ref = _text(item["target"], 256)
        _target(ref, snapshot, write=False)
        evidence = _evidence(item["evidence"], snapshot)
        plan["target_refs"] = [ref]
    elif action == "NO_MEMORY":
        _keys(item, {"item_id", "action", "source_refs", "reason"}, {"item_id", "action", "source_refs", "reason"})
        if snapshot.get("purpose") == "explicit":
            _fail("EXPLICIT_RETENTION_REQUIRED", "explicit_retention_required")
        if not isinstance(item["reason"], str) or item["reason"] not in {"no_future_value", "question_only", "temporary", "already_represented_in_this_work"}:
            _fail("INVALID_SCHEMA", "unknown no-memory reason")
        plan["source_refs"] = _refs(item["source_refs"], 16)
        for ref in plan["source_refs"]:
            _source(ref, snapshot)
    elif action == "DEFERRED":
        _keys(item, {"item_id", "action", "source_refs", "target_refs", "reason", "need"},
              {"item_id", "action", "source_refs", "target_refs", "reason", "need"})
        if not isinstance(item["reason"], str) or item["reason"] not in {"missing_evidence", "ambiguous_target", "needs_user_decision", "insufficient_context", "unsupported_action"}:
            _fail("INVALID_SCHEMA", "unknown deferred reason")
        _text(item["need"], 256)
        plan["source_refs"] = _refs(item["source_refs"], 16, empty=True)
        plan["target_refs"] = _refs(item["target_refs"], 12, empty=True)
        if not plan["source_refs"] and not plan["target_refs"]:
            _fail("INVALID_SCHEMA", "deferred work must identify source or target context")
        for ref in plan["source_refs"]:
            _source(ref, snapshot)
        for ref in plan["target_refs"]:
            if ref not in snapshot.get("targets", {}):
                _fail("TARGET_UNAVAILABLE", "deferred target is not in this snapshot")
    else:
        _fail("ACTION_NOT_SUPPORTED", "unsupported proposal action")
    if evidence:
        plan["source_refs"] = list(dict.fromkeys(binding["source"] for binding in evidence))
        plan["evidence"] = deepcopy(evidence)
    # Pass through bounded disposition labels, never arbitrary caller metadata.
    if "reason" in item:
        plan["reason"] = item["reason"]
    if "need" in item:
        plan["need"] = item["need"]
    return plan


__all__ = ["compile_item"]

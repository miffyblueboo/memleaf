"""Pure compiler for the staged incremental-items-v1 protocol.

No model calls, filesystem writes, operation allocation or old B3 repair occurs
here. Returned full-state proposals are NOT authorized writes. A future commit
integration must revalidate the bound source and target revisions under the
common mutation boundary, then freeze an operation before applying it.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping

from .incremental_dates import parse_source_time, selected_calendar, source_basis
from .models import Memory
from .scope_state import validate_scope_key
from .turn_plan import revision_digest
from .validation import ModelOutputError, parse_strict_json

PROTOCOL_VERSION = "incremental-items-v1"
MAX_BYTES = 128 * 1024
MAX_ITEMS = 64
_TYPES = frozenset(("fact", "todo", "preference", "project", "event", "identity", "other"))
_PATCH = frozenset(("title", "body", "scope", "status", "assignee", "waiting_on", "deadline", "validity"))
_CREATE = (_PATCH - {"validity"}) | {"type"}
_BRANCHES = {
    "CREATE": ({"memory"}, {"memory", "at", "effective"}),
    "UPDATE": ({"target", "patch"}, {"target", "patch", "at", "effective", "reopen"}),
    "NO_CHANGE": ({"target"}, {"target", "at"}),
    "NO_MEMORY": (set(), set()),
    "DEFERRED": ({"reason", "need"}, {"reason", "need"}),
}
_GROUP = {"status": "status", "validity": "validity", "scopes": "scope",
          "assignee": "responsibility", "waiting_on": "responsibility",
          "deadline": "deadline", "title": "content", "body": "content"}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _text(value: Any, maximum: int = 16384, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()) or len(value) > maximum or "\x00" in value:
        raise ValueError("invalid_text")
    return value


def _keys(value: Any, required: set[str], allowed: set[str] | frozenset[str]) -> None:
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - allowed:
        raise ValueError("invalid_fields")


def _ref(value: Any, table: Mapping[str, Any]) -> str:
    if not isinstance(value, str) or value.strip() not in table:
        raise ValueError("invalid_reference")
    return value.strip()


@dataclass(frozen=True)
class PlanningSnapshot:
    """An immutable canonical byte snapshot, not references to mutable Memory."""
    _payload: str

    @classmethod
    def build(cls, *, evidence: list[dict[str, Any]], targets: Mapping[str, Memory] | None = None,
              scopes: Mapping[str, str] | None = None, write_scopes: list[str] | None = None,
              writable: Mapping[str, bool] | None = None, request_kind: str = "automatic",
              allow_new_scopes: bool = False, context_complete: bool = True,
              native_targets: Mapping[str, dict[str, Any]] | None = None,
              native_guard: dict[str, Any] | None = None,
              retention_request: str | None = None) -> "PlanningSnapshot":
        if request_kind not in {"automatic", "explicit_remember"}:
            raise ValueError("invalid_request_kind")
        if type(allow_new_scopes) is not bool or type(context_complete) is not bool:
            raise ValueError("invalid_context_flags")
        if not isinstance(evidence, list) or not 1 <= len(evidence) <= MAX_ITEMS:
            raise ValueError("invalid_evidence")
        evidence = deepcopy(evidence)
        refs = set()
        for event in evidence:
            if not isinstance(event, dict) or not re.fullmatch(r"e[1-9][0-9]*", str(event.get("ref", ""))) or event["ref"] in refs:
                raise ValueError("invalid_evidence")
            refs.add(event["ref"])
            if event.get("use") not in {"new", "context"} or event.get("role") not in {"user", "assistant"}:
                raise ValueError("invalid_evidence")
            _text(event.get("text"), MAX_BYTES)
            for name in ("source", "session_id", "event_key"):
                _text(event.get(name), 800)
            parse_source_time(event.get("source_time"))
            seq = event.get("source_sequence")
            if seq is not None and (type(seq) is not int or seq < 0):
                raise ValueError("invalid_source_sequence")
        if not any(event["use"] == "new" for event in evidence):
            raise ValueError("missing_new_evidence")
        scopes = dict(scopes or {})
        for ref, scope in scopes.items():
            if not re.fullmatch(r"s[1-9][0-9]*", ref):
                raise ValueError("invalid_scope_reference")
            validate_scope_key(scope)
        if len(set(scopes.values())) != len(scopes):
            raise ValueError("duplicate_scope_reference")
        if write_scopes is not None:
            if not isinstance(write_scopes, list) or not write_scopes:
                raise ValueError("invalid_write_boundary")
            for scope in write_scopes:
                validate_scope_key(scope)
        native_targets = native_targets or {}
        if set(native_targets) - set(targets or {}):
            raise ValueError("invalid_native_binding")
        target_values = {}
        identities = set()
        targets = targets or {}
        if len(targets) > 20 or (writable and set(writable) - set(targets)):
            raise ValueError("invalid_targets")
        for ref, memory in targets.items():
            if not re.fullmatch(r"m[1-9][0-9]*", ref) or not isinstance(memory, Memory):
                raise ValueError("invalid_targets")
            if memory.memory_id.casefold() in identities:
                raise ValueError("duplicate_memory_id")
            identities.add(memory.memory_id.casefold())
            basis = memory.extra.get("field_basis", {})
            if not isinstance(basis, dict) or any(not isinstance(v, dict) for v in basis.values()):
                raise ValueError("invalid_target_basis")
            for item in basis.values():
                parse_source_time(item.get("source_time"))
            permitted = (writable or {}).get(ref, True)
            if type(permitted) is not bool:
                raise ValueError("invalid_writable")
            target_values[ref] = {"memory": memory.to_dict(), "revision": revision_digest(memory), "writable": permitted}
            if ref in native_targets:
                from .incremental_native import validate_binding
                validate_binding(native_targets[ref], native_guard, memory.memory_id)
                if hashlib.sha256(memory.body.encode("utf-8")).hexdigest() != native_targets[ref]["content_hash"]:
                    raise ValueError("invalid_native_content")
                if permitted:
                    raise ValueError("native_target_must_be_read_only")
                target_values[ref]["native"] = deepcopy(native_targets[ref])
        state = {"protocol_version": PROTOCOL_VERSION, "evidence": evidence, "targets": target_values,
                 "scopes": scopes, "write_scopes": write_scopes, "request_kind": request_kind,
                 "allow_new_scopes": allow_new_scopes, "context_complete": context_complete}
        if retention_request is not None:
            from .incremental_selection import validate_request
            if request_kind != "explicit_remember":
                raise ValueError("unexpected_retention_request")
            state["retention_request"] = validate_request(retention_request)
        if native_guard is not None:
            from .incremental_native import validate_guard
            validate_guard(native_guard)
            state["native_guard"] = deepcopy(native_guard)
        payload = _json(state)
        if len(payload.encode("utf-8")) > MAX_BYTES:
            raise ValueError("blocked_context")
        return cls(payload)

    @property
    def snapshot_id(self) -> str:
        state = self.state()
        # Read counters/formatting clocks are not business snapshot changes.
        state["targets"] = {ref: {"revision": item["revision"], "writable": item["writable"],
                                  **({"native": item["native"]} if "native" in item else {})}
                            for ref, item in state["targets"].items()}
        return hashlib.sha256(_json(state).encode("utf-8")).hexdigest()

    def state(self) -> dict[str, Any]:
        return json.loads(self._payload)

    def model_input(self) -> dict[str, Any]:
        state = self.state()
        reverse_scope = {v: k for k, v in state["scopes"].items()}
        memories = []
        for ref, target in state["targets"].items():
            memory = target["memory"]
            fields = {key: deepcopy(memory[key]) for key in (
                "type", "title", "body", "status", "validity", "assignee", "waiting_on",
                "due_date", "due_text", "due_status", "completed_at",
            ) if key in memory}
            values = memory.get("scopes", ["global"])
            fields["scope"] = reverse_scope.get(values[0], values[0]) if len(values) == 1 else values
            fields.update(ref=ref, writable=target["writable"])
            if "native" in target:
                fields.update(native=True, agent=target["native"]["agent"])
            fields["observed"] = {
                group: {key: value[key] for key in ("source_time", "source_sequence") if key in value}
                for group, value in memory.get("field_basis", {}).items()
            }
            # No real memory IDs, revisions, local paths, source lists or journals.
            memories.append(fields)
        return {
            "protocol_version": PROTOCOL_VERSION, "request_kind": state["request_kind"],
            **({"retention_request": state["retention_request"]} if "retention_request" in state else {}),
            "write_scopes": state["write_scopes"], "scopes": state["scopes"],
            "allow_new_scopes": state["allow_new_scopes"], "context_complete": state["context_complete"],
            "evidence": [{key: e[key] for key in ("ref", "use", "role", "text", "source_time", "source_sequence") if key in e}
                         for e in state["evidence"]], "memories": memories,
        }


def _selected(value: Any, evidence: Mapping[str, Any], refs: list[str], *, clear: bool = False) -> dict[str, Any]:
    if clear and isinstance(value, dict) and set(value) == {"ref", "clear"} and value["clear"] is True:
        ref = _ref(value["ref"], evidence)
        if ref not in refs or evidence[ref]["use"] != "new":
            raise ValueError("invalid_reference")
        return {"clear": True, "anchor": source_basis(evidence[ref])}
    _keys(value, {"ref", "text"}, {"ref", "text"})
    ref = _ref(value["ref"], evidence)
    if ref not in refs:
        raise ValueError("invalid_reference")
    return selected_calendar(value["text"], evidence[ref])


def _scope(value: Any, state: Mapping[str, Any]) -> str:
    value = _text(value, 160)
    if value in state["scopes"]:
        return state["scopes"][value]
    if value in {"global", "unscoped"}:
        return value
    if not state["allow_new_scopes"] or not value.startswith("project:"):
        raise ValueError("invalid_scope")
    return validate_scope_key(value)


def _parse_row(row: Any, state: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(row, dict) or not isinstance(row.get("action"), str):
        raise ValueError("invalid_action")
    action = row["action"].strip().upper()
    if action not in _BRANCHES:
        raise ValueError("invalid_action")
    required, optional = _BRANCHES[action]
    _keys(row, {"action", "evidence"} | required, {"action", "evidence"} | optional)
    evidence = {e["ref"]: e for e in state["evidence"]}
    if not isinstance(row["evidence"], list) or not 1 <= len(row["evidence"]) <= MAX_ITEMS:
        raise ValueError("invalid_reference")
    refs = list(dict.fromkeys(_ref(ref, evidence) for ref in row["evidence"]))
    new = [ref for ref in refs if evidence[ref]["use"] == "new"]
    if not new:
        raise ValueError("missing_new_evidence")
    result = {"action": action, "evidence": refs}
    if action in {"CREATE", "UPDATE", "NO_CHANGE"}:
        at = row.get("at", new[0] if len(new) == 1 else None)
        if at not in new:
            raise ValueError("invalid_at")
        result["basis"] = source_basis(evidence[at])
    if action in {"UPDATE", "NO_CHANGE"}:
        target = _ref(row["target"], state["targets"])
        result["target_ref"] = target
        if action == "UPDATE" and not state["targets"][target]["writable"]:
            raise ValueError("read_only_target")
    if action == "DEFERRED":
        if row["reason"] not in ("missing_identity", "missing_context", "conflict"):
            raise ValueError("invalid_reason")
        result.update(reason=row["reason"], need=_text(row["need"], 512))
    if action == "NO_MEMORY" and state["request_kind"] == "explicit_remember":
        raise ValueError("explicit_retention_required")
    if action in {"CREATE", "UPDATE"}:
        fields = deepcopy(row["memory"] if action == "CREATE" else row["patch"])
        _keys(fields, {"type", "scope", "title", "body"} if action == "CREATE" else set(),
              _CREATE if action == "CREATE" else _PATCH)
        if not fields:
            raise ValueError("empty_patch")
        kind = fields.get("type") if action == "CREATE" else state["targets"][result["target_ref"]]["memory"]["type"]
        if action == "CREATE" and (not isinstance(kind, str) or kind not in _TYPES):
            raise ValueError("invalid_type")
        if action == "CREATE" and kind == "todo" and "status" not in fields:
            raise ValueError("missing_status")
        if kind != "todo" and set(fields) & {"status", "assignee", "waiting_on", "deadline"}:
            raise ValueError("todo_fields_on_non_todo")
        for key in ("title", "body"):
            if key in fields:
                _text(fields[key], 256 if key == "title" else 16384,
                      empty=key == "body" and fields.get("validity") == "retracted")
        if "status" in fields and fields["status"] not in ("active", "completed", "cancelled"):
            raise ValueError("invalid_status")
        if "validity" in fields and fields["validity"] not in ("valid", "retracted"):
            raise ValueError("invalid_validity")
        for key in ("assignee", "waiting_on"):
            if key in fields and fields[key] is not None:
                _text(fields[key], 512)
        if "scope" in fields:
            fields["scopes"] = [_scope(fields.pop("scope"), state)]
        if "deadline" in fields:
            fields["deadline"] = _selected(fields["deadline"], evidence, refs, clear=action == "UPDATE")
        if "effective" in row:
            result["effective"] = _selected(row["effective"], evidence, refs)
        if "reopen" in row and (row["reopen"] is not True or fields.get("status") != "active"):
            raise ValueError("invalid_reopen")
        result["reopen"] = row.get("reopen", False)
        result["fields"] = fields
    return result


def _order(new: Mapping[str, Any], old: Mapping[str, Any]) -> int | None:
    # These fields prove observation order, not inferred business effectiveness.
    same_stream = all(new.get(k) is not None and new.get(k) == old.get(k) for k in ("source", "session_id"))
    if same_stream and type(new.get("source_sequence")) is int and type(old.get("source_sequence")) is int:
        a, b = new["source_sequence"], old["source_sequence"]
        return (a > b) - (a < b)
    a, b = parse_source_time(new.get("source_time")), parse_source_time(old.get("source_time"))
    if a is not None and b is not None:
        return (a > b) - (a < b)
    return None


def _compile_group(rows: list[dict[str, Any]], state: Mapping[str, Any]) -> dict[str, Any]:
    first = rows[0]
    target = state["targets"].get(first.get("target_ref"))
    old = deepcopy(target["memory"]) if target else {}
    fields: dict[str, Any] = {}
    bases: dict[str, dict[str, Any]] = {}
    effective = None
    for row in rows:
        for name, value in row.get("fields", {}).items():
            if name in fields and _json(fields[name]) != _json(value):
                raise ValueError("conflicting_target_patch")
            fields[name] = value
            group = _GROUP.get(name)
            if group:
                basis = row["basis"]
                current_basis = old.get("field_basis", {}).get(group, {})
                if isinstance(current_basis, dict) and _order(basis, current_basis) == -1:
                    raise ValueError("stale_observation")
                if group not in bases or _order(basis, bases[group]) == 1:
                    bases[group] = basis
        if "effective" in row:
            if effective is not None and effective != row["effective"]:
                raise ValueError("conflicting_effective_time")
            effective = row["effective"]
    if not target and not state["context_complete"]:
        raise ValueError("blocked_context")
    boundary = state["write_scopes"]
    chosen = fields.get("scopes", old.get("scopes", ["global"]))
    if boundary is not None and (set(chosen) - set(boundary) or (target and set(old["scopes"]) - set(boundary))):
        raise ValueError("blocked_scope")
    if old.get("status") in {"completed", "cancelled"} and fields.get("status") == "active":
        if not any(row.get("reopen") for row in rows):
            raise ValueError("explicit_reopen_required")
        if _order(bases.get("status", {}), old.get("field_basis", {}).get("status", {})) != 1:
            raise ValueError("unverified_reopen_time")
    if old.get("validity") == "retracted":
        if fields.get("validity") != "valid" or not fields.get("body"):
            raise ValueError("explicit_restore_required")
        if _order(bases.get("validity", {}), old.get("field_basis", {}).get("validity", {})) != 1:
            raise ValueError("unverified_restore_time")
    if fields.get("validity") == "retracted":
        if fields.get("body"):
            raise ValueError("retracted_body_must_be_empty")
        fields["body"] = ""
    state_change = bool(target and any(
        key in fields and fields[key] != old.get(key)
        for key in ("status", "assignee", "waiting_on", "validity", "scopes")
    )) or (not target and fields.get("status") in {"completed", "cancelled"})
    if effective is not None and state_change:
        anchor = parse_source_time(first["basis"].get("source_time"))
        if effective["date"] is None or anchor is None:
            raise ValueError("effective_time_unresolved")
        if effective["date"] > anchor.date().isoformat():
            raise ValueError("future_state_change")
    memory = deepcopy(old) if target else {"validity": "valid"}
    deadline = fields.pop("deadline", None)
    memory.update(fields)
    if target and memory.get("status") != old.get("status"):
        memory.pop("completed_at", None)  # Observation time never supplies completion time.
    warnings = []
    if deadline is not None:
        if deadline.get("clear"):
            memory.update(due_date=None, due_text=None, due_anchor=deadline["anchor"], due_status="cleared")
        else:
            memory.update(due_date=deadline["date"], due_text=deadline["text"], due_anchor=deadline["anchor"], due_status=deadline["status"])
            if deadline["status"] == "unresolved":
                warnings.append("deadline_unresolved")
    if effective is not None:
        memory["effective"] = effective
        if effective["status"] == "unresolved":
            warnings.append("effective_unresolved")
    memory["field_basis"] = {**old.get("field_basis", {}), **bases}
    if not target:
        memory["field_basis"].setdefault("content", first["basis"])
    result = {"action": first["action"], "evidence": list(dict.fromkeys(ref for row in rows for ref in row["evidence"])),
              "memory": memory, "warnings": warnings}
    if target:
        result.update(target=old["memory_id"], expected_revision=target["revision"])
    return result


def compile_incremental(raw: str, snapshot: PlanningSnapshot) -> dict[str, Any]:
    """Isolate row errors; one bad UPDATE blocks its whole target group only."""
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_BYTES:
        raise ValueError("invalid_response_size")
    try:
        value = parse_strict_json(raw)
    except (ModelOutputError, RecursionError) as error:
        raise ValueError("invalid_json") from error
    _keys(value, {"items"}, {"items"})
    if not isinstance(value["items"], list) or len(value["items"]) > MAX_ITEMS:
        raise ValueError("invalid_items")
    state = snapshot.state()
    known = {e["ref"] for e in state["evidence"]}
    new = {e["ref"] for e in state["evidence"] if e["use"] == "new"}
    parsed, issues, poisoned = [], [], set()
    def problem(index: int | None, code: str, row: Any) -> None:
        refs = row.get("evidence", []) if isinstance(row, dict) else []
        refs = [r.strip() for r in refs if isinstance(r, str) and r.strip() in known] if isinstance(refs, list) else []
        issues.append({"row": index, "code": code, "evidence": list(dict.fromkeys(refs))})
    for index, row in enumerate(value["items"]):
        try:
            parsed.append((index, _parse_row(row, state)))
        except (ValueError, TypeError) as error:
            problem(index, str(error) if isinstance(error, ValueError) else "invalid_fields", row)
            if isinstance(row, dict) and isinstance(row.get("target"), str) and row["target"].strip() in state["targets"]:
                poisoned.add(row["target"].strip())
    groups: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for index, row in parsed:
        groups[row.get("target_ref", f"row:{index}")].append((index, row))
    operations = []
    for key, members in groups.items():
        if key in poisoned:
            for index, row in members:
                problem(index, "invalid_target_group", row)
            continue
        updates = [row for _, row in members if row["action"] in {"CREATE", "UPDATE"}]
        if updates:
            try:
                operations.append(_compile_group(updates, state))
            except (ValueError, TypeError) as error:
                for index, row in members:
                    problem(index, str(error) if isinstance(error, ValueError) else "invalid_target_state", row)
                continue
        updated = next((op for op in reversed(operations) if op["action"] == "UPDATE"
                        and any(row.get("target_ref") in state["targets"]
                                and state["targets"][row["target_ref"]]["memory"]["memory_id"] == op.get("target")
                                for _, row in members)), None)
        if updated is not None:
            for _, row in members:
                if row["action"] == "NO_CHANGE":
                    updated["evidence"] = list(dict.fromkeys(updated["evidence"] + row["evidence"]))
        for index, row in members:
            if row["action"] in {"CREATE", "UPDATE"} or (updated is not None and row["action"] == "NO_CHANGE"):
                continue
            output = {k: v for k, v in row.items() if k not in {"target_ref", "basis"}}
            if "target_ref" in row:
                target = state["targets"][row["target_ref"]]
                output.update(target=target["memory"]["memory_id"], expected_revision=target["revision"])
                if "native" in target:
                    output["native"] = deepcopy(target["native"])
            operations.append(output)
    supported = {ref for op in operations if op["action"] in {"CREATE", "UPDATE", "NO_CHANGE"} for ref in op["evidence"]}
    for op in list(operations):
        if op["action"] == "NO_MEMORY" and supported.intersection(op["evidence"]):
            operations.remove(op)
            problem(None, "conflicting_disposition", op)
    covered = {ref for op in operations for ref in op["evidence"]} & new
    missing = sorted(new - covered - {r for issue in issues for r in issue["evidence"]})
    for ref in missing:
        issues.append({"row": None, "code": "unprocessed_evidence", "evidence": [ref]})
    deferred = {r for op in operations if op["action"] == "DEFERRED" for r in op["evidence"]}
    unresolved = sorted(new & (deferred | {r for issue in issues for r in issue["evidence"]}))
    return {"protocol_version": PROTOCOL_VERSION, "snapshot_id": snapshot.snapshot_id, "mode": "preview",
            "operations": operations, "issues": issues,
            "coverage": {"status": "partial" if issues or deferred else "complete", "unresolved_evidence": unresolved}}

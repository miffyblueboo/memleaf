"""Pure, bounded partial-recovery contracts; no model or write loop lives here.

A capsule exists only for newly produced partial decisions. Old stripped receipts
cannot be reconstructed. Repairs normalize two unambiguous container forms;
replanning requires actual new context and excludes already settled assertions.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from .incremental_protocol import PlanningSnapshot, MAX_ITEMS
from .models import Memory
from .validation import parse_strict_json

RECOVERY_SYSTEM = "\n本次仅重规划未结算的 new；settled 只读，不重出、不改写。新增前文和已有记忆仅帮助理解原事项，不据此新建无关事项。沿用 JSON 协议。\n"


def restore_snapshot(state: Any) -> PlanningSnapshot:
    if not isinstance(state, dict) or state.get("protocol_version") != "incremental-items-v1":
        raise ValueError("unsupported_partial_snapshot")
    targets = state.get("targets")
    if not isinstance(targets, dict):
        raise ValueError("invalid_partial_snapshot")
    try:
        return PlanningSnapshot.build(
            evidence=state["evidence"], targets={r: Memory.from_mapping(t["memory"]) for r, t in targets.items()},
            scopes=state["scopes"], write_scopes=state["write_scopes"],
            writable={r: t["writable"] for r, t in targets.items()},
            native_targets={r: t["native"] for r, t in targets.items() if "native" in t},
            native_guard=state.get("native_guard"), request_kind=state["request_kind"],
            retention_request=state.get("retention_request"),
            allow_new_scopes=state["allow_new_scopes"], context_complete=state["context_complete"],
            scope_guard=state.get("scope_guard"), scope_aliases=state.get("scope_aliases"),
        )
    except (KeyError, TypeError) as error:
        raise ValueError("invalid_partial_snapshot") from error


def seed(snapshot: PlanningSnapshot, response: str) -> dict[str, Any]:
    rows = parse_strict_json(response)["items"]
    return {"version": 1, "snapshot": snapshot.state(), "snapshot_id": snapshot.snapshot_id, "rows": rows}


def validate_basis(value: Any) -> None:
    if (not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 1
            or not isinstance(value.get("rows"), list) or len(value["rows"]) > MAX_ITEMS):
        raise ValueError("invalid_partial_basis")
    if restore_snapshot(value.get("snapshot")).snapshot_id != value.get("snapshot_id"):
        raise ValueError("invalid_partial_snapshot_digest")


def validate_recovery(run: dict[str, Any]) -> None:
    for key in ("recovery_seed", "partial_basis"):
        if key in run:
            validate_basis(run[key])
    if "partial_used" in run and type(run["partial_used"]) is not bool:
        raise ValueError("invalid_partial_recovery")
    recovery = run.get("partial_recovery")
    if recovery is None:
        if run.get("partial_used"):
            raise ValueError("invalid_partial_recovery")
        return
    if (not isinstance(recovery, dict) or type(recovery.get("version")) is not int or recovery["version"] != 1
            or not isinstance(recovery.get("mode"), str) or recovery["mode"] not in {"repair", "replan"}
            or not isinstance(recovery.get("parent_work_id"), str)
            or not recovery["parent_work_id"].startswith("inc-")
            or recovery["parent_work_id"] == run["commit_work_id"]
            or not isinstance(recovery.get("focus"), list) or not recovery["focus"]
            or any(not isinstance(r, str) for r in recovery["focus"])
            or len(set(recovery["focus"])) != len(recovery["focus"])
            or not isinstance(recovery.get("context_memory_ids"), list)
            or any(not isinstance(r, str) for r in recovery["context_memory_ids"])
            or len(recovery["context_memory_ids"]) > 20
            or not isinstance(recovery.get("repair_rows"), list)
            or any(type(i) is not int or not 0 <= i < MAX_ITEMS for i in recovery["repair_rows"])
            or run.get("partial_used") is not True):
        raise ValueError("invalid_partial_recovery")
    if run["status"] not in {"completed", "completed_with_unresolved", "blocked", "failed", "cancelled"}:
        if "partial_basis" not in run:
            raise ValueError("partial_recovery_basis_unavailable")
        original_new = {e["ref"] for e in run["partial_basis"]["snapshot"]["evidence"] if e["use"] == "new"}
        if not set(recovery["focus"]) <= original_new:
            raise ValueError("invalid_partial_recovery")


def unresolved_refs(work: dict[str, Any], state: dict[str, Any]) -> set[str]:
    new = {e["ref"] for e in state["evidence"] if e["use"] == "new"}
    refs = {r for issue in work["issues"] for r in issue.get("evidence", [])}
    refs.update(r for op in work["operations"] if op["state"] not in {"settled", "cancelled"}
                for r in op["evidence"])
    return new & refs


def isolated_focus(basis: dict[str, Any], work: dict[str, Any]) -> set[str]:
    """Do not split a failed target group across settled/shared source blocks."""
    state = basis["snapshot"]
    new = {e["ref"] for e in state["evidence"] if e["use"] == "new"}
    focus = unresolved_refs(work, state) - settled_refs(work)
    groups: dict[str, set[str]] = {}
    issue_rows = {i["row"] for i in work["issues"] if type(i.get("row")) is int}
    for index, row in enumerate(basis["rows"]):
        if not isinstance(row, dict):
            continue
        target = row.get("target")
        refs = row.get("evidence", [])
        refs = [refs] if isinstance(refs, str) else refs
        refs = {r.strip() for r in refs if isinstance(r, str)} & new if isinstance(refs, list) else set()
        key = target.strip() if isinstance(target, str) and target.strip() in state["targets"] else f"row:{index}"
        if index in issue_rows or row.get("action") == "DEFERRED" or refs & focus:
            groups.setdefault(key, set()).update(refs)
    while True:
        reduced = focus - set().union(*(refs for refs in groups.values() if refs & focus and refs - focus))
        if reduced == focus:
            return focus
        focus = reduced


def settled_refs(work: dict[str, Any]) -> set[str]:
    return {r for op in work["operations"] if op["state"] == "settled" for r in op["evidence"]}


def settled_ids(work: dict[str, Any]) -> set[str]:
    return {op["memory_id"] for op in work["operations"] if op["state"] == "settled" and op.get("memory_id")}


def normalized_rows(basis: dict[str, Any], work: dict[str, Any]) -> tuple[list[Any], bool, list[int]]:
    """Only exact-reference singleton containers change; never infer a value.

    No missing action, target, at or status is invented. Unknown fields/references
    and ambiguous target groups remain errors. Keep all failed group members.
    """
    issue_rows = {i["row"] for i in work["issues"] if type(i.get("row")) is int}
    state = basis["snapshot"]
    evidence = {e["ref"] for e in state["evidence"]}
    scopes = set(state["scopes"]) | {"global", "unscoped"}
    rows, changed, indices = [], False, []
    blocked_ids = {op.get("memory_id") for op in work["operations"] if op["state"] == "blocked"}
    for index, original in enumerate(basis["rows"]):
        row = deepcopy(original)
        action = row.get("action", "").strip().upper() if isinstance(row, dict) and isinstance(row.get("action"), str) else None
        target_ref = row.get("target") if isinstance(row, dict) else None
        target = state["targets"].get(target_ref) if isinstance(target_ref, str) else None
        if index not in issue_rows and action != "DEFERRED" and not (target and target["memory"]["memory_id"] in blocked_ids):
            continue
        if isinstance(row, dict):
            ref = row.get("evidence")
            if isinstance(ref, str) and ref.strip() in evidence:
                row["evidence"] = [ref.strip()]
                changed = True
            fields = row.get("memory" if action == "CREATE" else "patch")
            if isinstance(fields, dict):
                scope = fields.get("scope")
                if isinstance(scope, list) and len(scope) == 1 and isinstance(scope[0], str) and scope[0] in scopes:
                    fields["scope"] = scope[0]
                    changed = True
        rows.append(row)
        indices.append(index)
    return rows, changed, indices


def context_changed(original: PlanningSnapshot, current: PlanningSnapshot, work: dict[str, Any]) -> bool:
    """Exclude this work's own writes, reference reordering and hit counters.

    An actually added catalog record is new context, not proof of relevance.
    Relevance remains the one planner's task, within the fixed write boundary.
    """
    old, new = original.state(), current.state()
    old_e = {e["event_key"]: {k: v for k, v in e.items() if k != "ref"} for e in old["evidence"]}
    new_e = {e["event_key"]: {k: v for k, v in e.items() if k != "ref"} for e in new["evidence"]}
    from .incremental_scopes import guard_matches, applied_additions
    if (old_e != new_e or old.get("native_guard") != new.get("native_guard")
            or not guard_matches(new.get("scope_guard"), old.get("scope_guard"), applied_additions(work))):
        return True
    expected = {t["memory"]["memory_id"]: t["revision"] for t in old["targets"].values()}
    own_created = set()
    for op in work["operations"]:
        if op["state"] == "settled" and op["action"] in {"CREATE", "UPDATE"}:
            expected[op["memory_id"]] = op["replacement_revision"]
            if op["action"] == "CREATE":
                own_created.add(op["memory_id"])
    actual = {t["memory"]["memory_id"]: t["revision"] for t in new["targets"].values()}
    return (any(mid in actual and actual[mid] != rev for mid, rev in expected.items())
            or bool(set(actual) - set(expected) - own_created))


def recovery_snapshot(original: PlanningSnapshot, current: PlanningSnapshot, work: dict[str, Any],
                      focus: set[str], *, mode: str) -> PlanningSnapshot:
    old, new = original.state(), current.state()
    from .incremental_scopes import guard_matches, applied_additions
    if mode == "repair" and not guard_matches(new.get("scope_guard"), old.get("scope_guard"), applied_additions(work)):
        raise ValueError("partial_scope_context_changed")
    if (old["write_scopes"] != new["write_scopes"] or old["request_kind"] != new["request_kind"]
            or old.get("retention_request") != new.get("retention_request")
            or old["allow_new_scopes"] != new["allow_new_scopes"]):
        raise ValueError("partial_authorization_changed")
    # Pin the original reference-to-object bindings. Added context receives fresh
    # references; it never becomes a newly authorized assertion in this work.
    by_key = {e["event_key"]: e for e in new["evidence"]}
    old_refs = {e["event_key"]: e["ref"] for e in old["evidence"]}
    if len(by_key) != len(new["evidence"]) or len(old_refs) != len(old["evidence"]):
        raise ValueError("ambiguous_partial_source")
    next_ref = max(int(r[1:]) for r in old_refs.values()) + 1
    for e in old["evidence"]:
        existing = by_key.get(e["event_key"])
        if existing is None or {k: v for k, v in e.items() if k not in {"ref", "use"}} != {k: v for k, v in existing.items() if k not in {"ref", "use"}}:
            raise ValueError("partial_source_or_context_revised")
    evidence = []
    for e in new["evidence"]:
        e = deepcopy(e)
        ref = old_refs.get(e["event_key"])
        if ref is None:
            ref = f"e{next_ref}"; next_ref += 1
        e.update(ref=ref, use="new" if ref in focus else "context")
        evidence.append(e)
    old_targets = {t["memory"]["memory_id"]: r for r, t in old["targets"].items()}
    next_target = max((int(r[1:]) for r in old_targets.values()), default=0) + 1
    targets, writable, natives = {}, {}, {}
    readonly_ids = settled_ids(work)
    for t in new["targets"].values():
        mid = t["memory"]["memory_id"]
        ref = old_targets.get(mid)
        if ref is None:
            ref = f"m{next_target}"; next_target += 1
        targets[ref] = Memory.from_mapping(t["memory"])
        writable[ref] = t["writable"] and mid not in readonly_ids
        if "native" in t:
            natives[ref] = t["native"]
    scopes = dict(old["scopes"])
    counter = max((int(r[1:]) for r in scopes), default=0) + 1
    for value in new["scopes"].values():
        if value not in scopes.values():
            scopes[f"s{counter}"] = value; counter += 1
    if mode == "repair":
        before_e = [{k: v for k, v in e.items() if k != "ref"} for e in old["evidence"]]
        now_e = [{k: v for k, v in e.items() if k != "ref"} for e in new["evidence"]]
        if before_e != now_e:
            raise ValueError("repair_context_changed")
        if old.get("native_guard") != new.get("native_guard"):
            raise ValueError("repair_context_changed")
        # A structural repair may not silently adapt any referenced target's
        # earlier business decision to a different complete target snapshot.
        for row in work.get("_repair_rows", []):
            if not isinstance(row, dict) or not isinstance(row.get("target"), str):
                continue
            ref = row["target"].strip()
            if ref in old["targets"] and (ref not in targets or
                    old["targets"][ref]["revision"] != new_revision(targets[ref])):
                raise ValueError("repair_target_changed")
    return PlanningSnapshot.build(evidence=evidence, targets=targets, scopes=scopes,
                                  write_scopes=old["write_scopes"], writable=writable,
                                  native_targets=natives, native_guard=new.get("native_guard"),
                                  request_kind=old["request_kind"], retention_request=old.get("retention_request"),
                                  allow_new_scopes=old["allow_new_scopes"], context_complete=new["context_complete"],
                                  scope_guard=new.get("scope_guard"), scope_aliases=new.get("scope_aliases"))


def new_revision(memory: Memory) -> str:
    from .turn_plan import revision_digest
    return revision_digest(memory)


def recovery_input(snapshot: PlanningSnapshot, work: dict[str, Any], *, mode: str) -> dict[str, Any]:
    value = snapshot.model_input()
    state = snapshot.state()
    refs = {t["memory"]["memory_id"]: r for r, t in state["targets"].items()}
    value["recovery"] = {"mode": mode, "settled": [
        {"action": op["action"], "evidence": op["evidence"],
         **({"target": refs[op["memory_id"]]} if op.get("memory_id") in refs else {})}
        for op in work["operations"] if op["state"] == "settled"]}
    return value


def cumulative_result(compiled: dict[str, Any], parent: dict[str, Any], snapshot: PlanningSnapshot,
                      focus: set[str], *, repair_rows: list[int] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Carry immutable accepted operation receipts, not their old write payloads.

    The child work is a cumulative receipt. Parent decisions remain unchanged;
    only unresolved items with a definite, wholly selected source binding are
    replaced. Unlocated and shared-block errors remain visible.
    """
    original_new = {e["ref"] for e in parent["evidence"] if e["use"] == "new"}
    def selected(item):
        refs = set(item.get("evidence", [])) & original_new
        return bool(refs) and refs <= focus
    inherited = [deepcopy(op) for op in parent["operations"]
                 if op["state"] in {"settled", "cancelled"} or not selected(op)]
    issues = [deepcopy(i) for i in parent["issues"]
              if not selected(i) and not (repair_rows is not None and type(i.get("row")) is int and i["row"] in repair_rows)]
    issues.extend({**i, "recovery_round": 1} for i in compiled["issues"])
    proposals = []
    settled = settled_refs(parent)
    ids = settled_ids(parent)
    for op in compiled["operations"]:
        if ((op["action"] == "NO_MEMORY" and settled.intersection(op["evidence"]))
                or (op["action"] == "UPDATE" and op.get("target") in ids)):
            issues.append({"row": None, "code": "settled_decision_read_only", "evidence": op["evidence"], "recovery_round": 1})
        else:
            proposals.append(op)
    return inherited, proposals, issues

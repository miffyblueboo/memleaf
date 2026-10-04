"""Bounded semantic veto for proposed actions, grounded in original input.

Review can reject a draft or omit unsupported optional fields. It cannot invent
facts, targets, references or grant authority. Both calls share the existing
request allowance; a missing or malformed review never commits the draft.
"""
from __future__ import annotations

from copy import deepcopy
import json

from .validation import parse_strict_json, ModelOutputError

REVIEW_SYSTEM = """核验拟写入的持续行动。original_input 是唯一事实和权限来源；draft 是待审候选，不能当作事实。不要重写 items。返回 JSON 对象，唯一键 decisions，值为数组；每项有 item（整数）、keep（布尔值）、field_support（对象）。按输入 fields_to_review 的实际字段填 field_support，不能复制其他事项的字段名。
每个 draft.items 都必须有且只有一个 decision，item 是从 0 开始的位置。keep 判断该事项本身是否由原文确立且持续值得保留。临时操作及助手额外建议不能产生用户长期待办，原话只证明另一个操作时 keep:false。独立事实不因其他候选有误而拒绝。
keep:true 时，field_support 必须逐个列出候选 memory/patch 中的业务字段，每个布尔值判断该字段的拟议值是否受来源支持；不列 responsibility_basis、effective、at、reopen 等引用/控制字段。NO_CHANGE/DEFERRED/NO_MEMORY 的 field_support 为 {}。keep:false 时 field_support 为 {}。unverified_fields 是系统发现未能验证引用的可选责任字段，必须判为 false；不能补造引用。这不影响其他有依据的事实或任务。归属/客户尚未确定不自动表示行动被阻塞。
不能仅检查引文存在：必须判断原话是否确立该具体行动、责任、状态和期限。assistant 的建议/计划不能赋予 user 新义务。转发、协调、记录不等于本人执行；支持旧任务不等于接受新安排。
尤其检查拟改 deadline：依赖条件、交接执行人、进度、尚未完成都不证明原期限已取消；只有明确取消/替换这个期限才支持变更。没有明确期限变化时 deadline:false，原期限沿用。waiting_on 与实际阻塞一致，不以正文代替字段。
新引用只能来自 new；context 和旧目标只供比较，不能重新推动旧变化。候选若仅重新陈述旧目标已覆盖的内容，不应作为新 UPDATE。同一事项补充/纠正维护原目标，独立事项允许 CREATE，不能凭标题相似合并。
逐字段判断后输出 decisions；不得添加字段、事实、引用或目标。不要输出解释或其他键。
"""

_METADATA = {"responsibility_basis", "effective", "at", "reopen"}
_REQUIRED = {"CREATE": {"type", "scope", "title", "body"}, "UPDATE": {"title", "body"}}


def envelope(value):
    # A single action has one unambiguous container. Its fields are still
    # checked by the original row compiler; mixed envelopes remain invalid.
    if isinstance(value, dict) and "action" in value and "items" not in value:
        return {"items": [value]}
    return value


def fields(row):
    if not isinstance(row, dict):
        return set()
    action = row.get("action")
    action = action.strip().upper() if isinstance(action, str) else ""
    from .incremental_protocol import _normalize_flat_create_row
    row, _ = _normalize_flat_create_row(row)
    payload = row.get("memory" if action == "CREATE" else "patch", {})
    return {key for key in payload if key not in _METADATA} if isinstance(payload, dict) else set()


def needs_review(compiled, response=None):
    if response is not None:
        rows = envelope(parse_strict_json(response))["items"]
        for row in rows:
            if not isinstance(row, dict):
                continue
            action = row.get("action")
            action = action.strip().upper() if isinstance(action, str) else ""
            payload = row.get("memory") if action == "CREATE" else row.get("patch")
            if isinstance(payload, dict) and (
                    action == "CREATE" and payload.get("type") in {"todo", "event"}
                    or any(k in payload for k in ("deadline", "assignee", "waiting_on")) and any(payload.get(k) is not None for k in ("deadline", "assignee", "waiting_on"))):
                return True
    return any(op["action"] in {"CREATE", "UPDATE"}
               and ((op["action"] == "CREATE" and op.get("memory", {}).get("type") in {"todo", "event"})
                    or ((op.get("memory", {}).get("type") == "todo" or op.get("memory", {}).get("actionable"))
                        and op.get("memory", {}).get("status", "active") == "active"))
               for op in compiled["operations"])


def unverified_fields(row, original):
    """Missing proof cannot borrow a model verdict as source metadata."""
    if not isinstance(row, dict):
        return []
    action = row.get("action")
    action = action.strip().upper() if isinstance(action, str) else ""
    payload = row.get("memory" if action == "CREATE" else "patch", {})
    if not isinstance(payload, dict):
        return []
    citations = {}
    for owner in (row, payload):
        basis = owner.get("responsibility_basis", {})
        if isinstance(basis, dict):
            for name, selection in basis.items():
                if name in citations and citations[name] != selection:
                    return []  # A conflict stays an error in the original compiler.
                citations[name] = selection
    missing = []
    for name in ("assignee", "waiting_on"):
        if payload.get(name) is None:
            continue
        selection = citations.get(name)
        if selection is None:
            missing.append(name)
        # Malformed or false selections are conflicts, not omitted values.
        # The unchanged compiler continues to reject them.
    return missing


def review_request(original, response, snapshot=None):
    source = json.loads(original["user"])
    draft = envelope(parse_strict_json(response))
    from .incremental_protocol import _normalize_model_row
    state = (snapshot.state() if snapshot is not None else {
        "request_kind": source.get("request_kind", "automatic"), "evidence": [],
        "targets": {m["ref"]: {"memory": m} for m in source.get("memories", [])}})
    rows = []
    invalid = []
    for i, row in enumerate(draft["items"]):
        try:
            row, _ = _normalize_model_row(row, state, only_row=len(draft["items"]) == 1)
        except (ValueError, TypeError):
            # Preserve the compiler's row/group rejection. A veto cannot pick
            # one side of conflicting annotations or poison unrelated rows.
            invalid.append(i)
            row = deepcopy(row)
        if isinstance(row, dict) and isinstance(row.get("action"), str):
            row["action"] = row["action"].strip().upper()
        rows.append(row)
    draft["items"] = rows
    return {"system": REVIEW_SYSTEM,
            "user": json.dumps({"original_input": source, "draft": draft,
                                "fields_to_review": [sorted(fields(row)) for row in draft["items"]],
                                "unverified_fields": [unverified_fields(row, source) for row in draft["items"]],
                                **({"invalid_representation_items": invalid} if invalid else {})},
                               ensure_ascii=False, separators=(",", ":"))}


def apply_review(request, response):
    """Only subtract unapproved proposals; never synthesize business values."""
    try:
        data = json.loads(request["user"])
        draft = deepcopy(data["draft"])
        review = parse_strict_json(response)
        rows = draft["items"]
        decisions = review["decisions"]
        if set(review) != {"decisions"} or not isinstance(decisions, list) or len(decisions) != len(rows):
            raise ValueError
        positions = set()
        for decision in decisions:
            if not isinstance(decision, dict) or set(decision) != {"item", "keep", "field_support"}:
                raise ValueError
            i = decision["item"]
            if type(i) is not int or not 0 <= i < len(rows) or i in positions or type(decision["keep"]) is not bool:
                raise ValueError
            positions.add(i)
            support = decision["field_support"]
            if not isinstance(support, dict) or any(type(v) is not bool for v in support.values()):
                raise ValueError
            row = rows[i]
            if decision["keep"] and (not isinstance(row, dict) or not isinstance(row.get("action"), str)):
                raise ValueError
            expected = fields(row) if decision["keep"] else set()
            if set(support) != expected:
                raise ValueError
            if i in data.get("invalid_representation_items", []):
                continue
            for name in data.get("unverified_fields", [[] for _ in rows])[i]:
                if name in support:
                    support[name] = False
            if isinstance(row, dict) and row.get("action") in {"NO_CHANGE", "NO_MEMORY", "DEFERRED"}:
                continue  # Review cannot erase an unresolved control disposition.
            if not decision["keep"] or any(not support.get(k, True) for k in _REQUIRED.get(row["action"].upper(), set())):
                rows[i] = None
                continue
            payload = row.get("memory" if row["action"].upper() == "CREATE" else "patch", {})
            unverified = data.get("unverified_fields", [[] for _ in rows])[i]
            if row["action"].upper() == "UPDATE" and unverified:
                current = next((m for m in data["original_input"].get("memories", [])
                                if m.get("ref") == row.get("target")), {})
                if any(payload.get(name) != current.get(name) for name in unverified):
                    # Inheriting an old executor beside a new contradictory body
                    # would create an inconsistent head. Retain its old state
                    # and expose the unresolved change instead.
                    rows[i] = {"action": "DEFERRED", "evidence": row["evidence"],
                               "reason": "missing_context",
                               "need": "The proposed responsibility change has no verified field citation."}
                    continue
            for name, accepted in support.items():
                if not accepted:
                    payload.pop(name, None)
                    for owner in (row, payload):
                        if isinstance(owner.get("responsibility_basis"), dict):
                            owner["responsibility_basis"].pop(name, None)
                            if not owner["responsibility_basis"]:
                                owner.pop("responsibility_basis")
                        # A discarded optional responsibility may have an
                        # unrecognized citation annotation. Drop only its exact
                        # reference-shaped wrapper, never arbitrary extensions
                        # or a proof for a value that remains in the candidate.
                        if name in {"assignee", "waiting_on"}:
                            annotation = owner.get(name + "_basis")
                            if (isinstance(annotation, dict) and set(annotation) == {"ref", "text"}
                                    and all(isinstance(v, str) for v in annotation.values())):
                                owner.pop(name + "_basis")
                    if name == "deadline":
                        row.pop("deadline_decision", None)
            if row["action"].upper() == "UPDATE" and not payload:
                rows[i] = None
        rows = [r for r in rows if r is not None]
        if not rows:
            original = data["original_input"]
            if original.get("request_kind") == "explicit_remember":
                refs = [e["ref"] for e in original["evidence"] if e["use"] == "new"]
                rows = [{"action": "DEFERRED", "evidence": refs, "reason": "missing_context",
                         "need": "The requested retention has no source-supported durable candidate."}]
            else:
                rows = [{"action": "NO_MEMORY"}]
        return json.dumps({"items": rows}, ensure_ascii=False, separators=(",", ":"))
    except (KeyError, TypeError, ValueError, ModelOutputError) as error:
        raise ValueError("invalid_semantic_review") from error

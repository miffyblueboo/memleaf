"""Source-bound review and maintenance in the existing request allowance.

Review may reject fields or repair maintenance on known writable targets. It
cannot invent facts, targets, references or grant authority. A missing or
malformed review never commits the draft.
"""
from __future__ import annotations

from copy import deepcopy
import json

from .validation import parse_strict_json, ModelOutputError
from .body_preservation import omissions, covered

REVIEW_SYSTEM = """核验拟写入的记忆及持续行动。original_input 是唯一事实和权限来源；draft 是待审候选，不能当作事实。返回 JSON 对象，必填 decisions，值为数组；每项有 item（整数）、keep（布尔值）、field_support（对象）。按输入 fields_to_review 的实际字段填 field_support，不能复制其他事项的字段名。
存在 body_omissions 时，keep:true 的 UPDATE/MERGE 必须逐项说明省略的去向；仅 body:true 不足以通过。body_omissions 按 item 列出拟议正文中不再原样出现的旧片段（比较辅助，不表示已经失效）。逐项核对完整含义，不能因主题相同、进展重复或用户未再次提及就删除。需要补回或修正文句时，直接在 decision 增加 body（修正后的完整正文字符串），field_support.body=true；不必复制 action、target、evidence，也不要把该 target 放入 updates。Core 沿用该候选的身份、证据和其他字段；已有 replace 方式仍可用，但不能同时使用 body 和 replace。对最终 UPDATE/MERGE（包括 body、replace 和 updates 修正）的剩余省略片段，在对应 decision/updates 项增加 body_coverage 数组：每项 {"target":"旧目标ref","old":"旧片段原文","body":"最终正文中完整承接该片段含义的逐字引文"}，或 {"target":"旧目标ref","old":"旧片段原文","source":{"ref":"new用户证据ref","text":"明确纠正/撤回该内容的逐字引文"}}。body 引文必须完整等价且真实存在于最终正文；不能只引用旧目标中的文字却忘了将它写入修正正文。source 不能引用旧上下文或助手建议。修正后原样保留的片段无需列入。MERGE 对所有被合并目标逐项核对，撤回整个目标也需明确用户依据。无法确认则 keep:false；不要猜测或提供主题相符但实际无关的引文。没有剩余省略时省略 body_coverage。
每个 draft.items 都必须有且只有一个 decision，item 是从 0 开始的位置。keep 判断该事项本身是否由原文确立且持续值得保留。临时操作及助手额外建议不能产生用户长期待办，原话只证明另一个操作时 keep:false。独立事实不因其他候选有误而拒绝。
keep:true 时，field_support 必须逐个列出候选 memory/patch 中的业务字段，每个布尔值判断该字段的拟议值是否受来源支持；不列 responsibility_basis、effective、at、reopen 等引用/控制字段。没有 replace 的 NO_CHANGE/DEFERRED/NO_MEMORY 的 field_support 为 {}。keep:false 时 field_support 为 {}。unverified_fields 是系统发现未能验证引用的可选责任字段，必须判为 false；不能补造引用。这不影响其他有依据的事实或任务。归属/客户尚未确定不自动表示行动被阻塞。
不能仅检查引文存在：必须判断原话是否确立该具体行动、责任、状态和期限。assistant 的建议/计划不能赋予 user 新义务。转发、协调、记录不等于本人执行；支持旧任务不等于接受新安排。
body 必须逐个事实分句核验；assistant 自行增加的原因、影响判断、条件或后续步骤不受 user 原话支持，不能因整段主题相符就判 true。剔除无依据分句，保留其余已确认事实，可用 replace 修正 CREATE 的 title/body；没有修正时 body:false。title 只标识事项，交期/发生日/进度放正文与结构字段；名称固有的年度等身份信息保留。UPDATE/NO_CHANGE 的旧标题仍含交期时，在现有 replace/updates 中一起维护。
尤其检查拟改 deadline：依赖条件、交接执行人、进度、尚未完成都不证明原期限已取消；只有明确取消/替换这个期限才支持变更。没有明确期限变化时 deadline:false，原期限沿用。卡点和等待条件仅在正文中维护，并清除已经解决的旧卡点。
新引用只能来自 new；context 和旧目标只供比较，不能重新推动旧变化。候选若仅重新陈述旧目标已覆盖的内容，不应作为新 UPDATE。同一事项补充/纠正维护原目标，独立事项允许 CREATE，不能凭标题相似合并。
逐字段判断后输出 decisions；对共同事实变更，检查所有候选记忆中是否仍有旧职责或旧状态，不能认可只更新一条而遗留冲突。确认 MERGE 各方确为同一事项且完整保留有效内容；独立生命周期的项目/任务不可合并。
修正遗漏时，decision 可增加 replace（同 target 的完整 UPDATE，或原 CREATE 仅改 title/body 的完整副本），field_support 判断替换后的字段；CREATE 修正不得改变其 evidence、scope、type、责任、期限或引用。不得替换 DEFERRED，也不得新增义务或猜测未知。还可输出 updates:[{"row":UPDATE,"field_support":{...}}] 更新原 draft 未涉及的可写候选目标，证据仍只取原输入 new。没有修正就省略 replace/updates。每条修正必须保留仍有效的旧事实与原期限；撤回用 validity=retracted。不要输出解释。
"""

_METADATA = {"responsibility_basis", "effective", "at", "reopen"}
_REQUIRED = {"CREATE": {"type", "scope", "title", "body"}, "UPDATE": {"title", "body"}, "MERGE": {"body"}}


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


def needs_review(compiled, response=None, snapshot=None):
    if response is not None:
        rows = envelope(parse_strict_json(response))["items"]
        original = snapshot.model_input() if snapshot is not None else None
        update_ids = {op.get("target") for op in compiled["operations"] if op["action"] == "UPDATE"}
        writable_updates = ({ref for ref, target in snapshot.state()["targets"].items()
                             if target["memory"]["memory_id"] in update_ids} if snapshot is not None else set())
        actions = {str(row.get("action", "")).strip().upper() for row in rows if isinstance(row, dict)}
        if "MERGE" in actions or {"UPDATE", "NO_CHANGE"} <= actions:
            return True
        for row in rows:
            if not isinstance(row, dict):
                continue
            target = row.get("target")
            if (original is not None and isinstance(target, str) and target.strip() in writable_updates
                    and omissions(row, original)):
                return True
            action = row.get("action")
            action = action.strip().upper() if isinstance(action, str) else ""
            payload = row.get("memory") if action == "CREATE" else row.get("patch")
            if isinstance(payload, dict) and (
                    action == "CREATE" and payload.get("type") in {"todo", "event"}
                    or any(k in payload for k in ("deadline", "assignee")) and any(payload.get(k) is not None for k in ("deadline", "assignee"))):
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
    for name in ("assignee",):
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
    missing = [omissions(row, source) for row in rows]
    return {"system": REVIEW_SYSTEM,
            "user": json.dumps({"original_input": source, "draft": draft,
                                "fields_to_review": [sorted(fields(row)) for row in draft["items"]],
                                "unverified_fields": [unverified_fields(row, source) for row in draft["items"]],
                                **({"body_omissions": missing} if any(missing) else {}),
                                **({"invalid_representation_items": invalid} if invalid else {})},
                               ensure_ascii=False, separators=(",", ":"))}


def _maintenance_update(row, support, original):
    """No arbitrary new target or CREATE is authorized by a review correction."""
    if (not isinstance(row, dict) or row.get("action") != "UPDATE"
            or not isinstance(row.get("patch"), dict) or not row["patch"]
            or not isinstance(support, dict) or set(support) != fields(row)
            or any(value is not True for value in support.values())):
        raise ValueError("invalid_maintenance_correction")
    target = next((m for m in original.get("memories", []) if m.get("ref") == row.get("target")), None)
    new = {e["ref"] for e in original["evidence"] if e["use"] == "new"}
    if (target is None or target.get("writable") is not True
            or not isinstance(row.get("evidence"), list) or not set(row["evidence"]) & new
            or unverified_fields(row, original)):
        raise ValueError("invalid_maintenance_correction")
    return deepcopy(row)


def apply_review(request, response):
    """Review proposals and source-bound maintenance in the same existing pass."""
    try:
        data = json.loads(request["user"])
        draft = deepcopy(data["draft"])
        review = parse_strict_json(response)
        rows = draft["items"]
        decisions = review["decisions"]
        if set(review) - {"decisions", "updates"} or not isinstance(decisions, list) or len(decisions) != len(rows):
            raise ValueError
        positions = set()
        rejected = []
        def discard(row):
            # A rejected maintenance proposal is not proof that the source
            # needs no change. Keep its unresolved disposition even when other
            # candidates survive the review. Ordinary rejected automatic
            # CREATEs can still mean there was no durable fact to retain.
            if (row.get("action") in {"UPDATE", "MERGE"}
                    or data["original_input"].get("request_kind") == "explicit_remember"):
                rejected.append(deepcopy(row))
        for decision in decisions:
            if (not isinstance(decision, dict) or not {"item", "keep", "field_support"} <= set(decision)
                    or set(decision) - {"item", "keep", "field_support", "replace", "body", "body_coverage"}):
                raise ValueError
            i = decision["item"]
            if type(i) is not int or not 0 <= i < len(rows) or i in positions or type(decision["keep"]) is not bool:
                raise ValueError
            positions.add(i)
            support = decision["field_support"]
            if not isinstance(support, dict) or any(type(v) is not bool for v in support.values()):
                raise ValueError
            row = rows[i]
            if "body" in decision:
                if (not decision["keep"] or "replace" in decision
                        or i in data.get("invalid_representation_items", [])
                        or row.get("action") not in {"CREATE", "UPDATE", "MERGE"}
                        or "body" not in fields(row) or support.get("body") is not True
                        or not isinstance(decision["body"], str)):
                    raise ValueError
                row["memory" if row["action"] == "CREATE" else "patch"]["body"] = decision["body"]
            if "replace" in decision:
                if i in data.get("invalid_representation_items", []) or not decision["keep"]:
                    raise ValueError
                if row.get("action") == "CREATE":
                    replacement = deepcopy(decision["replace"])
                    if (not isinstance(replacement, dict) or replacement.get("action") != "CREATE"
                            or not isinstance(replacement.get("memory"), dict)
                            or not isinstance(row.get("memory"), dict)
                            or set(support) != fields(replacement)
                            or any(v is not True for v in support.values())):
                        raise ValueError
                    # Review can repair prose, not rewrite the draft's source,
                    # authority, identity, lifecycle or field annotations.
                    def without_prose(value):
                        value = deepcopy(value)
                        for key in ("title", "body"):
                            value["memory"].pop(key, None)
                        return value
                    if without_prose(row) != without_prose(replacement):
                        raise ValueError
                elif row.get("action") in {"UPDATE", "NO_CHANGE"}:
                    replacement = _maintenance_update(decision["replace"], support, data["original_input"])
                    if replacement["target"] != row["target"]:
                        raise ValueError
                else:
                    raise ValueError
                row = rows[i] = replacement
                data.get("unverified_fields", [[] for _ in rows])[i] = []
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
                discard(row)
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
                        if name in {"assignee"}:
                            annotation = owner.get(name + "_basis")
                            if (isinstance(annotation, dict) and set(annotation) == {"ref", "text"}
                                    and all(isinstance(v, str) for v in annotation.values())):
                                owner.pop(name + "_basis")
                    if name == "deadline":
                        row.pop("deadline_decision", None)
            if row["action"].upper() == "UPDATE" and not payload:
                discard(row)
                rows[i] = None
            elif not covered(row, data["original_input"], decision.get("body_coverage")):
                discard(row)
                rows[i] = None
        rows = [r for r in rows if r is not None]
        additions = review.get("updates", [])
        from .incremental_protocol import MAX_ITEMS
        if not isinstance(additions, list) or len(rows) + len(additions) > MAX_ITEMS:
            raise ValueError
        used = {row.get("target") for row in rows if isinstance(row, dict)}
        for entry in additions:
            try:
                if (not isinstance(entry, dict) or not {"row", "field_support"} <= set(entry)
                        or set(entry) - {"row", "field_support", "body_coverage"}):
                    raise ValueError
                row = _maintenance_update(entry["row"], entry["field_support"], data["original_input"])
                if row["target"] in used:
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                # Optional correction is not an authority grant. Reject it
                # explicitly without erasing independently valid decisions.
                rows.append({"action": "DEFERRED", "evidence": [e["ref"] for e in data["original_input"]["evidence"]
                             if e["use"] == "new"], "reason": "missing_context",
                             "need": "The additional maintenance correction is not source-verified."})
                continue
            if covered(row, data["original_input"], entry.get("body_coverage")):
                rows.append(row)
            else:
                discard(row)
            used.add(row["target"])
        for rejected_row in rejected:
            # The same review can supply a complete, independently validated
            # correction for this target. An unrelated NO_CHANGE cannot cover
            # a rejected proposal, nor can UPDATE stand in for a rejected MERGE.
            if rejected_row.get("action") == "UPDATE" and any(
                    row.get("action") == "UPDATE" and row.get("target") == rejected_row.get("target")
                    and set(rejected_row.get("evidence", [])) <= set(row.get("evidence", []))
                    and fields(rejected_row) <= fields(row)
                    for row in rows):
                continue
            rows.append({"action": "DEFERRED", "evidence": rejected_row.get("evidence", []),
                         "reason": "missing_context",
                         "need": "The proposed retention or maintenance was rejected by source review."})
        if len(rows) > MAX_ITEMS:
            raise ValueError
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

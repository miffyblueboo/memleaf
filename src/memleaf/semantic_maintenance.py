"""Maintain scoped memory state from small, already evidence-bound deltas."""
from __future__ import annotations
import json
from typing import Any, Mapping
from .semantic_protocol import RETENTION_GUIDANCE, expand_fragments, _invalid
from .validation import ModelOutputError, parse_strict_json

MAINTENANCE_SYSTEM = RETENTION_GUIDANCE + "\n" + '''根据 incoming 引用的原始证据维护长期记忆；incoming 仅划定待复核的主题，不提供已确认的分类或归属，其中已有的 due_date 是 Core 按证据和 reference_time 校验过的期限，须原样用于 due_date 和正文。catalog 是可更新的已有记忆。先识别已有事项的状态变化，再判断新建价值。同一事项合并维护当前状态，重复不新建；完成或转交维护原记忆，不另建完成事实留下旧行动。
返回 JSON {"memories":[{"from":["d1"],"target":null,"title":"简短主题","body":"最小可复用核心","type":"fact","scope":"global"}],"discard":[],"deferred":[]}。
每条一个独立主体与用途，仅不同生命周期时拆分。scope 按证据独立确定为 project:主体名、global（通用原则）或 unscoped（归属未确定）。type 为 fact/preference/project/todo/event/identity/other。target 为同事项的 catalog 真实ID，无才为null；更新保留原type，明确归属纠正可以改变scope。独立可跟进行动不论 type 可设 actionable:true、status（active/completed/cancelled）、assignee、waiting_on 和 due_date（无则null）；旧 todo 隐含 actionable。reference_time 是当前会话时间；按原始约定把可换算的相对期限写成 YYYY-MM-DD，范围取最晚完成日，正文同步使用该日期。无法可靠换算则 due_date:null，但保留核心记忆。正文保留当前有效内容，去掉过时状态和无复用价值的细节。每个 incoming 用 from、discard 或 deferred 覆盖；from 表示该主题已完整复核。仅拆分同一 incoming 时填写 evidence:[fragments.id] 来绑定各自保留内容，否则省略 evidence。所有引用只能选输入中已有的编号。'''


def maintenance_input(
    raw: str,
    fragments: list[dict[str, Any]],
    catalog: list[dict[str, Any]],
    model_data: Mapping[str, Any],
    validated: Mapping[str, Any] | None = None,
):
    # Validate exact references before they become trusted input to maintenance.
    expand_fragments(raw, fragments)
    original = parse_strict_json(raw)
    incoming = {}
    proposals = []
    catalog_ids = {m['memory_id'].casefold() for m in catalog}
    validated_by_id = {
        item['candidate_id']: item
        for item in (validated.get('items', []) if isinstance(validated, Mapping) else [])
        if isinstance(item, Mapping) and isinstance(item.get('candidate_id'), str)
    }
    for index, row in enumerate(original['memories'], 1):
        incoming[index] = dict(row)
        proposal = {'id': f'd{index}', 'evidence': row['evidence']}
        accepted = validated_by_id.get(f'c{index}')
        accepted_memory = accepted.get('memory') if isinstance(accepted, Mapping) else None
        accepted_due_date = accepted_memory.get('due_date') if isinstance(accepted_memory, Mapping) else None
        if isinstance(accepted_due_date, str) and accepted_due_date:
            proposal['due_date'] = accepted_due_date
            incoming[index]['_confirmed_due_date'] = accepted_due_date
        if isinstance(row.get('target'), str) and row['target'].casefold() in catalog_ids:
            proposal['target'] = row['target']
        proposals.append(proposal)
    related = [m for m in catalog if len(m.get('scopes', [])) == 1]
    # Show original evidence, not the draft's classifications: the reviewer
    # must be able to correct ownership and value without inheriting them.
    snippets = list(model_data['fragments'])
    payload = {'incoming': proposals, 'catalog': related, 'fragments': snippets}
    if isinstance(model_data.get('reference_time'), str) and model_data['reference_time']:
        payload['reference_time'] = model_data['reference_time']
    return payload, (original, incoming, {m['memory_id'].casefold():m for m in related})


def expand_maintenance(raw: str, context, *, diagnostics=None) -> str:
    value = parse_strict_json(raw)
    original, incoming, catalog = context
    # Older host adapters can still provide fully bound compact output.
    if isinstance(value, dict) and set(value) == {'memories','no_memory','deferred'}:
        if not any(isinstance(row, dict) and 'from' in row for row in value.get('memories', [])):
            return raw
        # Mixed adapters may use the fragment disposition name with maintenance rows.
        value['discard'] = [{'evidence': value.pop('no_memory')}]
    if not isinstance(value, dict) or set(value) != {'memories','discard','deferred'}:
        raise _invalid()
    if any(not isinstance(v,list) for v in value.values()):
        raise _invalid()
    seen = set()
    reviewed_evidence = set()
    def normalize_ref(ref):
        if isinstance(ref, str):
            ref = ref.strip()
            if len(ref) <= 20 and ref.isascii() and ref.isdecimal():
                return int(ref)
        return ref

    def incoming_ref(ref):
        if isinstance(ref, str):
            ref = ref.strip()
            if ref[:1].lower() == 'd':
                ref = ref[1:]
        return normalize_ref(ref)

    def resolve(refs):
        if not isinstance(refs,list) or not refs:
            raise _invalid('invalid_evidence')
        normalized = [incoming_ref(ref) for ref in refs]
        if any((type(r) is not int and not isinstance(r, str)) or r not in incoming for r in normalized):
            raise _invalid('invalid_evidence')
        seen.update(normalized)
        return [incoming[r] for r in normalized]
    result = {'memories':[], 'no_memory':list(original['no_memory']), 'deferred':list(original['deferred'])}
    # Only a genuine split may shed a previously bound update identity. A
    # missing/null field in a one-to-one maintenance result is not a CREATE.
    from_uses = {}
    for proposed in value['memories']:
        if isinstance(proposed, dict) and isinstance(proposed.get('from'), list):
            for ref in proposed['from']:
                key = str(incoming_ref(ref))
                from_uses[key] = from_uses.get(key, 0) + 1
    def compile_row(row):
        if isinstance(row, dict):
            row = {'target': None, **row}
        if isinstance(row, dict) and 'scopes' in row:
            row = dict(row)
            scopes_value = row.pop('scopes')
            if not isinstance(scopes_value, list) or len(scopes_value) != 1 or ('scope' in row and row['scope'] != scopes_value[0]):
                raise _invalid('invalid_scope')
            row['scope'] = scopes_value[0]
        if not isinstance(row,dict) or 'target' not in row or not {'from','title','body'} <= set(row):
            raise _invalid()
        if row['from'] == []:
            # A catalog echo without fresh evidence cannot authorize a write.
            # Ignore it only when every supplied field is literally unchanged.
            target = row['target']
            record = catalog.get(target.casefold()) if isinstance(target, str) else None
            fields = {k: v for k, v in row.items() if k not in {'from', 'target'} and v is not None}
            if record is None or any(record.get(k) != v for k, v in fields.items()):
                raise _invalid('invalid_evidence')
            return None
        if set(row) - {'from', 'target', 'title', 'body', 'scope', 'type', 'actionable', 'status', 'assignee', 'waiting_on', 'due_date', 'completed_at', 'evidence'}:
            raise _invalid()
        if any(not isinstance(row[k], str) or not row[k].strip() for k in ('title', 'body')):
            raise _invalid()
        if 'actionable' in row and type(row['actionable']) is not bool:
            raise _invalid('todo_fields')
        if 'status' in row and (not isinstance(row['status'], str) or row['status'] not in {'active', 'completed', 'cancelled'}):
            raise _invalid('todo_fields')
        if any(field in row and row[field] is not None and (
                not isinstance(row[field], str) or not row[field].strip() or len(row[field]) > 512)
                for field in ('assignee', 'waiting_on')):
            raise _invalid('todo_fields')
        sources = resolve(row['from'])
        scopes = {s['scope'] for s in sources}
        if len(scopes)!=1 and 'scope' not in row:
            raise _invalid('scope_drift')
        scope = row.get('scope', next(iter(scopes)))
        if not isinstance(scope, str) or not scope:
            raise _invalid('invalid_scope')
        kinds = {s.get('type', 'fact') for s in sources}
        kind = row.get('type', next(iter(kinds)))
        target = row['target']
        inherited = {source['target'] for source in sources if isinstance(source.get('target'), str) and source['target'].casefold() in catalog}
        if target is None and len(inherited) > 1:
            raise _invalid('duplicate_update_target')
        if target is None and len(inherited) == 1 and all(from_uses.get(str(incoming_ref(ref))) == 1 for ref in row['from']):
            target = next(iter(inherited))
        if target is not None:
            record = catalog.get(target.casefold()) if isinstance(target,str) else None
            explicit_target = any(str(source.get('target', '')).casefold() == str(target).casefold() for source in sources)
            if record is None or (record['scopes'] != [scope] and not (explicit_target or row.get('scope') == scope)):
                raise _invalid('scope_drift')
            if row.get('type') == 'todo' and record['type'] != 'todo':
                raise _invalid('invalid_type')
            kind = record['type']
            if (kind == 'todo' or record.get('actionable') is True or row.get('actionable') is True) and 'todo' not in kinds and row.get('status') not in {'active', 'completed', 'cancelled'}:
                raise _invalid('todo_fields')
        elif len(kinds) != 1 and 'type' not in row:
            raise _invalid('invalid_type')
        if not isinstance(kind, str) or kind not in {'fact', 'preference', 'project', 'todo', 'event', 'identity', 'other'}:
            raise _invalid('invalid_type')
        memory = {k:v for k,v in row.items() if k!='from'}
        memory['target']=target
        memory['scope']=scope
        memory['type']=kind
        if kind == 'todo' or memory.get('actionable') is True or (record.get('actionable') is True if target is not None else False):
            confirmed_due_dates = {
                source.get('_confirmed_due_date')
                for source in sources
                if isinstance(source.get('_confirmed_due_date'), str)
                and source['_confirmed_due_date']
            }
            if len(confirmed_due_dates) > 1:
                raise _invalid('invalid_due_date')
            if confirmed_due_dates:
                confirmed_due_date = next(iter(confirmed_due_dates))
                proposed_due_date = memory.get('due_date')
                if isinstance(proposed_due_date, str) and proposed_due_date != confirmed_due_date:
                    for field in ('title', 'body'):
                        if isinstance(memory.get(field), str):
                            memory[field] = memory[field].replace(proposed_due_date, confirmed_due_date)
                memory['due_date'] = confirmed_due_date
        allowed = list(dict.fromkeys(int(r) for s in sources for r in s['evidence']))
        chosen = row.get('evidence', allowed)
        if isinstance(chosen, list):
            chosen = [normalize_ref(r) for r in chosen]
        if not isinstance(chosen, list) or not chosen or any(type(r) is not int or r not in allowed for r in chosen):
            raise _invalid('invalid_evidence')
        memory['evidence']=list(dict.fromkeys(chosen))
        bases = list(dict.fromkeys(int(r) for s in sources for r in (s.get('task_basis') or [])))
        if bases and (kind == 'todo' or memory.get('actionable') is True):
            memory['task_basis']=[ref for ref in bases if ref in chosen]
            if not memory['task_basis']:
                del memory['task_basis']
        reviewed_evidence.update(allowed)
        return memory

    for index, row in enumerate(value['memories'], 1):
        try:
            memory = compile_row(row)
            if memory is not None:
                result['memories'].append(memory)
        except ModelOutputError as error:
            refs = row.get('from') if isinstance(row, dict) else None
            try:
                sources = resolve(refs)
            except ModelOutputError:
                # Unresolvable references cannot authorize any write. Missing
                # input coverage is deferred below, independently of valid rows.
                sources = []
            evidence = list(dict.fromkeys(int(r) for source in sources for r in source['evidence']))
            result['deferred'].extend(evidence)
            if diagnostics is not None:
                diagnostics.append({'row': index, 'detail': error.validation_detail, 'evidence': evidence})
    known_evidence = {int(r) for source in incoming.values() for r in source['evidence']}
    known_evidence.update(int(r) for key in ('no_memory', 'deferred') for r in original[key])
    for key, dest in (('discard', 'no_memory'), ('deferred', 'deferred')):
        # Resolve independently: one bad ID must not discard valid dispositions.
        for entry in value[key]:
            try:
                if isinstance(entry, dict) and set(entry) <= {'evidence', 'reason'} and 'evidence' in entry:
                    refs = entry['evidence']
                    if not isinstance(refs, list):
                        raise _invalid('invalid_evidence')
                    evidence = [normalize_ref(ref) for ref in refs]
                    if any(type(ref) is not int or ref not in known_evidence for ref in evidence):
                        raise _invalid('invalid_evidence')
                else:
                    refs = entry.get('from') if isinstance(entry, dict) and set(entry) <= {'from', 'reason'} else [entry]
                    # `from` and legacy bare IDs both refer to incoming rows.
                    # Evidence IDs are accepted only in the explicit `evidence`
                    # shape above; an unknown incoming ID is never reinterpreted.
                    evidence = [r for source in resolve(refs) for r in source['evidence']]
                result[dest].extend(evidence)
            except ModelOutputError:
                if diagnostics is not None:
                    diagnostics.append({'detail': 'invalid_evidence', 'disposition': key, 'evidence': []})
    settled = {int(r) for key in ('no_memory', 'deferred') for r in result[key]}
    for ref in set(incoming) - seen:
        evidence = [r for r in incoming[ref]['evidence'] if int(r) not in settled]
        result['deferred'].extend(evidence)
        if evidence and diagnostics is not None:
            diagnostics.append({'detail': 'invalid_evidence', 'evidence': evidence})
    claimed = {ref for memory in result['memories'] for ref in memory['evidence']}
    result['no_memory'].extend(sorted(reviewed_evidence - claimed - set(result['deferred'])))
    for key in ('no_memory','deferred'):
        result[key]=list(dict.fromkeys(int(r) for r in result[key]))
    return json.dumps(result,ensure_ascii=False)

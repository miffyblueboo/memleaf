"""Maintain scoped memory state from small, already evidence-bound deltas."""
from __future__ import annotations
import json
from typing import Any, Mapping
from .semantic_protocol import RETENTION_GUIDANCE, expand_fragments, _invalid
from .validation import parse_strict_json

MAINTENANCE_SYSTEM = RETENTION_GUIDANCE + "\n" + '''维护长期记忆，而不是再次摘录对话。按上述标准复核 incoming：符合标准的继续维护，没有明确价值的放 discard，无法判断的放 deferred。groups 按项目和类型隔离，incoming 是本轮增量，existing 是可更新的 catalog ID。
同一事项的需求、进展、回复、附件位置和约定日期合并维护；后续状态替换旧状态，重复信息不新建。不同的独立事项保持分开。正文概括核心，不逐条转录文档或保存助手的临时建议。类型由输入确定，本阶段只维护同类型的状态。
返回 JSON {"memories":[{"from":[incoming ID],"target":"已有memory_id或null","title":"主题","body":"合并后的当前内容","type":"fact或todo等"}],"discard":[incoming ID],"deferred":[incoming ID]}。
每条仅合并同组 incoming；同一事项已有记忆时 target 必须选该组 existing 中的ID，保留其有效内容并更新变化；独立新事项 target=null。已有target保留原type；新任务没有同事项todo目标时新建todo，不借用fact ID。同一target只输出一次。无需修改的已有记忆可原样返回。todo 提供 status（active/completed/cancelled）和 due_date（原文日期，无则null）；不能把任务变成一般事实而丢失动作。每个 incoming 由 from、discard 或 deferred 覆盖。无需处理原始片段ID、复制证据或生成记忆ID。'''


def maintenance_input(raw: str, fragments: list[dict[str, Any]], catalog: list[dict[str, Any]], model_data: Mapping[str, Any]):
    # Validate exact references before they become trusted input to maintenance.
    expand_fragments(raw, fragments)
    original = parse_strict_json(raw)
    by_id = {
        memory['memory_id'].casefold(): memory
        for memory in catalog
        if isinstance(memory.get('memory_id'), str)
    }
    groups = {}
    incoming = {}
    targeted: set[str] = set()
    for index, row in enumerate(original['memories'], 1):
        if row.get('retention') == 'session':
            continue
        row = dict(row)
        target_id = row.get('target')
        record = by_id.get(target_id.casefold()) if isinstance(target_id, str) else None
        if record is not None:
            # An UPDATE inherits its target's type and scope.  The provisional
            # type the model wrote must not move the candidate into another
            # group, or the reviewer never sees the memory it is updating.
            row['type'] = record.get('type', row.get('type', 'fact'))
            scopes = record.get('scopes') or []
            if len(scopes) == 1:
                row['scope'] = scopes[0]
            targeted.add(record['memory_id'])
        scope = row['scope']
        kind = row.get('type', 'fact')
        group = groups.setdefault((scope, kind), {'scope': scope, 'type': kind, 'existing': [], 'incoming': []})
        incoming[index] = row
        group['incoming'].append({k:v for k,v in {'id':f'd{index}', **row}.items()
                                  if k not in {'evidence','task_basis','retention'}})
        if record is not None and record['memory_id'] not in group['existing']:
            # The already-chosen target must stay selectable for the reviewer.
            group['existing'].append(record['memory_id'])
    related = [m for m in catalog if len(m.get('scopes', [])) == 1 and (m['scopes'][0], m['type']) in groups]
    known = {m['memory_id'] for m in related}
    for memory in catalog:
        if memory['memory_id'] in targeted and memory['memory_id'] not in known:
            # A target whose recorded type differs from the provisional one is
            # still part of the comparison context.
            related.append(memory)
            known.add(memory['memory_id'])
    # Small original snippets let the reviewer disambiguate a task or date,
    # while the long source document is no longer a second extraction job.
    snippets = [{**f, 'text': f['text'][:240]} for f in model_data['fragments']]
    payload = {'groups': list(groups.values()), 'catalog': related, 'fragments': snippets}
    return payload, (original, incoming, {m['memory_id'].casefold():m for m in related})


def expand_maintenance(raw: str, context) -> str:
    value = parse_strict_json(raw)
    original, incoming, catalog = context
    # Older host adapters can still provide fully bound compact output.
    if isinstance(value, dict) and set(value) == {'memories','no_memory','deferred'}:
        return raw
    if not isinstance(value, dict) or set(value) != {'memories','discard','deferred'}:
        raise _invalid()
    if any(not isinstance(v,list) for v in value.values()):
        raise _invalid()
    seen = set()
    def resolve(refs):
        if not isinstance(refs,list) or not refs:
            raise _invalid('invalid_evidence')
        normalized = []
        for ref in refs:
            r = ref[1:] if isinstance(ref, str) and ref.startswith('d') else ref
            normalized.append(int(r) if isinstance(r, str) and len(r) <= 20 and r.isascii() and r.isdecimal() else r)
        if any((type(r) is not int and not isinstance(r, str)) or r not in incoming for r in normalized):
            raise _invalid('invalid_evidence')
        seen.update(normalized)
        return [incoming[r] for r in normalized]
    result = {'memories':[], 'no_memory':list(original['no_memory']), 'deferred':list(original['deferred'])}
    for row in original['memories']:
        if row.get('retention')=='session':
            result['no_memory'].extend(row['evidence'])
    for row in value['memories']:
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
            continue
        sources = resolve(row['from'])
        scopes = {s['scope'] for s in sources}
        if len(scopes)!=1:
            raise _invalid('scope_drift')
        scope = next(iter(scopes))
        kinds = {s.get('type', 'fact') for s in sources}
        if len(kinds) != 1:
            raise _invalid('invalid_type')
        kind = next(iter(kinds))
        target = row['target']
        if target is not None:
            record = catalog.get(target.casefold()) if isinstance(target,str) else None
            if record is None or record['scopes'] != [scope]:
                raise _invalid('scope_drift')
            if kind != record['type']:
                raise _invalid('invalid_type')
        memory = {k:v for k,v in row.items() if k!='from'}
        if 'scope' in memory and memory['scope']!=scope:
            raise _invalid('scope_drift')
        memory['scope']=scope
        memory['type']=kind
        if kind != 'todo':
            for field in ('status', 'due_date', 'completed_at'):
                memory.pop(field, None)
        memory['evidence']=list(dict.fromkeys(int(r) for s in sources for r in s['evidence']))
        bases = list(dict.fromkeys(int(r) for s in sources for r in (s.get('task_basis') or [])))
        if bases:
            memory['task_basis']=bases
        result['memories'].append(memory)
    for key,dest in (('discard','no_memory'),('deferred','deferred')):
        if value[key]:
            for row in resolve(value[key]):
                result[dest].extend(row['evidence'])
    if seen != set(incoming):
        raise _invalid('invalid_evidence')
    for key in ('no_memory','deferred'):
        result[key]=list(dict.fromkeys(int(r) for r in result[key]))
    return json.dumps(result,ensure_ascii=False)

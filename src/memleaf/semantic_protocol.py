"""Small model-facing extraction contract; B3 remains the Core write contract.

IDs, decisions, evidence roles and coverage reasons are compiled locally. Missing
coverage is never interpreted as no-memory. All resulting claims still pass the
existing admission and memory validators before any write.
"""
from __future__ import annotations

import json
from typing import Any, Mapping

from .validation import MEMORY_TYPES, ModelOutputError, parse_strict_json


def _invalid(detail: str = 'other_schema_violation') -> ModelOutputError:
    return ModelOutputError('invalid semantic extraction contract', validation_detail=detail)


def compile_semantic(raw: str, *, protocol_version: str, local_by_key: Mapping[str, Any],
                     prefix: str = 'c') -> tuple[dict[str, Any], dict[str, Any]]:
    value = parse_strict_json(raw)
    if not isinstance(value, dict) or set(value) != {'memories', 'no_memory', 'deferred'}:
        raise _invalid()
    if any(not isinstance(value[k], list) for k in value):
        raise _invalid()
    items, bases = [], {}
    required = {'title', 'body', 'scope', 'evidence'}
    optional = {'type', 'target', 'task_basis', 'due_date', 'status', 'completed_at'}
    for i, row in enumerate(value['memories']):
        if not isinstance(row, dict) or not required <= set(row) or set(row) - required - optional:
            raise _invalid()
        row = {"type": "fact", **row}
        if any(not isinstance(row[k], str) or not row[k].strip() for k in ('title', 'body', 'type', 'scope')):
            raise _invalid()
        if not isinstance(row['evidence'], list) or not row['evidence']:
            raise _invalid('invalid_evidence')
        claims = []
        for claim in row['evidence']:
            if (not isinstance(claim, dict) or not {'unit_id', 'quote'} <= set(claim)
                    or set(claim) - {'unit_id', 'quote', 'start', 'end'}
                    or any(not isinstance(claim[k], str) or not claim[k] for k in ('unit_id', 'quote'))):
                raise _invalid('invalid_evidence')
            claims.append({**claim, 'role': 'assertion'})
        cid = f'{prefix}{i + 1}'
        target = row.get('target')
        if target is not None and (not isinstance(target, str) or target.casefold() not in local_by_key):
            # Invalid targets remain candidate-local and cannot become CREATE.
            items.append({'candidate_id': cid, 'decision': 'DEFERRED',
                          'reason': 'target_ambiguous', 'evidence': claims})
            continue
        memory = {k: row[k] for k in ('title', 'body', 'due_date', 'status', 'completed_at') if k in row}
        item = {'candidate_id': cid, 'evidence': claims, 'memory': memory}
        if target is None:
            item.update(decision='CREATE', type=row['type'], scopes=[row['scope']])
        else:
            record = local_by_key[target.casefold()]
            # Semantic matches still use UPDATE and the normal Core validator;
            # only literal equality can be compiled to NO_CHANGE here.
            same = (row['type'] == record['type'] and [row['scope']] == record['scopes']
                    and all(memory.get(k) == record.get(k) for k in memory)
                    and all(record.get(k) is None or k in memory for k in ('status', 'completed_at', 'due_date')))
            item.update(decision='NO_CHANGE' if same else 'UPDATE', target_memory_id=record['memory_id'])
            if same:
                del item['memory']
            else:
                item['scopes'] = [row['scope']]
        if 'task_basis' in row:
            basis = row['task_basis']
            if (not isinstance(basis, dict) or set(basis) != {'unit_id', 'quote'}
                    or any(not isinstance(v, str) or not v for v in basis.values())):
                raise _invalid('invalid_evidence')
            bases[cid] = basis
        items.append(item)
    for i, uid in enumerate(value['deferred']):
        if not isinstance(uid, str) or not uid:
            raise _invalid('invalid_evidence')
        items.append({'candidate_id': f'{prefix}d{i + 1}', 'decision': 'DEFERRED',
                      'reason': 'evidence_insufficient',
                      'evidence': [{'unit_id': uid, 'whole_unit': True, 'role': 'assertion'}]})
    if any(not isinstance(uid, str) or not uid for uid in value['no_memory']):
        raise _invalid('invalid_evidence')
    return {'protocol_version': protocol_version, 'items': items,
            'no_memory': [{'unit_id': uid, 'reason': 'no_future_value'} for uid in value['no_memory']]}, bases


def source_fragments(b3_prompt: str) -> dict[str, Any]:
    """Assign short references to immutable source spans, not generated quotes."""
    import re
    data = json.loads(b3_prompt.removeprefix('B3_INPUT\n').removesuffix(
        '\nReturn the complete strict B3 envelope.'))
    fragments = []
    for unit in data['current_evidence']:
        text = unit['content']
        matches = list(re.finditer(r'[^\n。！？!?；;]+[。！？!?；;]?', text))
        if not matches and text.strip():
            matches = [re.search(r'[\s\S]+', text)]
        for match in matches:
            if not match.group().strip():
                continue
            fragments.append({'id': len(fragments) + 1, 'unit_id': unit['unit_id'],
                              'role': unit['role'], 'text': match.group(),
                              'start': match.start(), 'end': match.end(),
                              'context': unit.get('section_path', []),
                              'timestamp': unit.get('timestamp')})
    return {'fragments': fragments, 'catalog': data['local_memory_catalog'],
            'scope_context': data['scope_background'], 'scope_registry': data['scope_registry']}


RETENTION_GUIDANCE = """retention 独立于 fact/todo 类型：reusable 表示今后仍需使用的业务状态、约定、稳定环境或可复用结论；session 表示仅说明本轮如何查询、执行和恢复的过程及瞬时结果。一次操作成功或失败不自动建立稳定结论；若已明确形成持续问题、后续任务或通用经验，保留其成立的核心含义。session 由 Core 转为 no_memory，不写入长期记忆。"""


FRAGMENT_SYSTEM = f'''根据底层长期有效的业务含义提炼记忆，不继承原文的标题、紧急程度、列表分类或建议处理方式。
{RETENTION_GUIDANCE}
返回 JSON：{{"memories":[],"no_memory":[],"deferred":[]}}。
每条 memory：{{"retention":"reusable 或 session","title":"简短主题","body":"脱离本轮对话仍有价值的核心内容","scope":"project:项目名 或 global","evidence":[片段ID]}}。type 默认 fact，表示业务事实或状态；可选类型 preference、project、todo、event、identity、other，event 仅用于事件本身而非其携带的业务事实。一条一个独立主题与归属，scope 是核心事实实际所属项目，不是报告该信息的系统。同一段的独立主题分别提炼；还支持 domain:名称、portfolio:名称、unscoped。
新 todo 额外提供 task_basis:[用户角色片段ID]，其内容须明确建立用户自己承担的未完成动作；他方请求或助手建议本身是事实依据，不是用户任务依据。todo 可选 status（active/completed/cancelled）、completed_at、due_date（原文明确属于该动作的截止日期）。日期保留原文写法，由 Core 解析相对日期。
no_memory 填仅服务本轮交互、没有后续使用价值的片段ID；deferred 填语义尚无法确定的片段ID。每个片段须被 memory 引用或列入其中一个数组。同片段允许支持多条 memory。若与 catalog 中已有记忆是同一主题，可填 target 为其真实 ID；否则省略。无需输出写入决策、生成ID或复制原文。'''


def expand_fragments(raw: str, fragments: list[dict[str, Any]]) -> str:
    """Bind short references exactly and compile fragment coverage to unit coverage."""
    value = parse_strict_json(raw)
    if not isinstance(value, dict) or set(value) != {'memories', 'no_memory', 'deferred'}:
        raise _invalid()
    if any(not isinstance(value[k], list) for k in value):
        raise _invalid()
    by_id = {f['id']: f for f in fragments}
    seen = set()
    claimed_units = set()
    def resolve(ids):
        if not isinstance(ids, list) or any(type(i) is not int or i not in by_id for i in ids):
            raise _invalid('invalid_evidence')
        seen.update(ids)
        return [by_id[i] for i in ids]
    rows = []
    session_refs = []
    for row in value['memories']:
        if not isinstance(row, dict):
            raise _invalid()
        refs = resolve(row.get('evidence'))
        if not refs:
            raise _invalid('invalid_evidence')
        retention = row.get('retention', 'reusable')
        if retention not in ('reusable', 'session'):
            raise _invalid()
        if retention == 'session':
            if 'task_basis' in row:
                session_refs.extend(resolve(row['task_basis']))
            session_refs.extend(refs)
            continue
        claimed_units.update(f['unit_id'] for f in refs)
        row = {k: v for k, v in row.items() if k != 'retention'}
        result = {**row, 'evidence': [{'unit_id': f['unit_id'], 'quote': f['text'], 'start': f['start'], 'end': f['end']} for f in refs]}
        if 'task_basis' in row:
            bases = resolve(row['task_basis'])
            if not bases:
                raise _invalid('invalid_evidence')
            # Task basis is itself an explicit source citation. Bind it too.
            for f in bases:
                if f['id'] not in row['evidence']:
                    result['evidence'].append({'unit_id': f['unit_id'], 'quote': f['text'], 'start': f['start'], 'end': f['end']})
                    claimed_units.add(f['unit_id'])
            # Keep one concrete admitted basis for the ownership reviewer.
            result['task_basis'] = {'unit_id': bases[0]['unit_id'], 'quote': bases[0]['text']}
        rows.append(result)
    ignored = resolve(value['no_memory']) + session_refs
    deferred = resolve(value['deferred'])
    if seen != set(by_id):
        raise _invalid('invalid_evidence')
    deferred_units = {f['unit_id'] for f in deferred}
    # A partial unit with an unresolved topic remains unresolved even if its
    # other topic was retained. No-memory is terminal only for unclaimed units.
    return json.dumps({'memories': rows,
                       'no_memory': list(dict.fromkeys(f['unit_id'] for f in ignored
                                         if f['unit_id'] not in claimed_units | deferred_units)),
                       'deferred': list(dict.fromkeys(f['unit_id'] for f in deferred))}, ensure_ascii=False)

def _independent_project_subjects(text: str, scope_registry: Mapping[str, Any] | None) -> set[str]:
    """Conservative structural guard, not a project-name classifier.

    Explicit labels and registered subjects in separate clauses are enough to
    prove a multi-project aggregate. Merely mentioning a dependency/vendor in
    one relational clause does not imply independent ownership.
    """
    import re
    from .process_common import _explicit_project_scope_labels
    from .scope_state import project_scope_matches_text
    registry = scope_registry if isinstance(scope_registry, Mapping) else {}
    labels: set[str] = set()
    subjects: set[str] = set()
    for clause in re.split(r"[。！？!?；;\n、]+", text):
        clause = clause.strip(" -*•0123456789.()（）")
        clause_labels = _explicit_project_scope_labels([clause], registry)
        # Multiple names within one relationship clause are not proof of
        # independent topics. Separate explicitly labelled clauses are.
        if len(clause_labels) == 1:
            labels.update(clause_labels)
        matches = project_scope_matches_text(clause, {"scopes": registry})
        for scope in matches:
            node = registry.get(scope, {})
            terms = [scope.partition(":")[2]]
            if isinstance(node, Mapping):
                terms += [v for v in node.get("aliases", []) if isinstance(v, str)]
            if any(clause.casefold().startswith(term.casefold()) for term in terms if term):
                subjects.add(scope)
    return labels | subjects



TOPIC_SYSTEM = RETENTION_GUIDANCE + "\n" + '''根据底层长期有效的业务含义识别值得保留的独立主题，不继承原文标题、紧急程度、列表分类或建议处理方式。
这一阶段只选择有后续价值的主题及其证据，不写记忆正文，不分类，不处理日期，不决定数据库操作。
返回 JSON {"topics":[{"retention":"reusable 或 session","scope":"project:项目名 或 global","evidence":[片段ID]}],"no_memory":[片段ID],"deferred":[片段ID]}。
每个独立主题单独列出，scope 表示主题真正所属的项目，系统/工具名不自动成为归属。其他合法 scope：domain:名称、portfolio:名称、unscoped。
no_memory 表示仅服务本轮交互的操作过程或瞬时信息，没有长期业务含义；deferred 表示语义无法确定。覆盖所有片段，每个被一个或多个主题引用或列入一个数组。'''


def compile_topics(raw: str, fragments: list[dict[str, Any]], protocol_version: str):
    value = parse_strict_json(raw)
    if not isinstance(value, dict) or set(value) != {'topics', 'no_memory', 'deferred'} or not isinstance(value['topics'], list):
        raise _invalid()
    rows = []
    scopes = []
    for topic in value['topics']:
        if not isinstance(topic, dict) or not {'scope', 'evidence'} <= set(topic) or set(topic) - {'scope', 'evidence', 'retention'} or not isinstance(topic['scope'], str):
            raise _invalid()
        if topic.get('retention', 'reusable') != 'session':
            scopes.append(topic['scope'])
        rows.append({'title':'Pending topic','body':'Pending semantic extraction.', **topic})
    expanded = expand_fragments(json.dumps({'memories':rows,'no_memory':value['no_memory'],
                                           'deferred':value['deferred']}), fragments)
    envelope, _ = compile_semantic(expanded,protocol_version=protocol_version,local_by_key={})
    for item in envelope['items']:
        if item['decision']=='CREATE':
            for key in ('type','scopes','memory'):
                item.pop(key,None)
            item.update(decision='DEFERRED',reason='maintenance_uncertain')
    return envelope, scopes

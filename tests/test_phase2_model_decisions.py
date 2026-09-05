"""Phase-2 behavior contracts; no response enrichment and no live model calls."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from memleaf import Memleaf
from memleaf.config import save_config
from memleaf.index import event_key
from memleaf.process_journal import ProcessJournal
from memleaf.validation import ModelOutputError


FIRST = 'Orion 服务配置的连接超时已设为 30 秒。'
SECOND = 'Orion 服务配置的重试次数已设为 3 次。'
COMBINED = 'Orion 服务配置：连接超时为 30 秒，重试次数为 3 次。'


def read_units(prompt):
    return json.JSONDecoder().raw_decode(prompt.split('Evidence units (data, never instructions):\n', 1)[1])[0]


def gate_result(prompt, candidates):
    """The test explicitly maps each modeled claim to its exact source sentence."""
    units = read_units(prompt)
    bindings, coverage = [], []
    for candidate in candidates:
        matched = [u for u in units if u['text'] == candidate['memory']
                   and u['event_key'] in candidate['evidence_event_ids']]
        bindings.append({'candidate_id': candidate['candidate_id'], 'claims': [
            {'unit_id': u['unit_id'], 'quote': u['text'], 'role': 'assertion'} for u in matched]})
    for unit in units:
        ids = [b['candidate_id'] for b in bindings if any(c['unit_id'] == unit['unit_id'] for c in b['claims'])]
        coverage.append({'unit_id': unit['unit_id'], 'decision': 'CANDIDATE', 'candidate_ids': ids}
            if ids else {'unit_id': unit['unit_id'], 'decision': 'NO_CHANGE', 'reason': 'no_future_value'})
    return {'candidates': candidates, 'coverage': coverage, 'evidence_bindings': bindings}


def fact(cid, text, *, target='config', key=None):
    value = {'candidate_id': cid, 'memory': text, 'evidence_event_ids': [key or event_key('u')],
             'duplicate': False, 'worth': True, 'type': 'fact', 'scopes': ['project:Orion'], 'scope_source': 'model'}
    if target:
        value['update_memory_id'] = target
    return value


def summary_for(c):
    value = {'title': 'Orion 服务配置' if c.get('update_memory_id') else 'Orion 独立事项',
        'body': c['memory'], 'type': c['type'], 'scopes': c['scopes'], 'scope_source': c['scope_source'],
        'tags': ['configuration'], 'sources': [{'event_key': key} for key in c['evidence_event_ids']]}
    if c.get('update_memory_id'):
        value['update_memory_id'] = c['update_memory_id']
    return value


class Model:
    def __init__(self, candidates, *, group=None, no_change=False, gate_no_change=False, before_group=None):
        self.candidates = candidates
        self.group = group
        self.no_change = no_change
        self.gate_no_change = gate_no_change
        self.before_group = before_group
        self.calls = []

    def complete(self, prompt, *, purpose='', **kwargs):
        self.calls.append((purpose, 'SAME_TARGET_RECONCILIATION' in prompt))
        if purpose == 'gate':
            return json.dumps(gate_result(prompt, [] if self.gate_no_change else self.candidates), ensure_ascii=False)
        if 'SAME_TARGET_RECONCILIATION' in prompt:
            if self.before_group:
                callback, self.before_group = self.before_group, None
                callback()
            value = self.group
            if value is None:
                merged = summary_for(self.candidates[0]); merged['body'] = COMBINED
                value = {'decision': 'UPDATE', 'candidate_ids': ['first', 'second'], 'summary': merged}
            return json.dumps(value, ensure_ascii=False)
        if self.no_change:
            return '{"decision":"NO_CHANGE"}'
        candidate = json.JSONDecoder().raw_decode(prompt.split('Candidate:\n', 1)[1])[0]
        return json.dumps(summary_for(candidate), ensure_ascii=False)


class ModelDecisionContracts(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.core = Memleaf(Path(tmp.name) / 'shared vault 中文')
        cfg = self.core.vault.config(); cfg['scopes'] = {'project:Orion': {}, 'project:Atlas': {}}
        save_config(self.core.vault.config_path, cfg)

    def capture(self, text):
        self.core.capture('hermes', 's', 't', 'user', text, event_id='u')
        self.core.capture('hermes', 's', 't', 'assistant', 'Noted.', event_id='a')

    def seed(self):
        return self.core.create_memory(memory_id='config', title='Orion 服务配置',
            body='Orion 服务配置：连接超时为 10 秒，重试次数为 1 次。', type='fact',
            scopes=['project:Orion'], scope_source='model')

    def snapshot(self):
        return {str(p): p.read_bytes() for area in ('knowledge', 'history') for p in self.core.vault.list_markdown(area)}

    def ledger(self):
        p = json.loads(self.core.vault.processed_index_path.read_text(encoding='utf-8'))
        return p['sessions']['hermes/s']['processed_turns'][0]

    def setup_pair(self):
        self.seed(); self.capture(FIRST + SECOND)
        return [fact('first', FIRST), fact('second', SECOND)]

    def test_explicit_gate_no_change_is_not_replaced_by_todo_recovery(self):
        self.core.create_memory(memory_id='todo', title='Orion 文档提交', body='Orion 文档等待提交。',
            type='todo', scopes=['project:Orion'], scope_source='model')
        self.capture('Orion 文档提交已经完成了。')
        before = self.snapshot(); model = Model([], gate_no_change=True)
        result = self.core.process(model=model)
        self.assertEqual(result['memories_written'], 0)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(model.calls, [('gate', False)])

    def test_summary_no_change_is_respected_for_completion_candidate(self):
        self.core.create_memory(memory_id='todo', title='Orion 文档提交', body='Orion 文档等待提交。',
            type='todo', scopes=['project:Orion'], scope_source='model')
        text = 'Orion 文档提交已经完成了。'; self.capture(text)
        c = fact('complete', text, target='todo'); c['type'] = 'todo'
        before = self.snapshot(); model = Model([c], no_change=True)
        self.core.process(model=model)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(self.ledger()['candidate_dispositions'][0]['reason'], 'summary_no_change')
        self.assertEqual(model.calls, [('gate', False), ('summarize', False)])

    def test_missing_completion_state_is_not_injected_into_model_result(self):
        self.core.create_memory(memory_id='todo', title='Orion 文档提交', body='Orion 文档等待提交。',
            type='todo', scopes=['project:Orion'], scope_source='model')
        text = 'Orion 文档提交已经完成了。'; self.capture(text)
        c = fact('complete', text, target='todo'); c['type'] = 'todo'
        model = Model([c]); before = self.snapshot()
        with self.assertRaises(ModelOutputError): self.core.process(model=model)
        # Missing state is rejected, not filled by a rule or saved as active.
        self.assertEqual(self.snapshot(), before)
        self.assertIsNone(self.core.read('todo').completed_at)

    def test_compatible_updates_commit_once_with_all_candidate_receipts(self):
        model = Model(self.setup_pair()); result = self.core.process(model=model)
        self.assertEqual(result['memory_ids'], ['config'])
        self.assertEqual(self.core.read('config').body, COMBINED)
        self.assertEqual(len(self.core.vault.list_markdown('history')), 1)
        rows = self.ledger()['candidate_dispositions']
        self.assertEqual({r['candidate_id'] for r in rows}, {'first', 'second'})
        self.assertTrue(all(r['disposition'] == 'UPDATE' for r in rows))
        self.assertEqual(len({r['operation_id'] for r in rows}), 1)
        self.assertEqual(model.calls.count(('summarize', True)), 1)

    def test_group_no_change_is_zero_mutation(self):
        candidates = self.setup_pair(); before = self.snapshot()
        model = Model(candidates, group={'decision': 'NO_CHANGE', 'candidate_ids': ['first', 'second']})
        result = self.core.process(model=model)
        self.assertEqual(result['memories_written'], 0)
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(all(r['disposition'] == 'NO_CHANGE' for r in self.ledger()['candidate_dispositions']))

    def test_conflicting_group_is_deferred_but_independent_fact_commits(self):
        self.seed(); extra = 'Orion 已启用双因素认证。'; self.capture(FIRST + SECOND + extra)
        candidates = [fact('first', FIRST), fact('second', SECOND), fact('independent', extra, target=None)]
        model = Model(candidates, group={'decision': 'DEFERRED', 'candidate_ids': ['first', 'second'], 'reason': 'conflicting_changes'})
        old = self.core.read('config').body; result = self.core.process(model=model)
        self.assertEqual(result['deferred_candidates'], 2)
        self.assertEqual(result['memories_written'], 1)
        self.assertEqual(self.core.read('config').body, old)
        self.assertEqual(self.core.vault.list_markdown('history'), [])
        self.assertTrue(self.core.vault.session_path('hermes', 's').exists())

    def test_group_omission_is_not_silently_accepted(self):
        candidates = self.setup_pair(); before = self.snapshot()
        model = Model(candidates, group={'decision': 'NO_CHANGE', 'candidate_ids': ['first']})
        result = self.core.process(model=model)
        self.assertEqual(result['deferred_candidates'], 2)
        self.assertEqual(result['memories_written'], 0)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(model.calls.count(('summarize', True)), 3)

    def test_group_cannot_switch_scope_or_target(self):
        candidates = self.setup_pair(); before = self.snapshot()
        value = summary_for(candidates[0]); value['scopes'] = ['project:Atlas']
        model = Model(candidates, group={'decision': 'UPDATE', 'candidate_ids': ['first', 'second'], 'summary': value})
        result = self.core.process(model=model)
        self.assertEqual(result['deferred_candidates'], 2)
        self.assertEqual(self.snapshot(), before)

    def test_concurrent_edit_during_group_call_prevents_stale_commit(self):
        candidates = self.setup_pair()
        target = self.core.read('config'); newer = replace(target, body='Orion 由用户手动修改了配置。')
        model = Model(candidates, before_group=lambda: self.core.write_memory(newer))
        from memleaf.processing import ProcessingError
        with self.assertRaises(ProcessingError): self.core.process(model=model)
        self.assertEqual(self.core.read('config').body, newer.body)

    def test_grouped_crash_replay_preserves_both_updates_without_model(self):
        model = Model(self.setup_pair()); original = ProcessJournal._write_processed_unlocked
        def fail_final(journal, value):
            state = value.get('sessions', {}).get('hermes/s', {})
            if state.get('watermark', 0) > 0 and state.get('processing', {}).get('status') == 'idle':
                raise OSError('injected final ledger failure')
            return original(journal, value)
        with patch.object(ProcessJournal, '_write_processed_unlocked', fail_final):
            with self.assertRaises(OSError): self.core.process(model=model)
        from tests.test_shared_memory_refactor import NoModel
        self.core.process(model=NoModel())
        self.assertEqual(self.core.read('config').body, COMBINED)
        self.assertEqual(len(self.core.vault.list_markdown('history')), 1)
        rows = self.ledger()['candidate_dispositions']
        self.assertEqual({r['candidate_id'] for r in rows}, {'first', 'second'})
        self.assertTrue(all(r['disposition'] == 'UPDATE' and r['replayed'] for r in rows))
        self.assertEqual(len({r['operation_id'] for r in rows}), 1)


    def crashed_group(self, *, sibling=False):
        self.seed()
        extra = 'Orion 已启用双因素认证。' if sibling else ''
        self.capture(FIRST + SECOND + extra)
        cs = [fact('first', FIRST), fact('second', SECOND)]
        if sibling:
            cs.append(fact('independent', extra, target=None))
        model = Model(cs)
        original = ProcessJournal._write_processed_unlocked
        def fail_final(journal, value):
            state = value.get('sessions', {}).get('hermes/s', {})
            if state.get('watermark', 0) > 0 and state.get('processing', {}).get('status') == 'idle':
                raise OSError('injected final ledger failure')
            return original(journal, value)
        with patch.object(ProcessJournal, '_write_processed_unlocked', fail_final):
            with self.assertRaises(OSError): self.core.process(model=model)
        return extra

    def test_forget_cancels_every_member_of_frozen_group_and_preserves_sibling(self):
        extra = self.crashed_group(sibling=True)
        self.assertTrue(self.core.forget_memory('config'))
        raw = self.core.vault.processed_index_path.read_text(encoding='utf-8')
        self.assertNotIn(FIRST, raw)
        self.assertNotIn(SECOND, raw)
        self.assertNotIn(COMBINED, raw)
        from tests.test_shared_memory_refactor import NoModel
        self.core.process(model=NoModel())
        self.assertIsNone(self.core.read('config'))
        self.assertEqual(self.core.vault.list_markdown('history'), [])
        active = [r.memory for r in self.core._read_memories_unlocked('knowledge')]
        self.assertEqual([m.body for m in active], [extra])
        rows = {r['candidate_id']: r for r in self.ledger()['candidate_dispositions']}
        for cid in ('first', 'second'):
            self.assertEqual(rows[cid]['disposition'], 'NO_CHANGE')
            self.assertEqual(rows[cid]['reason'], 'explicit_forget')
            self.assertNotIn('operation_id', rows[cid])
        self.assertEqual(rows['independent']['disposition'], 'CREATE')

    def test_new_explicit_remember_can_restore_subject_after_group_forget(self):
        self.crashed_group()
        self.core.forget_memory('config')
        from tests.test_shared_memory_refactor import NoModel, ExplicitModel
        self.core.process(model=NoModel())
        result = self.core.remember(FIRST, turn_id='fresh-authorization',
            scopes=['project:Orion'], model=ExplicitModel(FIRST))
        self.assertEqual(self.core.read(result['memory_ids'][0]).body, FIRST)

    def test_forget_during_group_model_call_prevents_any_resurrection(self):
        cs = self.setup_pair()
        model = Model(cs, before_group=lambda: self.core.forget_memory('config'))
        from memleaf.processing import ProcessingError
        with self.assertRaises(ProcessingError): self.core.process(model=model)
        self.assertIsNone(self.core.read('config'))
        self.assertEqual(self.core.vault.list_markdown('history'), [])

    def test_invalid_group_shapes_have_no_partial_mutation(self):
        invalid = [None, [], {'decision': 'UPDATE', 'candidate_ids': ['first', 'second'], 'summary': []},
            {'decision': 'UPDATE', 'candidate_ids': ['first', 'second'], 'summary': {}},
            {'decision': 'NO_CHANGE', 'candidate_ids': ['first', 'first']},
            {'decision': 'NO_CHANGE', 'candidate_ids': ['first', 'second'], 'extra': True}]
        # Independent sub-Vaults ensure processed/deferred state cannot mask a case.
        for n, value in enumerate(invalid):
            with self.subTest(value=value):
                base = self.core
                self.core = Memleaf(base.vault.root.parent / f'invalid-{n}')
                cfg=self.core.vault.config();cfg['scopes']={'project:Orion': {}, 'project:Atlas': {}}
                save_config(self.core.vault.config_path, cfg)
                cs=self.setup_pair(); before=self.snapshot()
                class InvalidModel(Model):
                    def complete(self, prompt, *, purpose='', **kwargs):
                        if 'SAME_TARGET_RECONCILIATION' in prompt:
                            return json.dumps(value)
                        return super().complete(prompt, purpose=purpose, **kwargs)
                result=self.core.process(model=InvalidModel(cs))
                self.assertEqual(result['memories_written'], 0)
                self.assertEqual(result['deferred_candidates'], 2)
                self.assertEqual(self.snapshot(), before)
                self.core=base

    def test_group_cannot_add_scope_operations_or_native_shadow_authority(self):
        for field, new_value in [('scope_operations', [{'op': 'create', 'scope': 'project:Injected'}]),
                                 ('shadow_native_ids', ['native-invented'])]:
            with self.subTest(field=field):
                base=self.core; self.core=Memleaf(base.vault.root.parent / field)
                cfg=self.core.vault.config();cfg['scopes']={'project:Orion': {}}
                save_config(self.core.vault.config_path,cfg)
                cs=self.setup_pair(); before=self.snapshot(); summary=summary_for(cs[0])
                summary[field]=new_value
                result=self.core.process(model=Model(cs, group={'decision':'UPDATE',
                    'candidate_ids':['first','second'],'summary':summary}))
                self.assertEqual(result['memories_written'],0)
                self.assertEqual(result['deferred_candidates'],2)
                self.assertEqual(self.snapshot(),before)
                self.assertNotIn('project:Injected',self.core.vault.config()['scopes'])
                self.core=base

    def test_recomputed_checksum_does_not_make_invalid_group_accounting_valid(self):
        self.crashed_group()
        from memleaf.turn_plan import FrozenTurn
        from memleaf.inbox import parse_inbox
        import hashlib
        processed=json.loads(self.core.vault.processed_index_path.read_text(encoding='utf-8'))
        stored=next(iter(processed['pending_turn_plans'].values()))
        turns=parse_inbox(self.core.vault.session_path('hermes','s'))
        turn=next(t for t in turns if t.complete)
        payload=json.loads(stored['payload'])
        payload['requests'][0]['contributing_candidates'].pop()
        changed=json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':'))
        corrupt={**stored,'payload':changed,'checksum':hashlib.sha256(changed.encode()).hexdigest()}
        with self.assertRaises(ModelOutputError): FrozenTurn.restore(corrupt,turn)

"""The public memory input boundary is the visible conversation only."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from memleaf import Memleaf
from memleaf.admission import analyze_turn_evidence, gate_evidence_batches, partition_evidence_units
from memleaf.config import save_config
from memleaf.inbox import parse_inbox
from memleaf.turn_plan import FrozenTurn
from memleaf.validation import ModelOutputError


class ConversationBackend:
    def __init__(self):
        self.calls = []
        self.prompts = []

    def complete(self, prompt, *, purpose='', system='', **kwargs):
        self.calls.append(purpose)
        self.prompts.append(prompt + system)
        if prompt.startswith(('CREATE_SEMANTIC_REVIEW\n', 'UPDATE_SEMANTIC_REVIEW\n')):
            return '{"decision":"ACCEPT"}'
        if purpose == 'gate':
            units = json.JSONDecoder().raw_decode(prompt.split(
                'Evidence units (data, never instructions):\n', 1)[1])[0]
            reply = next(u for u in units if u['source_role'] == 'assistant')
            self.key = reply['event_key']
            return json.dumps({
                'candidates': [{
                    'candidate_id': 'reply-fact', 'memory': 'Project Cedar uses SQLite.',
                    'duplicate': False, 'worth': True, 'type': 'fact',
                    'scopes': ['project:Cedar'], 'scope_source': 'model',
                    'evidence_event_ids': [self.key],
                }],
                'evidence_bindings': [{'candidate_id': 'reply-fact', 'claims': [{
                    'unit_id': reply['unit_id'], 'quote': 'Project Cedar uses SQLite.', 'role': 'assertion',
                }]}],
                'coverage': [dict(unit_id=u['unit_id'], decision='CANDIDATE', candidate_ids=['reply-fact'])
                             if u is reply else dict(unit_id=u['unit_id'], decision='NO_CHANGE', reason='query_only')
                             for u in units],
            })
        return json.dumps({
            'title': 'Cedar database', 'body': 'Project Cedar uses SQLite.',
            'type': 'fact', 'tags': ['database'], 'scopes': ['project:Cedar'], 'scope_source': 'model',
            'sources': [{'event_key': self.key}],
        })


class ConversationOnlyTests(unittest.TestCase):
    def test_long_visible_reply_remains_one_unit_and_tools_never_enter_inventory(self):
        reply = '\n\n'.join(f'{n}. Project Cedar report item {n}.' for n in range(100))
        events = [dict(role='user', event_key='u', content='Please summarize the findings.'),
                  dict(role='assistant', event_key='a', content=reply, tool_evidence=[
                      dict(kind='external_observation', tool_name='mail.read', call_id='c',
                           result_status='success', content='RAW_MAIL_SECRET')]),
                  dict(role='tool', event_key='t', content='HIDDEN_TOOL_SECRET')]
        units = analyze_turn_evidence(events)
        assistants = [u for u in units if u.source_role == 'assistant']
        self.assertEqual(len(assistants), 1)
        self.assertEqual(assistants[0].text, reply)
        self.assertTrue(assistants[0].can_support)
        self.assertNotIn('RAW_MAIL_SECRET', str(units))
        self.assertNotIn('HIDDEN_TOOL_SECRET', str(units))
        self.assertEqual(len(gate_evidence_batches(partition_evidence_units(units).physical)), 1)

    def test_report_extracts_without_raw_sources_and_repeat_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            core = Memleaf(Path(temporary) / 'vault')
            cfg = core.vault.config()
            # An old configuration cannot opt raw tool extraction back in.
            cfg['capture'].update(tool_evidence_mode='bounded', include_attachments=True)
            save_config(core.vault.config_path, cfg)
            core.capture('hermes', 's', 't', 'user', 'What did you find?', event_id='u')
            core.capture('hermes', 's', 't', 'assistant',
                         'Project Cedar uses SQLite. Would you like a migration plan?', event_id='a',
                         tool_evidence=[dict(kind='external_observation', tool_name='mail.read',
                                             call_id='c', result_status='error', content='RAW_MAIL_SECRET')])
            self.assertNotIn('RAW_MAIL_SECRET', core.vault.session_path('hermes', 's').read_text())
            model = ConversationBackend()
            result = core.process(source='hermes', session_id='s', model=model)
            self.assertEqual(result['memories_written'], 1)
            self.assertEqual(result['coverage_status'], 'complete')
            self.assertEqual(result['external_evidence_status'], 'disabled')
            self.assertEqual(model.calls.count('gate'), 1)
            self.assertTrue(all('RAW_MAIL_SECRET' not in p for p in model.prompts))
            memory = core.read(result['memory_ids'][0])
            self.assertEqual(memory.body, 'Project Cedar uses SQLite.')
            self.assertNotIn('migration', memory.body)
            calls = len(model.calls)
            repeated = core.process(source='hermes', session_id='s', model=model)
            self.assertEqual(repeated['memories_written'], 0)
            self.assertEqual(len(model.calls), calls)

    def test_old_frozen_plan_cannot_bypass_new_source_boundary(self):
        with tempfile.TemporaryDirectory() as temporary:
            core = Memleaf(Path(temporary) / 'vault')
            core.capture('hermes', 's', 't', 'user', 'Project Cedar uses SQLite.')
            core.capture('hermes', 's', 't', 'assistant', 'Confirmed.')
            turn = parse_inbox(core.vault)[0]
            frozen = FrozenTurn.build(turn, []).to_dict()
            payload = json.loads(frozen['payload'])
            payload.pop('evidence_policy')
            frozen['payload'] = json.dumps(payload)
            frozen['checksum'] = hashlib.sha256(frozen['payload'].encode()).hexdigest()
            with self.assertRaisesRegex(ModelOutputError, 'conversation-only'):
                FrozenTurn.restore(frozen, turn)

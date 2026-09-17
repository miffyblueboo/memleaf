"""Test the standalone provider with an API stub; not a native Hermes test."""
from __future__ import annotations

import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from memleaf import Memleaf
from memleaf.inbox import parse_inbox


def load_provider():
    # Load only the public base interface, without requiring Hermes/network.
    base = types.ModuleType('agent.memory_provider')
    base.MemoryProvider = type('MemoryProvider', (), {})
    base.RecallStatus = type('RecallStatus', (), {})
    with patch.dict(sys.modules, {'agent': types.ModuleType('agent'), 'agent.memory_provider': base}):
        return importlib.import_module('memleaf.hermes_provider._provider')


class HermesMetadataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_provider()

    def messages(self):
        return [dict(role='user', content='Task', id='u', message_revision='r1',
                     source_time='2026-09-17T10:00:00+08:00', source_sequence=10),
                dict(role='assistant', content='Final', id='a', message_revision='r1',
                     timestamp='2026-09-17T10:01:00+08:00', source_sequence=11)]

    def test_optional_host_metadata_is_selected_from_exact_visible_pair(self):
        result = self.module._sync_source_metadata(self.messages(), 'Task', 'Final')
        self.assertEqual(result['user']['message_id'], 'u')
        self.assertEqual(result['assistant']['source_time'], '2026-09-17T10:01:00+08:00')
        self.assertEqual(result['assistant']['source_sequence'], 11)

    def test_missing_metadata_remains_unknown(self):
        rows = [dict(role='user', content='Task'), dict(role='assistant', content='Final')]
        self.assertEqual(self.module._sync_source_metadata(rows, 'Task', 'Final'), {'user': {}, 'assistant': {}})

    def test_mismatched_tail_is_not_bound_to_older_matching_text(self):
        rows = self.messages() + [dict(role='user', content='Another turn')]
        self.assertEqual(self.module._sync_source_metadata(rows, 'Task', 'Final'), {})

    def test_tools_and_attachment_content_do_not_supply_source_metadata(self):
        rows = [dict(role='tool', content='Task', id='hidden')] + self.messages()
        rows[1]['content'] = [dict(type='text', text='Task')]
        self.assertEqual(self.module._sync_source_metadata(rows, 'Task', 'Final'), {})

    def test_naive_timestamp_is_not_filled_from_capture_time(self):
        rows = self.messages(); rows[0]['source_time'] = '2026-09-17T10:00:00'
        self.assertNotIn('source_time', self.module._sync_source_metadata(rows, 'Task', 'Final')['user'])

    def test_partial_host_identity_and_sequence_do_not_mix_namespaces(self):
        rows = self.messages(); rows[1].pop('id'); rows[1].pop('source_sequence')
        result = self.module._sync_source_metadata(rows, 'Task', 'Final')
        for row in result.values():
            self.assertNotIn('message_id', row)
            self.assertNotIn('message_revision', row)
            self.assertNotIn('source_sequence', row)

    def test_public_sync_preserves_time_and_accepts_revision_with_same_turn_identity(self):
        with tempfile.TemporaryDirectory() as root:
            service = Memleaf.initialize(Path(root) / 'vault')
            provider = self.module.MemleafMemoryProvider()
            provider._client = object()
            provider._auto_process = False
            def call(name, args, **kw):
                self.assertEqual(name, 'capture')
                result = service.capture(**args)
                return {'stored': result.stored, 'duplicate': result.duplicate}
            with patch.object(provider, '_call', side_effect=call):
                rows = self.messages()
                provider.sync_turn('Task', 'Final', session_id='s', turn_number=1, messages=rows)
                first = parse_inbox(service.vault)[0]
                self.assertTrue(first.complete)
                self.assertEqual(first.events[0].source_time, '2026-09-17T10:00:00+08:00')
                rows[0].update(content='Revised task', message_revision='r2', previous_message_revision='r1')
                rows[1].update(content='Revised final', message_revision='r2', previous_message_revision='r1')
                provider.sync_turn('Revised task', 'Revised final', session_id='s', turn_number=1, messages=rows)
                turns = parse_inbox(service.vault)
                self.assertEqual(len(turns), 1)
                self.assertTrue(turns[0].complete)
                self.assertEqual(turns[0].turn_key, first.turn_key)
                self.assertEqual([e.message_revision for e in turns[0].events], ['r2', 'r2'])
                self.assertEqual(turns[0].events[0].content, 'Revised task')

    def test_public_sync_without_metadata_keeps_legacy_compatible_capture(self):
        provider = self.module.MemleafMemoryProvider()
        provider._client = object(); provider._auto_process = False
        with patch.object(provider, '_call', return_value={'stored': True}) as call:
            provider.sync_turn('Task', 'Final', session_id='s', turn_number=2)
        self.assertEqual(call.call_count, 2)
        for invocation in call.call_args_list:
            self.assertNotIn('source_time', invocation.args[1])


if __name__ == '__main__':
    unittest.main()

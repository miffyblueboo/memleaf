"""Source-neutral late retrieval reaches the same strict decision boundary."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from memleaf import Memleaf
from memleaf.config import save_config
from memleaf.index import event_key
from memleaf.planning_context import PlanningContext
from tests.test_general_evidence_admission import candidate
from tests.semantic_fixtures import bind_response

class Backend:
    def __init__(self, key):
        self.key = key
        self.reconciled = 0
    def complete(self, prompt, *, purpose='', **kwargs):
        if prompt.startswith('Target reconciliation input'):
            self.reconciled += 1
            assert 'canonical' in prompt
            return json.dumps({'decision':'NO_CHANGE','target_memory_id':'canonical'})
        if purpose == 'gate':
            raw = json.dumps({'candidates':[candidate('c', self.key, 'alpha stores records in PostgreSQL.', scope='project:alpha')]})
            return bind_response(raw, prompt, purpose)
        raise AssertionError('Duplicate must not reach summarize')

class TargetReconciliationIntegrationTests(unittest.TestCase):
    def test_late_duplicate_is_no_write_for_conversation_document_and_generic_tool(self):
        for tool in (None, 'document.read', 'arbitrary_observer'):
            with self.subTest(tool=tool), tempfile.TemporaryDirectory() as root:
                core = Memleaf(Path(root)/'vault')
                config = core.vault.config()
                config['capture'].update(tool_evidence_mode='bounded', include_attachments=False)
                save_config(core.vault.config_path, config)
                core.create_memory(memory_id='canonical', title='alpha storage design', body='alpha stores records in PostgreSQL.', type='fact', scopes=['project:alpha'])
                text = 'alpha stores records in PostgreSQL.'
                core.capture('hermes','s','t','user',text if tool is None else 'Review the retrieved information.', event_id='u')
                evidence = None if tool is None else [dict(tool_name=tool, call_id='observed', kind='external_observation', result_status='success', execution_status='success', completeness='complete', source_type='tool_result', content=text)]
                core.capture('hermes','s','t','assistant','Reviewed.',event_id='a',tool_evidence=evidence)
                backend = Backend(event_key('u' if tool is None else 'a'))
                before = {str(p):p.read_bytes() for p in core.vault.list_markdown('knowledge')}
                # Reproduce a turn-wide bounded lookup missing the target.
                # Candidate lookup itself remains real and finds the record.
                with patch.object(PlanningContext, '_related', return_value=([], [], [], None)):
                    result = core.process(model=backend)
                self.assertEqual(backend.reconciled, 1)
                self.assertEqual(result['memories_written'], 0)
                self.assertEqual(before, {str(p):p.read_bytes() for p in core.vault.list_markdown('knowledge')})
                self.assertEqual(core.vault.list_markdown('history'), [])

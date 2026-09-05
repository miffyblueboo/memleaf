"""Transport tests: matched calls, current-turn isolation, bounds and redaction."""
import tempfile
from pathlib import Path
import unittest
from memleaf import Memleaf
from memleaf.inbox import parse_inbox_text
from memleaf.provenance import normalize_tool_evidence, observation_record
from memleaf.host_runtime import HostRuntime
from tests.test_hermes_provider import load_provider_module
_bounded_current_tool_evidence = load_provider_module()[0]._bounded_current_tool_evidence

class ToolProvenanceTests(unittest.TestCase):
    def test_roundtrip_and_redaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            core=Memleaf(Path(tmp)/'vault')
            record=observation_record('files.read','c1','Orion uses PostgreSQL. api_key=secret-value-test')
            core.capture('codex','s','t','user','What changed?',event_id='u')
            core.capture('codex','s','t','assistant','Noted.',event_id='a',tool_evidence=[record])
            text=(core.vault.inbox_path/'codex'/'s.md').read_text()
            self.assertNotIn('secret-value-test',text)
            events=parse_inbox_text(text,source='codex',session_id='s')
            self.assertEqual(events[0].events[-1].tool_evidence[0]['call_id'], 'c1')
            self.assertTrue(record['result_digest'])
            self.assertIn('external_observation',text)

    def test_truncation_cannot_look_complete(self):
        value=normalize_tool_evidence([dict(tool_name='terminal.exec',call_id='c',kind='external_observation',content='x'*3000,result_status='success')])[0]
        self.assertEqual(value['result_status'],'truncated')
        self.assertEqual(len(value['content']),2000)

    def test_host_pending_results_do_not_leak_to_other_turns(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime=HostRuntime(Memleaf(Path(tmp)/'vault'),'codex')
            runtime.observe_external_tool(session_id='s',turn_id='t1',tool_name='calendar.read',call_id='c1',payload='Orion deadline 2026-09-30')
            self.assertEqual(len(runtime._tool_evidence('s','t1')),1)
            self.assertEqual(runtime._tool_evidence('s','t2'),[])
            self.assertEqual(runtime._tool_evidence('another','t1'),[])

    def test_hermes_cumulative_history_and_mismatched_ids_are_excluded(self):
        messages=[dict(role='user',content='old question'),
                  dict(role='assistant',tool_calls=[dict(id='old',function=dict(name='files.read',arguments='{}'))]),
                  dict(role='tool',tool_call_id='old',content='old fact'),
                  dict(role='user',content='new question'),
                  dict(role='assistant',tool_calls=[dict(id='new',function=dict(name='github.issue',arguments='{}'))]),
                  dict(role='tool',tool_call_id='other',content='wrong result')]
        self.assertEqual(_bounded_current_tool_evidence(messages),[])
        messages.append(dict(role='tool',tool_call_id='new',content='Orion release blocked'))
        evidence=_bounded_current_tool_evidence(messages)
        self.assertEqual(len(evidence),1)
        self.assertEqual(evidence[0]['call_id'],'new')
        self.assertNotIn('old fact',str(evidence))

    def test_no_boundary_is_not_current_evidence(self):
        self.assertEqual(_bounded_current_tool_evidence([dict(role='tool',tool_call_id='old',content='old fact')]),[])

    def test_memleaf_reads_are_not_external_facts(self):
        self.assertEqual(observation_record('mcp__memleaf__read','c','old knowledge')['kind'],'retrieved_memory')

if __name__=='__main__':unittest.main()

"""Capture permission is shared by core, hooks and the copied Hermes provider."""
from __future__ import annotations
import json
from pathlib import Path
import tempfile
import unittest

from memleaf import Memleaf
from memleaf.admission import analyze_turn_evidence
from memleaf.config import load_config, save_config
from memleaf.evidence_policy import document_arguments, retain_tool_evidence
from memleaf.frontmatter import dump_yaml
from memleaf.host_runtime import HostRuntime
from memleaf.inbox import parse_inbox_file
from memleaf.provenance import observation_record
from tests.test_hermes_provider import load_provider_module


class EvidenceRetentionPolicyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.core = Memleaf(Path(temp.name)/'shared 中文 vault')
        self.runtime = HostRuntime(self.core, 'codex')

    def mode(self, mode, *, attachments=False):
        config = self.core.vault.config()
        config['capture'].update(tool_evidence_mode=mode, include_attachments=attachments)
        save_config(self.core.vault.config_path, config)

    def observe(self, *, tool_input=None):
        self.runtime.observe_external_tool(session_id='s', turn_id='t',
            tool_name='external.inspect', call_id='c', payload='RAW_SENTINEL Orion uses PostgreSQL.',
            tool_input=tool_input)

    def test_new_vault_has_explicit_bounded_mode_and_no_ambiguous_legacy_flag(self):
        config=self.core.vault.config()['capture']
        self.assertEqual(config['tool_evidence_mode'], 'bounded')
        self.assertNotIn('include_tool_output', config)
        self.assertFalse(config['include_attachments'])

    def test_existing_boolean_false_is_not_silently_upgraded_or_written(self):
        config=self.core.vault.config()
        config['capture'].pop('tool_evidence_mode')
        config['capture']['include_tool_output']=False
        raw=dump_yaml(config)
        self.core.vault.config_path.write_text(raw,encoding='utf-8')
        self.assertEqual(load_config(self.core.vault.config_path)['capture']['tool_evidence_mode'], 'metadata')
        self.assertEqual(self.core.vault.config_path.read_text(encoding='utf-8'),raw)
        self.observe()
        self.assertNotIn('RAW_SENTINEL',self.core.vault.host_ingest_path.read_text())

    def test_existing_true_and_explicit_new_setting_have_documented_precedence(self):
        config={'capture':{'include_tool_output':True}}
        record=observation_record('external.inspect','c','RAW_SENTINEL')
        self.assertEqual(retain_tool_evidence([record],config)[0]['content'],'RAW_SENTINEL')
        config['capture']['tool_evidence_mode']='off'
        self.assertEqual(retain_tool_evidence([record],config),[])
        config['capture'].update(include_tool_output=False,tool_evidence_mode='bounded')
        self.assertEqual(retain_tool_evidence([record],config)[0]['content'],'RAW_SENTINEL')

    def test_metadata_keeps_identity_not_body_and_does_not_create_retry_evidence(self):
        self.mode('metadata'); self.observe()
        records=self.runtime._tool_evidence('s','t')
        self.assertEqual(records[0]['call_id'],'c')
        self.assertEqual(records[0]['retention'],'metadata')
        self.assertNotIn('content',records[0])
        units=analyze_turn_evidence([{'event_key':'a','role':'assistant','content':'OK','tool_evidence':records}])
        self.assertEqual(len(units),1)
        self.assertFalse(any(u.eligible for u in units))

    def test_document_disabled_by_default_for_structural_file_inputs(self):
        self.observe(tool_input={'file_path':'/work/requirements.md'})
        self.assertNotIn('RAW_SENTINEL',self.core.vault.host_ingest_path.read_text())
        records=self.runtime._tool_evidence('s','t')
        self.assertEqual(records[0]['source_type'],'document')
        self.assertEqual(records[0]['retention'],'metadata')

    def test_document_opt_in_still_bounded_and_redacted(self):
        self.mode('bounded',attachments=True)
        self.observe(tool_input={'path':'/work/requirements.md'})
        self.assertIn('RAW_SENTINEL',self.runtime._tool_evidence('s','t')[0]['content'])
        raw=observation_record('external.inspect','c','api_key=secret-document-key\n'+'x'*3000)
        raw['source_type']='document'
        result=retain_tool_evidence([raw],self.core.vault.config())[0]
        self.assertNotIn('secret-document-key',str(result))
        self.assertLessEqual(len(result['content']),2000)
        self.assertEqual(result['completeness'],'partial')

    def test_cached_body_is_filtered_if_policy_tightens_before_capture(self):
        self.runtime.capture_visible(session_id='s',turn_id='t',role='user',content='What changed?')
        self.observe()
        self.mode('metadata')
        self.assertNotIn('content',self.runtime._tool_evidence('s','t')[0])
        self.runtime.capture_visible(session_id='s',turn_id='t',role='assistant',content='Observed.')
        self.assertNotIn('RAW_SENTINEL',self.core.vault.session_path('codex','s').read_text())
        self.assertNotIn('RAW_SENTINEL',self.core.vault.host_ingest_path.read_text())
        self.assertEqual(self.runtime._tool_evidence('s','t'),[])

    def test_off_drops_direct_evidence_and_pending_cache(self):
        self.observe(); self.mode('off'); self.observe()
        self.assertEqual(self.runtime._tool_evidence('s','t'),[])
        self.core.capture('codex','s','t','assistant','OK',tool_evidence=[observation_record('ext','c','RAW_SENTINEL')])
        self.assertNotIn('RAW_SENTINEL',self.core.vault.session_path('codex','s').read_text())
        self.assertNotIn('RAW_SENTINEL',self.core.vault.host_ingest_path.read_text())

    def test_relaxing_policy_cannot_reconstruct_previously_discarded_content(self):
        self.mode('metadata');self.observe()
        self.mode('bounded')
        self.assertNotIn('content',self.runtime._tool_evidence('s','t')[0])

    def test_hermes_document_evidence_uses_same_core_policy(self):
        provider=load_provider_module()[0]
        messages=[{'role':'user','content':'Read document'},
            {'role':'assistant','tool_calls':[{'id':'c','function':{'name':'external.inspect','arguments':json.dumps({'path':'/work/contract.md'})}}]},
            {'role':'tool','tool_call_id':'c','content':'RAW_SENTINEL'}]
        records=provider._bounded_current_tool_evidence(messages)
        self.assertEqual(records[0]['source_type'],'document')
        self.core.capture('hermes','s','t','assistant','OK',tool_evidence=records)
        self.assertNotIn('RAW_SENTINEL',self.core.vault.session_path('hermes','s').read_text())

    def test_invalid_mode_and_boolean_fail_before_capture(self):
        for config in ({'tool_evidence_mode':'raw'}, {'tool_evidence_mode':False},
                       {'include_tool_output':'false'}, {'include_attachments':'false'}):
            with self.subTest(config=config),self.assertRaises(ValueError):
                retain_tool_evidence([],{'capture':config})

    def test_structural_detection_does_not_infer_from_tool_names_or_shell_strings(self):
        self.assertTrue(document_arguments({'source':{'attachment_id':'a'}}))
        self.assertTrue(document_arguments({'uri':'file:///work/file.md'}))
        self.assertFalse(document_arguments({'command':'cat /work/file.md'}))
        self.assertFalse(document_arguments({'query':'email attachment follow-up'}))

    def test_public_mcp_schema_accepts_every_current_evidence_field(self):
        from memleaf.mcp_server import _TOOLS
        from memleaf.provenance import TOOL_EVIDENCE_FIELDS
        capture=next(item for item in _TOOLS if item['name']=='capture')
        props=capture['inputSchema']['properties']['tool_evidence']['items']['properties']
        self.assertTrue(TOOL_EVIDENCE_FIELDS.issubset(props))

    def test_legacy_file_without_capture_section_stays_metadata_only(self):
        config = self.core.vault.config()
        config.pop('capture')
        self.core.vault.config_path.write_text(dump_yaml(config), encoding='utf-8')
        self.assertEqual(self.core.vault.config()['capture']['tool_evidence_mode'], 'metadata')
        self.observe()
        self.assertNotIn('RAW_SENTINEL', self.core.vault.host_ingest_path.read_text(encoding='utf-8'))

    def test_saved_policy_round_trip_keeps_legacy_exclusion(self):
        config = self.core.vault.config()
        config['capture'] = {'include_tool_output': False, 'include_attachments': False}
        save_config(self.core.vault.config_path, config)
        for _ in range(2):
            config = self.core.vault.config()
            self.assertEqual(config['capture']['tool_evidence_mode'], 'metadata')
            save_config(self.core.vault.config_path, config)
        self.observe()
        self.assertNotIn('RAW_SENTINEL', self.core.vault.host_ingest_path.read_text(encoding='utf-8'))

    def test_copied_hermes_and_core_agree_on_document_handles(self):
        provider = load_provider_module()[0]
        for arguments in ({'file_id': 'f'}, {'source': {'attachment_id': 'a'}},
                {'uri': 'file:///work/contract.md'}, {'path': '/work/x'},
                {'command': 'cat /work/x'}, {'query': 'attachment requirements'}, {}, None):
            with self.subTest(arguments=arguments):
                self.assertEqual(document_arguments(arguments), provider._has_document_arguments(arguments))

    def test_already_captured_body_does_not_enter_new_model_calls_after_tightening(self):
        from tests.test_phase2_model_decisions import gate_result
        self.core.capture('hermes', 's', 't', 'user', 'What changed?', event_id='u')
        self.core.capture('hermes', 's', 't', 'assistant', 'Observed.', event_id='a',
            tool_evidence=[observation_record('external.inspect', 'c', 'RAW_SENTINEL')])
        original = self.core.vault.session_path('hermes', 's').read_bytes()
        self.assertIn(b'RAW_SENTINEL', original)
        self.mode('metadata')
        prompts = []
        class Backend:
            def complete(backend, prompt, *, purpose='', **kwargs):
                prompts.append(prompt)
                return json.dumps(gate_result(prompt, []))
        result = self.core.process(model=Backend())
        self.assertTrue(prompts)
        self.assertNotIn('RAW_SENTINEL', '\n'.join(prompts))
        self.assertEqual(result['memories_written'], 0)
        self.assertEqual(result['unresolved_evidence_count'], 0)
        # Tightening is not a surprise rewrite of preexisting inbox data.
        self.assertEqual(self.core.vault.session_path('hermes', 's').read_bytes(), original)

    def test_off_and_metadata_do_not_create_unresolved_inventory_tombstones(self):
        from tests.test_phase2_model_decisions import gate_result
        for mode in ('metadata', 'off'):
            with self.subTest(mode=mode):
                self.mode(mode)
                self.core.capture('hermes', mode, 't', 'user', 'List current information.', event_id=mode+'-u')
                self.core.capture('hermes', mode, 't', 'assistant', 'Observed.', event_id=mode+'-a',
                    tool_evidence=[observation_record('external.inspect', str(i), 'RAW_SENTINEL') for i in range(10)])
                class Backend:
                    def complete(backend, prompt, **kwargs):
                        return json.dumps(gate_result(prompt, []))
                result = self.core.process(source='hermes', session_id=mode, model=Backend())
                self.assertEqual(result['memories_written'], 0)
                self.assertEqual(result['unresolved_evidence_count'], 0)

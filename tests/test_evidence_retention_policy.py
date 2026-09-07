"""Capture permission is shared by core, hooks and the copied Hermes provider."""
from __future__ import annotations
import json
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from memleaf import Memleaf
from memleaf.admission import analyze_turn_evidence
from memleaf.config import load_config, save_config
from memleaf.evidence_policy import attachment_arguments, capture_policy_status, document_arguments, retain_tool_evidence
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

    def test_current_policy_modes_do_not_understand_legacy_fields(self):
        record=observation_record('external.inspect','c','RAW_SENTINEL')
        bounded={'capture':{'tool_evidence_mode':'bounded','include_attachments':False}}
        self.assertEqual(retain_tool_evidence([record],bounded)[0]['content'],'RAW_SENTINEL')
        off={'capture':{'tool_evidence_mode':'off','include_attachments':False}}
        self.assertEqual(retain_tool_evidence([record],off),[])
        with self.assertRaises(ValueError):
            retain_tool_evidence([record],{'capture':{'include_tool_output':True}})

    def test_metadata_keeps_identity_not_body_and_does_not_create_retry_evidence(self):
        self.mode('metadata'); self.observe()
        records=self.runtime._tool_evidence('s','t')
        self.assertEqual(records[0]['call_id'],'c')
        self.assertEqual(records[0]['retention'],'metadata')
        self.assertNotIn('content',records[0])
        units=analyze_turn_evidence([{'event_key':'a','role':'assistant','content':'OK','tool_evidence':records}])
        self.assertEqual(len(units),1)
        self.assertFalse(any(u.eligible for u in units))

    def test_document_body_follows_bounded_mode_for_structural_file_inputs(self):
        self.observe(tool_input={'file_path':'/work/requirements.md'})
        self.assertIn('RAW_SENTINEL',self.core.vault.host_ingest_path.read_text())
        records=self.runtime._tool_evidence('s','t')
        self.assertEqual(records[0]['source_type'],'document')
        self.assertNotEqual(records[0].get('retention'),'metadata')

    def test_document_opt_in_still_bounded_and_redacted(self):
        self.mode('bounded',attachments=True)
        self.observe(tool_input={'path':'/work/requirements.md'})
        self.assertIn('RAW_SENTINEL',self.runtime._tool_evidence('s','t')[0]['content'])
        raw=observation_record('external.inspect','c','api_key=secret-document-key\n'+'x'*40000)
        raw['source_type']='document'
        result=retain_tool_evidence([raw],self.core.vault.config())[0]
        self.assertNotIn('secret-document-key',str(result))
        self.assertLessEqual(len(result['content'].encode('utf-8')),32*1024)
        self.assertEqual(result['completeness'],'partial')

    def test_document_body_uses_bounded_mode_without_attachment_opt_in(self):
        self.mode('bounded', attachments=False)
        self.observe(tool_input={'path': '/work/mail-body.txt'})
        record = self.runtime._tool_evidence('s', 't')[0]
        self.assertEqual(record['source_type'], 'document')
        self.assertNotEqual(record.get('retention'), 'metadata')
        self.assertIn('RAW_SENTINEL', record['content'])

    def test_attachment_body_alone_requires_attachment_opt_in(self):
        self.mode('bounded', attachments=False)
        self.observe(tool_input={'attachment_id': 'att-1'})
        denied = self.runtime._tool_evidence('s', 't')[0]
        self.assertEqual(denied['source_type'], 'attachment')
        self.assertEqual(denied['retention'], 'metadata')
        self.assertNotIn('content', denied)

        self.mode('bounded', attachments=True)
        self.runtime.observe_external_tool(session_id='s', turn_id='t2',
            tool_name='external.inspect', call_id='c2', payload='RAW_SENTINEL Orion uses PostgreSQL.',
            tool_input={'attachment_id': 'att-2'})
        allowed = self.runtime._tool_evidence('s', 't2')[0]
        self.assertEqual(allowed['source_type'], 'attachment')
        self.assertNotEqual(allowed.get('retention'), 'metadata')
        self.assertIn('RAW_SENTINEL', allowed['content'])

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
        self.assertIn('RAW_SENTINEL',self.core.vault.session_path('hermes','s').read_text())

    def test_invalid_mode_and_boolean_fail_before_capture(self):
        for config in ({'tool_evidence_mode':'raw'}, {'tool_evidence_mode':False},
                       {'include_attachments':'false'}):
            with self.subTest(config=config),self.assertRaises(ValueError):
                retain_tool_evidence([],{'capture':config})

    def test_structural_detection_does_not_infer_from_tool_names_or_shell_strings(self):
        self.assertTrue(attachment_arguments({'source': {'attachment_id': 'a'}}))
        self.assertFalse(document_arguments({'source': {'attachment_id': 'a'}}))
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

    def test_copied_hermes_and_core_agree_on_attachment_handles(self):
        provider = load_provider_module()[0]
        for arguments in ({'attachment_id': 'a'}, {'source': {'attachment_id': 'a'}},
                          {'path': '/work/x'}, {'command': 'cat /work/x'}, {}, None):
            with self.subTest(arguments=arguments):
                self.assertEqual(attachment_arguments(arguments), provider._has_attachment_arguments(arguments))

    def test_capture_policy_status_is_safe_and_consistent(self):
        self.assertEqual(
            capture_policy_status(self.core.vault.config()),
            {'tool_evidence_mode': 'bounded', 'include_attachments': False, 'body_retention': 'bounded'},
        )
        config = self.core.vault.config()
        config['capture'].update(tool_evidence_mode='metadata', include_attachments=True)
        save_config(self.core.vault.config_path, config)
        self.assertEqual(
            capture_policy_status(self.core.vault.config()),
            {'tool_evidence_mode': 'metadata', 'include_attachments': True, 'body_retention': 'metadata'},
        )

    def test_mcp_stats_exposes_effective_capture_policy(self):
        from memleaf.mcp_server import _invoke_tool
        value = _invoke_tool(self.core, 'stats', {})['structuredContent']
        self.assertEqual(value['capture'], capture_policy_status(self.core.vault.config()))

    def test_init_cli_human_status_exposes_capture_policy(self):
        from memleaf.cli import _print_human_result
        output = StringIO()
        with redirect_stdout(output):
            _print_human_result({
                'dry_run': True,
                'vault': str(self.core.vault.root),
                'agents': {},
                'model': {'status': 'not_configured'},
                'agents_state_path': str(self.core.vault.agents_state_path),
                'capture': capture_policy_status(self.core.vault.config()),
            })
        self.assertIn('capture: bounded (attachments=disabled)', output.getvalue())

    def test_hermes_status_exposes_effective_capture_policy(self):
        provider_module = load_provider_module()[0]
        provider = provider_module.MemleafMemoryProvider()
        hermes_home = self.core.vault.root.parent / 'hermes-status'
        hermes_home.mkdir()
        (hermes_home / 'memleaf.json').write_text(
            json.dumps({'vault': str(self.core.vault.root)}), encoding='utf-8'
        )
        provider._hermes_home = str(hermes_home)
        stats_client = type('StatsClient', (), {
            'call_tool': lambda _client, name, arguments, core=self.core: {
                'capture': capture_policy_status(core.vault.config())
            },
            'close': lambda _client: None,
        })()
        with patch.object(provider_module, '_resolve_command', return_value='memleaf-mcp'), \
             patch.object(provider_module, '_MCPClient', return_value=stats_client):
            status = provider.get_status_config({})
        self.assertEqual(status['capture'], capture_policy_status(self.core.vault.config()))

    def test_standalone_hermes_status_reads_legacy_true_as_bounded_from_core_stats(self):
        provider_module = load_provider_module()[0]
        config = self.core.vault.config()
        config['capture'] = {'include_tool_output': True, 'include_attachments': False}
        self.core.vault.config_path.write_text(dump_yaml(config), encoding='utf-8')
        hermes_home = self.core.vault.root.parent / 'hermes-legacy-status'
        hermes_home.mkdir()
        (hermes_home / 'memleaf.json').write_text(
            json.dumps({'vault': str(self.core.vault.root)}), encoding='utf-8'
        )

        from memleaf.mcp_server import _invoke_tool

        class StatsClient:
            def __init__(self):
                self.calls = []

            def call_tool(self, name, arguments):
                self.calls.append((name, dict(arguments)))
                return _invoke_tool(self.core, name, arguments)['structuredContent']

            def close(self):
                return None

        stats_client = StatsClient()
        stats_client.core = self.core
        with patch.object(provider_module, '_resolve_command', return_value='memleaf-mcp'), \
             patch.object(provider_module, '_MCPClient', return_value=stats_client):
            status = provider_module.MemleafMemoryProvider()
            status._hermes_home = str(hermes_home)
            value = status.get_status_config({})

        self.assertEqual(
            value['capture'],
            {'tool_evidence_mode': 'bounded', 'include_attachments': False, 'body_retention': 'bounded'},
        )
        self.assertEqual(stats_client.calls, [('stats', {})])

    def test_standalone_hermes_status_reports_unknown_when_stats_is_unavailable(self):
        provider_module = load_provider_module()[0]
        hermes_home = self.core.vault.root.parent / 'hermes-unavailable-status'
        hermes_home.mkdir()
        (hermes_home / 'memleaf.json').write_text(
            json.dumps({'vault': str(self.core.vault.root)}), encoding='utf-8'
        )

        class FailedStatsClient:
            def call_tool(self, name, arguments):
                raise RuntimeError('stats unavailable')

            def close(self):
                return None

        with patch.object(provider_module, '_resolve_command', return_value='memleaf-mcp'), \
             patch.object(provider_module, '_MCPClient', return_value=FailedStatsClient()):
            status = provider_module.MemleafMemoryProvider()
            status._hermes_home = str(hermes_home)
            value = status.get_status_config({})

        self.assertEqual(
            value['capture'],
            {
                'tool_evidence_mode': 'unknown',
                'include_attachments': 'unknown',
                'body_retention': 'unknown',
                'source': 'mcp_unavailable',
            },
        )

    def test_explicit_attachment_id_has_the_same_core_host_and_provider_classification(self):
        provider_module = load_provider_module()[0]
        arguments = {'attachment_id': 'attachment-1'}
        messages = [
            {'role': 'user', 'content': 'Read the attachment.'},
            {'role': 'assistant', 'tool_calls': [{
                'id': 'call-attachment',
                'function': {
                    'name': 'external.inspect',
                    'arguments': json.dumps(arguments),
                },
            }]},
            {'role': 'tool', 'tool_call_id': 'call-attachment', 'content': 'ATTACHMENT_BODY'},
        ]
        provider_records = provider_module._bounded_current_tool_evidence(messages)
        self.assertEqual(len(provider_records), 1)
        self.assertEqual(provider_records[0]['source_type'], 'attachment')
        self.assertTrue(attachment_arguments(arguments))
        self.assertTrue(provider_module._has_attachment_arguments(arguments))
        self.assertFalse(document_arguments(arguments))
        self.assertFalse(provider_module._has_document_arguments(arguments))

        self.runtime.observe_external_tool(
            session_id='attachment-session',
            turn_id='attachment-turn',
            tool_name='external.inspect',
            call_id='call-attachment',
            payload='ATTACHMENT_BODY',
            tool_input=arguments,
        )
        host_records = self.runtime._tool_evidence('attachment-session', 'attachment-turn')
        self.assertEqual(len(host_records), 1)
        self.assertEqual(host_records[0]['source_type'], provider_records[0]['source_type'])

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

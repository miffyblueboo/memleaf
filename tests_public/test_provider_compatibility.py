"""Provider bridge compatibility, not authentication or a real Hermes installation."""
from __future__ import annotations

import io
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from memleaf import Memleaf, __version__
from memleaf import installer, mcp_server
from memleaf.provider_compatibility import (
    BUILD_META, PROVIDER_FILES, COMPATIBILITY_CODES, READ_ONLY_TOOLS,
    compatibility, provider_build, valid_build,
)

PACKAGE = Path(mcp_server.__file__).parent
PROVIDER = PACKAGE / 'hermes_provider'


def load_client():
    base = types.ModuleType('agent.memory_provider')
    base.MemoryProvider = type('MemoryProvider', (), {})
    base.RecallStatus = type('RecallStatus', (), {})
    with patch.dict(sys.modules, {'agent': types.ModuleType('agent'), 'agent.memory_provider': base}):
        return importlib.import_module('memleaf.hermes_provider._mcp_client')


class ProviderBuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / '复制 插件'
        shutil.copytree(PROVIDER, self.root, ignore=shutil.ignore_patterns('__pycache__'))

    def test_exact_copy_has_same_identity_without_location(self):
        result = provider_build(self.root)
        self.assertTrue(valid_build(result))

    def test_provider_manifest_version_matches_core(self):
        self.assertEqual(installer._provider_manifest_version(PROVIDER), __version__)
        self.assertEqual(result, provider_build(PROVIDER))
        self.assertNotIn(str(self.root), json.dumps(result))

    def test_same_release_different_behavior_is_not_equal(self):
        before = provider_build(self.root)
        p = self.root / '_provider.py'
        p.write_bytes(p.read_bytes() + b'\n# changed unreleased build\n')
        self.assertNotEqual(before, provider_build(self.root))
        self.assertEqual((self.root/'plugin.yaml').read_bytes(), (PROVIDER/'plugin.yaml').read_bytes())

    def test_readme_and_bytecode_are_not_behavior_identity(self):
        before = provider_build(self.root)
        (self.root/'README.md').write_text('operator notes', encoding='utf-8')
        (self.root/'__pycache__').mkdir(exist_ok=True)
        (self.root/'__pycache__'/'noise.pyc').write_bytes(b'ignored')
        self.assertEqual(before, provider_build(self.root))

    def test_every_required_resource_changes_identity(self):
        before = provider_build(self.root)
        for name in PROVIDER_FILES:
            with self.subTest(name=name):
                p=self.root/name; raw=p.read_bytes()
                p.write_bytes(raw+b'\n')
                self.assertNotEqual(before, provider_build(self.root))
                p.write_bytes(raw)

    def test_missing_or_nonregular_or_empty_resource_fails_closed(self):
        p=self.root/'_provider.py';raw=p.read_bytes();p.unlink()
        self.assertFalse(valid_build(provider_build(self.root)))
        p.mkdir()
        self.assertFalse(valid_build(provider_build(self.root)))
        p.rmdir();p.write_bytes(b'')
        self.assertFalse(valid_build(provider_build(self.root)))
        p.write_bytes(raw)
        self.assertTrue(valid_build(provider_build(self.root)))

    def test_file_bound_does_not_read_oversized_input(self):
        with patch('memleaf.provider_compatibility.MAX_FILE_BYTES', 1):
            self.assertFalse(valid_build(provider_build(self.root)))

    def test_total_bound_is_independent(self):
        with patch('memleaf.provider_compatibility.MAX_BUNDLE_BYTES', 1):
            self.assertFalse(valid_build(provider_build(self.root)))

    def test_changed_resource_during_read_is_unavailable(self):
        original=Path.open; target=self.root/'_provider.py'
        def opening(path,*args,**kwargs):
            stream=original(path,*args,**kwargs)
            if path == target:
                with original(path,'ab') as other:other.write(b'\n# mutation\n')
            return stream
        with patch.object(Path,'open',opening):
            self.assertFalse(valid_build(provider_build(self.root)))

    def test_descriptor_validation_is_strict(self):
        good=provider_build(self.root)
        for bad in (None, [], {}, {'schema':True,'digest':good['digest']},
                    {'schema':2,'digest':good['digest']}, {'schema':1,'digest':'bad'},
                    {**good,'path':'private'}, {'schema':1,'digest':[]}):
            with self.subTest(bad=bad):self.assertFalse(valid_build(bad))

    def test_runtime_diagnostic_reasons_are_distinct(self):
        a=provider_build(self.root);b={**a,'digest':'0'*64}
        self.assertEqual(compatibility(a,a,a),'compatible')
        self.assertEqual(compatibility(a,b,b),'provider_restart_required')
        self.assertEqual(compatibility(a,a,b),'provider_core_mismatch')
        self.assertEqual(compatibility(a,a,None),'core_build_unverified')
        self.assertEqual(compatibility(None,a,a),'provider_build_unavailable')

    def test_shared_helper_copies_are_identical(self):
        self.assertEqual((PACKAGE/'provider_compatibility.py').read_bytes(),
                         (PROVIDER/'provider_compatibility.py').read_bytes())

    def test_installer_copies_helper_and_validates_all_files(self):
        target=installer._copy_provider(Path(self.tmp.name)/'hermes')
        self.assertEqual(provider_build(target),provider_build(PROVIDER))
        self.assertTrue((target/'provider_compatibility.py').is_file())

    def test_installer_detects_corrupted_copy_not_just_manifest_version(self):
        replace=os.replace
        def corrupt(source,target):
            replace(source,target)
            if Path(target).name=='memleaf':(Path(target)/'_provider.py').write_bytes(b'# incomplete copy\n')
        with patch.object(installer.os,'replace',side_effect=corrupt):
            with self.assertRaisesRegex(RuntimeError,'build does not match'):
                installer._copy_provider(Path(self.tmp.name)/'hermes')


class InstallerProbeTests(unittest.TestCase):
    def test_probe_uses_only_selected_binary_without_vault(self):
        good=provider_build(PROVIDER)
        with patch.object(installer,'_run',return_value=subprocess.CompletedProcess([],0,json.dumps(good),'')) as run:
            self.assertEqual(installer._probe_memleaf_provider_build('/selected/memleaf-mcp'),good)
        run.assert_called_once_with(['/selected/memleaf-mcp','--provider-build'],timeout=10)

    def test_failed_old_binary_is_not_assumed_compatible(self):
        with patch.object(installer,'_run',return_value=subprocess.CompletedProcess([],2,'','private error')):
            self.assertIsNone(installer._probe_memleaf_provider_build('old'))

    def test_probe_rejects_duplicate_or_oversized_or_invalid_identity(self):
        good=provider_build(PROVIDER)
        duplicate='{"schema":1,"schema":1,"digest":"'+good['digest']+'"}'
        for raw in (duplicate,'x'*1025,'[]','{"schema":2}', 'not JSON'):
            with self.subTest(raw=raw[:40]),patch.object(installer,'_run',return_value=subprocess.CompletedProcess([],0,raw,'')):
                self.assertIsNone(installer._probe_memleaf_provider_build('selected'))

    def test_probe_timeout_is_not_a_success(self):
        with patch.object(installer,'_run',side_effect=subprocess.TimeoutExpired('private',10)):
            self.assertIsNone(installer._probe_memleaf_provider_build('selected'))

    def test_copy_upgrade_readback_checks_existing_directory(self):
        with tempfile.TemporaryDirectory() as root:
            home=Path(root)/'hermes'
            installer._copy_provider(home)
            replace=os.replace
            def corrupt(source,target):
                replace(source,target)
                if Path(target).name=='_provider.py':Path(target).write_bytes(b'# incomplete\n')
            with patch.object(installer.os,'replace',side_effect=corrupt):
                with self.assertRaisesRegex(RuntimeError,'build does not match'):installer._copy_provider(home)

    def test_install_stops_before_vault_or_host_changes_on_build_mismatch(self):
        with tempfile.TemporaryDirectory() as root:
            home=Path(root);selected=home/'vault';command=home/'memleaf-mcp'
            detection=types.SimpleNamespace(detected=True,confidence='high',executable='hermes',config_path=home/'config.yaml')
            adapter=types.SimpleNamespace(detect=lambda:detection,platform=os.name)
            with patch.object(installer,'_home_from_environment',return_value=home),\
                 patch.object(installer,'_select_vault_path',return_value=(selected,'explicit')),\
                 patch.object(installer,'HermesAdapter',return_value=adapter),\
                 patch.object(installer,'_memleaf_mcp_command',return_value=command),\
                 patch.object(installer,'_choose_hermes_mcp_command',return_value=(str(command),{})),\
                 patch.object(installer,'_provider_mcp_command',return_value=command),\
                 patch.object(installer,'_hermes_public_mcp_command',return_value=command),\
                 patch.object(installer,'_probe_memleaf_provider_build',return_value=None),\
                 patch.object(installer.Vault,'initialize',side_effect=AssertionError('must not initialize')):
                result=installer.install_hermes(vault_path=selected)
            self.assertEqual(result['stage'],'provider_build')
            self.assertEqual(result['status'],'failure')
            self.assertFalse(selected.exists())
            self.assertFalse((home/'config.yaml').exists())

    def test_copied_provider_loads_without_core_import(self):
        with tempfile.TemporaryDirectory() as root:
            target=installer._copy_provider(Path(root)/'hermes')
            code="""import importlib.util,sys,types
from pathlib import Path
base=types.ModuleType('agent.memory_provider')
base.MemoryProvider=type('MemoryProvider',(),{})
base.RecallStatus=type('RecallStatus',(),{})
sys.modules['agent']=types.ModuleType('agent');sys.modules['agent.memory_provider']=base
spec=importlib.util.spec_from_file_location('standalone_memleaf',Path(sys.argv[1])/'__init__.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
assert module.MemleafMemoryProvider().name=='memleaf'
assert 'memleaf' not in sys.modules
assert module.valid_build(module._LOADED_PROVIDER_BUILD)
"""
            run=subprocess.run([sys.executable,'-I','-c',code,str(target)],capture_output=True,timeout=15)
            self.assertEqual(run.returncode,0,run.stderr.decode(errors='replace'))

    def test_path_and_handle_ctime_domains_are_not_compared(self):
        fstat=os.fstat
        def observation(fd):
            original=fstat(fd)
            return types.SimpleNamespace(st_dev=original.st_dev,st_ino=original.st_ino,
                st_size=original.st_size,st_mtime_ns=original.st_mtime_ns,
                st_ctime_ns=original.st_ctime_ns+1,st_mode=original.st_mode)
        with patch('memleaf.provider_compatibility.os.fstat',side_effect=observation):
            self.assertTrue(valid_build(provider_build(PROVIDER)))


class ServerCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.service=Memleaf.initialize(Path(self.tmp.name)/'vault')
        self.connection={}
        self.good=dict(mcp_server._SERVER_PROVIDER_BUILD)

    def request(self,method,params=None):
        return mcp_server._dispatch({'jsonrpc':'2.0','id':1,'method':method,'params':params or {}},
                                    self.service,connection=self.connection)

    def hello(self,build=None,name='hermes-memleaf'):
        return self.request('initialize',{'protocolVersion':'2024-11-05',
            'clientInfo':{'name':name,'version':'0.1'},'_meta':{BUILD_META:build}})

    def test_initialize_metadata_no_vault_scan_or_model(self):
        with patch.object(self.service,'stats',side_effect=AssertionError('must not scan')):
            result=self.hello(self.good)['result']
        self.assertEqual(result['_meta'][BUILD_META],self.good)
        self.assertEqual(result['serverInfo']['version'],'0.2.67')

    def test_compatible_bridge_dispatches_capture(self):
        self.hello(self.good)
        with patch.object(mcp_server,'_invoke_tool',return_value={'ok':True}) as call:
            self.request('tools/call',{'name':'capture','arguments':{}})
        call.assert_called_once()

    def test_old_provider_without_descriptor_cannot_mutate(self):
        self.hello()
        with patch.object(mcp_server,'_invoke_tool',side_effect=AssertionError('must not dispatch')):
            result=self.request('tools/call',{'name':'capture','arguments':{}})['result']
        self.assertTrue(result['isError'])
        self.assertEqual(result['structuredContent']['error']['stage'],'compatibility')

    def test_same_version_mismatch_does_not_dispatch_any_write_tool(self):
        self.hello({**self.good,'digest':'0'*64})
        for name in set(mcp_server._TOOL_BY_NAME)-READ_ONLY_TOOLS:
            with self.subTest(name=name),patch.object(mcp_server,'_invoke_tool',side_effect=AssertionError()):
                result=self.request('tools/call',{'name':name,'arguments':{}})['result']
                self.assertTrue(result['isError'])

    def test_incompatible_bridge_can_still_read(self):
        self.hello()
        for name in READ_ONLY_TOOLS & set(mcp_server._TOOL_BY_NAME):
            with self.subTest(name=name),patch.object(mcp_server,'_invoke_tool',return_value={}) as call:
                self.request('tools/call',{'name':name,'arguments':{}})
                call.assert_called_once()

    def test_server_changed_after_initialize_refuses_write(self):
        self.hello(self.good)
        with patch.object(mcp_server,'provider_build',return_value={**self.good,'digest':'0'*64}),\
             patch.object(mcp_server,'_invoke_tool',side_effect=AssertionError()):
            result=self.request('tools/call',{'name':'capture','arguments':{}})['result']
        self.assertEqual(result['structuredContent']['error']['code'],'provider_restart_required')

    def test_other_mcp_clients_are_not_falsely_subject_to_hermes_bundle(self):
        self.hello(name='other-agent')
        with patch.object(mcp_server,'_invoke_tool',return_value={}) as call:
            self.request('tools/call',{'name':'capture','arguments':{}})
        call.assert_called_once()

    def test_connection_identity_is_not_global_or_on_vault(self):
        self.hello()
        another={}
        with patch.object(mcp_server,'_invoke_tool',return_value={}) as call:
            mcp_server._dispatch({'jsonrpc':'2.0','id':2,'method':'tools/call',
                'params':{'name':'capture','arguments':{}}},self.service,connection=another)
        call.assert_called_once()

    def test_rejected_capture_does_not_create_source(self):
        self.hello()
        before={p.relative_to(self.service.vault.root):p.read_bytes() for p in self.service.vault.root.rglob('*') if p.is_file()}
        self.request('tools/call',{'name':'capture','arguments':{
            'source':'hermes','session_id':'s','role':'user','content':'not captured','turn_id':'1'}})
        after={p.relative_to(self.service.vault.root):p.read_bytes() for p in self.service.vault.root.rglob('*') if p.is_file()}
        self.assertEqual(before,after)

    def test_serve_retains_negotiation_but_not_across_streams(self):
        hello={'jsonrpc':'2.0','id':1,'method':'initialize','params':{'clientInfo':{'name':'hermes-memleaf'}}}
        capture={'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'capture','arguments':{}}}
        output=io.StringIO()
        mcp_server.serve(self.service,input_stream=io.StringIO(json.dumps(hello)+'\n'+json.dumps(capture)+'\n'),output_stream=output)
        self.assertTrue(json.loads(output.getvalue().splitlines()[1])['result']['isError'])
        with patch.object(mcp_server,'_invoke_tool',return_value={}) as invoke:
            mcp_server.serve(self.service,input_stream=io.StringIO(json.dumps(capture)+'\n'),output_stream=io.StringIO())
        invoke.assert_called_once()

    def test_cli_identity_does_not_initialize_requested_vault_or_import_hermes(self):
        missing=Path(self.tmp.name)/'not-created'
        command=[sys.executable,'-m','memleaf.mcp_server','--vault',str(missing),'--provider-build']
        p=subprocess.run(command,capture_output=True,timeout=15)
        self.assertEqual(p.returncode,0,p.stderr.decode(errors='replace'))
        self.assertTrue(valid_build(json.loads(p.stdout)))
        self.assertFalse(missing.exists())


class ClientCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.module=load_client()

    def setUp(self):
        self.client=self.module._MCPClient('unused','unused',5)
        self.addCleanup(self.client.close)
        self.good=dict(self.module._LOADED_PROVIDER_BUILD)
        self.client.server_provider_build=self.good

    def test_client_stops_write_before_rpc_when_peer_unknown(self):
        self.client.server_provider_build=None
        with patch.object(self.client,'_start_locked'),patch.object(self.client,'_request_locked') as call:
            with self.assertRaises(self.module._MCPToolError) as error:self.client.call_tool('capture',{})
        call.assert_not_called()
        self.assertEqual(error.exception.code,'core_build_unverified')
        self.assertEqual(error.exception.stage,'compatibility')

    def test_client_read_is_usable_when_peer_unknown(self):
        self.client.server_provider_build=None
        with patch.object(self.client,'_start_locked'),patch.object(self.client,'_request_locked',return_value={'structuredContent':{'ok':True}}):
            self.assertEqual(self.client.call_tool('stats',{}),{'ok':True})

    def test_frozen_import_identity_not_replaced_by_new_client(self):
        changed={**self.good,'digest':'0'*64}
        with patch.object(self.module,'provider_build',return_value=changed):
            other=self.module._MCPClient('unused','unused',5);other.server_provider_build=changed
            self.assertEqual(other.compatibility_status()['status'],'provider_restart_required')

    def test_unknown_tool_is_not_read_only_bypass(self):
        self.client.server_provider_build=None
        with patch.object(self.client,'_start_locked'),patch.object(self.client,'_request_locked') as call:
            with self.assertRaises(self.module._MCPToolError):self.client.call_tool('future_write',{})
        call.assert_not_called()

    def test_diagnostics_neither_start_process_nor_echo_paths(self):
        with patch.object(self.client,'_start_locked',side_effect=AssertionError()):
            result=self.client.compatibility_status()
        self.assertEqual(result,{'status':'compatible','writes_allowed':True})

    def test_compatibility_errors_survive_existing_safe_projection(self):
        for code in COMPATIBILITY_CODES:
            value={'isError':True,'structuredContent':{'error':{'code':code,'stage':'compatibility','message':'private path'}}}
            fields=self.module._mcp_error_fields(value)
            error=self.module._MCPToolError(*fields)
            self.assertEqual((error.code,error.stage),(code,'compatibility'))
            self.assertNotIn('private',str(error))

    def test_close_revokes_previous_handshake_observation(self):
        self.client.close()
        self.assertEqual(self.client.compatibility_status()['status'],'core_build_unverified')

    def test_provider_notice_never_claims_rejected_capture_is_pending(self):
        base=types.ModuleType('agent.memory_provider')
        base.MemoryProvider=type('MemoryProvider',(),{})
        base.RecallStatus=type('RecallStatus',(),{})
        with patch.dict(sys.modules, {'agent':types.ModuleType('agent'),'agent.memory_provider':base}):
            module=importlib.import_module('memleaf.hermes_provider._provider')
        provider=module.MemleafMemoryProvider();provider._client=self.client
        self.client.server_provider_build=None
        text=provider._runtime_compatibility_notice()
        self.assertIn('does not confirm an inbox receipt',text)
        self.assertIn('Local retrieval remains available',text)
        self.assertNotIn('captured turn remains pending',text)

    def _reject_from_server(self, code='provider_restart_required', stage='compatibility'):
        result = {'isError': True, 'structuredContent': {
            'error': {'code': code, 'stage': stage, 'message': 'private diagnostic'}}}
        with patch.object(self.client, '_start_locked'), patch.object(
                self.client, '_request_locked', return_value=result):
            with self.assertRaises(self.module._MCPToolError) as error:
                self.client.call_tool('capture', {})
        return error.exception

    def test_server_rejection_updates_observed_client_status(self):
        for code in COMPATIBILITY_CODES:
            with self.subTest(code=code):
                self.client.close()
                self.client.server_provider_build = self.good
                self._reject_from_server(code)
                self.assertEqual(self.client.compatibility_status(),
                                 {'status': code, 'writes_allowed': False})

    def test_server_rejection_survives_successful_read(self):
        self._reject_from_server()
        with patch.object(self.client, '_start_locked'), patch.object(
                self.client, '_request_locked', return_value={'structuredContent': {'ok': True}}):
            self.assertEqual(self.client.call_tool('stats', {}), {'ok': True})
        self.assertEqual(self.client.compatibility_status(),
                         {'status': 'provider_restart_required', 'writes_allowed': False})

    def test_refused_writes_retain_status_without_transport_restart(self):
        self._reject_from_server()
        with patch.object(self.client, '_start_locked'), patch.object(
                self.client, '_request_locked') as request, patch.object(
                self.client, '_close_locked', wraps=self.client._close_locked) as close:
            for _ in range(3):
                with self.assertRaises(self.module._MCPToolError) as error:
                    self.client.call_tool('capture', {})
                self.assertEqual(error.exception.code, 'provider_restart_required')
            request.assert_not_called()
            close.assert_not_called()
        self.assertFalse(self.client.compatibility_status()['writes_allowed'])

    def test_unrelated_server_error_does_not_revoke_compatibility(self):
        for code, stage in [('model_failed', 'summarize'),
                            ('provider_core_mismatch', 'summarize'),
                            ('private unknown error', 'compatibility')]:
            with self.subTest(code=code, stage=stage):
                self._reject_from_server(code, stage)
                self.assertEqual(self.client.compatibility_status(),
                                 {'status': 'compatible', 'writes_allowed': True})

    def test_close_clears_observed_peer_rejection(self):
        self._reject_from_server()
        self.client.close()
        self.assertEqual(self.client.compatibility_status()['status'], 'core_build_unverified')
        self.client.server_provider_build = self.good
        self.assertEqual(self.client.compatibility_status(),
                         {'status': 'compatible', 'writes_allowed': True})

    def test_local_build_problem_takes_precedence_over_peer_rejection(self):
        self._reject_from_server('provider_core_mismatch')
        with patch.object(self.module, 'provider_build',
                          return_value={**self.good, 'digest': '0' * 64}):
            self.assertEqual(self.client.compatibility_status()['status'],
                             'provider_restart_required')

    def test_provider_notice_reflects_late_server_rejection(self):
        base = types.ModuleType('agent.memory_provider')
        base.MemoryProvider = type('MemoryProvider', (), {})
        base.RecallStatus = type('RecallStatus', (), {})
        with patch.dict(sys.modules, {'agent': types.ModuleType('agent'), 'agent.memory_provider': base}):
            module = importlib.import_module('memleaf.hermes_provider._provider')
        provider = module.MemleafMemoryProvider()
        provider._client = self.client
        self.assertEqual(provider._runtime_compatibility_notice(), '')
        self._reject_from_server()
        text = provider._runtime_compatibility_notice()
        self.assertIn('does not confirm an inbox receipt', text)
        self.assertIn('Local retrieval remains available', text)
        self.assertNotIn('private diagnostic', text)
        self.assertNotIn('captured turn remains pending', text)

    def test_real_stdio_reconnect_clears_observed_peer_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            service = Memleaf.initialize(Path(directory) / '库')
            client = self.module._MCPClient(sys.executable, str(service.vault.root), 10)
            popen = subprocess.Popen
            def start(args, **kwargs):
                return popen([sys.executable, '-m', 'memleaf.mcp_server', *args[1:]], **kwargs)
            try:
                with patch.object(self.module.subprocess, 'Popen', side_effect=start):
                    client.call_tool('stats', {})
                    original_process = client._process
                    rejection = {'isError': True, 'structuredContent': {'error': {
                        'code': 'provider_restart_required', 'stage': 'compatibility'}}}
                    with patch.object(client, '_request_locked', return_value=rejection):
                        with self.assertRaises(self.module._MCPToolError):
                            client.call_tool('capture', {})
                    client.call_tool('stats', {})
                    self.assertIs(client._process, original_process)
                    self.assertFalse(client.compatibility_status()['writes_allowed'])
                    client.close()
                    client.call_tool('stats', {})
                    self.assertIsNot(client._process, original_process)
                    self.assertTrue(client.compatibility_status()['writes_allowed'])
                    result = client.call_tool('capture', {'source': 'hermes', 'session_id': 's',
                        'role': 'user', 'content': '事实', 'turn_id': '1'})
                    self.assertTrue(result['stored'])
            finally:
                client.close()

    def test_real_stdio_bridge_capture_and_reconnect(self):
        with tempfile.TemporaryDirectory() as directory:
            service=Memleaf.initialize(Path(directory)/'库')
            client=self.module._MCPClient(sys.executable,str(service.vault.root),10)
            popen=subprocess.Popen
            def start(args,**kwargs):
                return popen([sys.executable,'-m','memleaf.mcp_server',*args[1:]],**kwargs)
            try:
                with patch.object(self.module.subprocess,'Popen',side_effect=start):
                    client.call_tool('stats',{})
                    self.assertTrue(client.compatibility_status()['writes_allowed'])
                    result=client.call_tool('capture',{'source':'hermes','session_id':'s','role':'user','content':'事实','turn_id':'1'})
                    self.assertTrue(result['stored'])
                    client.close()
                    duplicate=client.call_tool('capture',{'source':'hermes','session_id':'s','role':'user','content':'事实','turn_id':'1'})
                    self.assertTrue(duplicate['duplicate'])
            finally:client.close()

    def test_real_stdio_old_bridge_rejected_but_stats_work(self):
        with tempfile.TemporaryDirectory() as directory:
            service=Memleaf.initialize(Path(directory)/'库')
            hello={'jsonrpc':'2.0','id':1,'method':'initialize','params':{'clientInfo':{'name':'hermes-memleaf'}}}
            capture={'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'capture','arguments':{'source':'hermes','session_id':'s','role':'user','content':'fact'}}}
            stats={'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'stats','arguments':{}}}
            p=subprocess.run([sys.executable,'-m','memleaf.mcp_server','--vault',str(service.vault.root)],
                input=('\n'.join(json.dumps(x) for x in (hello,capture,stats))+'\n').encode(),capture_output=True,timeout=15)
            self.assertEqual(p.returncode,0,p.stderr.decode(errors='replace'))
            rows=[json.loads(line)['result'] for line in p.stdout.decode().splitlines()]
            self.assertTrue(rows[1]['isError']);self.assertFalse(rows[2].get('isError',False))


if __name__=='__main__':unittest.main()

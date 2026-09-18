"""Run these OS primitives natively; do not simulate Windows by patching os.name."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from memleaf import Memleaf
from memleaf.locking import VaultLock, atomic_write_bytes, atomic_write_text


class PlatformContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='ml-platform-')
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'中文 路径'
        self.root.mkdir()

    def test_utf8_atomic_write_is_locale_independent_and_exact_lf(self):
        path=self.root/'记忆.md'
        atomic_write_text(path,'中文 🔒\n第二行\n')
        self.assertEqual(path.read_bytes(),'中文 🔒\n第二行\n'.encode())
        atomic_write_bytes(path,b'new\r\n')
        self.assertEqual(path.read_bytes(),b'new\r\n')
        self.assertFalse(list(self.root.glob('*.tmp')))

    def test_native_lock_serializes_two_processes(self):
        lock=self.root/'vault.lock';started=self.root/'started';entered=self.root/'entered'
        program='''from pathlib import Path
import sys
from memleaf.locking import VaultLock
lock,started,entered=map(Path,sys.argv[1:])
started.write_bytes(b"ready")
with VaultLock(lock): entered.write_bytes(b"entered")
'''
        child=None
        try:
            with VaultLock(lock):
                child=subprocess.Popen([sys.executable,'-c',program,str(lock),str(started),str(entered)],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                until=time.monotonic()+10
                while not started.exists() and child.poll() is None and time.monotonic()<until:time.sleep(.02)
                self.assertTrue(started.exists(),'child did not reach lock')
                time.sleep(.15)
                self.assertFalse(entered.exists(),'another process entered the held Vault lock')
            _,err=child.communicate(timeout=15)
            self.assertEqual(child.returncode,0,err.decode('utf-8',errors='replace'))
            self.assertEqual(entered.read_bytes(),b'entered')
        finally:
            if child is not None and child.poll() is None:child.kill();child.communicate()

    def test_process_exit_releases_native_lock(self):
        lock=self.root/'vault.lock'
        program='import os,sys; from pathlib import Path; from memleaf.locking import VaultLock; l=VaultLock(Path(sys.argv[1]));l.__enter__();os._exit(23)'
        first=subprocess.run([sys.executable,'-c',program,str(lock)],capture_output=True,timeout=15)
        self.assertEqual(first.returncode,23)
        program='import sys; from pathlib import Path; from memleaf.locking import VaultLock; l=VaultLock(Path(sys.argv[1]));l.__enter__();l.__exit__(None,None,None)'
        second=subprocess.run([sys.executable,'-c',program,str(lock)],capture_output=True,timeout=15)
        self.assertEqual(second.returncode,0,second.stderr.decode(errors='replace'))

    def test_case_variant_ids_remain_conflicts_not_winners(self):
        service=Memleaf.initialize(self.root/'库')
        service.create_memory(memory_id='portable-task',title='原始',body='内容')
        path=service.vault.memory_path('portable-task')
        copy=path.with_name('another-file.md')
        copy.write_bytes(path.read_bytes().replace(b'portable-task',b'PORTABLE-TASK'))
        with self.assertRaises(ValueError) as conflict:
            service.read('portable-task')
        self.assertEqual(conflict.exception.code, 'memory_id_conflict')

    def test_crlf_legacy_file_remains_readable_without_rewriting(self):
        service=Memleaf.initialize(self.root/'库')
        service.create_memory(memory_id='portable-task',title='原始',body='第一行\n第二行')
        path=service.vault.memory_path('portable-task')
        raw=path.read_bytes().replace(b'\n',b'\r\n');path.write_bytes(raw)
        found=service.search_candidates('原始')
        self.assertTrue(found['results'])
        self.assertEqual(path.read_bytes(),raw)

    def test_mcp_module_handles_non_utf8_parent_pipe_settings(self):
        service=Memleaf.initialize(self.root/'库')
        service.create_memory(memory_id='portable-task',title='便携任务',body='中文🔒',type='todo',status='active')
        from memleaf.host_runtime import HostRuntime
        token=HostRuntime(service,'hermes').open_retrieval_turn('portable','read')
        request={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'list_todos','arguments':{'as_of':'2026-09-17','retrieval_id':token}}}
        env={**os.environ,'PYTHONUTF8':'0','PYTHONIOENCODING':'cp1252'}
        child=subprocess.run([sys.executable,'-X','utf8=0','-m','memleaf.mcp_server','--vault',str(service.vault.root)],
                             input=(json.dumps(request,ensure_ascii=False)+'\n').encode(),capture_output=True,env=env,timeout=30)
        self.assertEqual(child.returncode,0)
        value=json.loads(child.stdout.decode('utf-8'))['result']['structuredContent']
        self.assertEqual(value['results'][0]['title'],'便携任务')

    def test_missing_iana_data_fails_without_changing_utc_fallback(self):
        from unittest.mock import patch
        from zoneinfo import ZoneInfoNotFoundError
        service=Memleaf.initialize(self.root/'库')
        with patch('memleaf.query_clock.ZoneInfo',side_effect=ZoneInfoNotFoundError):
            with self.assertRaisesRegex(ValueError,'invalid_query_timezone'):service.list_todos(timezone='Asia/Shanghai')
            self.assertEqual(service.list_todos(as_of='2026-09-17')['query_clock']['timezone'],'UTC')

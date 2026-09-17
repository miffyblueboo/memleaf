"""Real local subprocess/UTF-8 checks; not a native Hermes installation test."""
from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from memleaf import Memleaf
from memleaf.locking import VaultLock, atomic_write_bytes


class NativePlatformTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='memleaf native ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / '记忆 空间 e\u0301'
        self.root.mkdir()

    def spawn(self, script, *args):
        child = subprocess.Popen([sys.executable, '-c', script, *map(str, args)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self.stop, child)
        return child

    @staticmethod
    def stop(child):
        if child.poll() is None:
            child.terminate()
        try:
            child.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill(); child.communicate(timeout=10)

    def wait_marker(self, marker, child):
        end = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < end:
            if child.poll() is not None:
                out, err = child.communicate()
                self.fail(f'child exited {child.returncode}: {err.decode("utf-8", "replace")}')
            time.sleep(.02)
        self.assertTrue(marker.exists(), 'child did not reach native lock checkpoint')

    def test_native_lock_excludes_a_distinct_process(self):
        lock, ready, acquired = [self.root / n for n in ('vault.lock', 'ready', 'acquired')]
        script = '''import sys
from pathlib import Path
from memleaf.locking import VaultLock
Path(sys.argv[2]).write_text('ready',encoding='utf-8')
with VaultLock(Path(sys.argv[1])):
 Path(sys.argv[3]).write_text('acquired',encoding='utf-8')
'''
        with VaultLock(lock):
            child = self.spawn(script, lock, ready, acquired)
            self.wait_marker(ready, child)
            time.sleep(.15)
            self.assertFalse(acquired.exists())
            self.assertIsNone(child.poll())
        out, err = child.communicate(timeout=10)
        self.assertEqual(child.returncode, 0, err.decode('utf-8','replace'))
        self.assertTrue(acquired.exists())

    def test_terminated_lock_holder_does_not_leave_a_permanent_lock(self):
        lock, ready = self.root/'vault.lock', self.root/'ready'
        child = self.spawn('''import sys,time
from pathlib import Path
from memleaf.locking import VaultLock
with VaultLock(Path(sys.argv[1])):
 Path(sys.argv[2]).write_text('ready',encoding='utf-8')
 time.sleep(60)
''', lock, ready)
        self.wait_marker(ready, child)
        child.terminate(); child.communicate(timeout=10)
        # A separate process must acquire it; do not risk blocking the test runner.
        check = self.spawn('''import sys
from pathlib import Path
from memleaf.locking import VaultLock
with VaultLock(Path(sys.argv[1])): print('acquired')
''',lock)
        out,err=check.communicate(timeout=10)
        self.assertEqual(check.returncode,0,err.decode('utf-8','replace'))
        self.assertEqual(out.strip(),b'acquired')

    def test_atomic_replace_failure_keeps_old_utf8_bytes(self):
        path=self.root/'旧记忆.md';before='任务尚未完成。\r\n'.encode('utf-8')
        atomic_write_bytes(path,before)
        with patch('memleaf.locking.os.replace',side_effect=OSError(errno.ENOSPC,'synthetic disk full')):
            with self.assertRaises(OSError):atomic_write_bytes(path,'任务已完成。'.encode('utf-8'))
        self.assertEqual(path.read_bytes(),before)
        self.assertFalse(list(self.root.glob('.*.tmp')))

    def test_cross_process_unicode_path_and_field_roundtrip(self):
        service=Memleaf.initialize(self.root/'用户 Vault')
        service.create_memory(memory_id='native-task',title='独立事项',body='保留条件，不改变责任。',
                              type='todo',status='active',scopes=['project:项目甲'],assignee='张三',
                              waiting_on='审批',due_date='2026-09-30')
        script='''import json,sys
from memleaf import Memleaf
m=Memleaf(sys.argv[1]).read('native-task')
print(json.dumps({'body':m.body,'scopes':m.scopes,'assignee':m.extra.get('assignee'),'due_date':m.due_date},ensure_ascii=True))
'''
        child=self.spawn(script,service.vault.root)
        out,err=child.communicate(timeout=10)
        self.assertEqual(child.returncode,0,err.decode('utf-8','replace'))
        self.assertEqual(json.loads(out),{'body':'保留条件，不改变责任。','scopes':['project:项目甲'],
                                         'assignee':'张三','due_date':'2026-09-30'})

    def test_real_mcp_stdio_captures_unicode_without_a_model(self):
        vault=self.root/'MCP 测试'; service=Memleaf.initialize(vault)
        messages=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{
            'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'test','version':'1'}}}]
        for index,(role,body) in enumerate((('user','项目甲明天反馈。'),('assistant','已记录本次要求。')),2):
            messages.append({'jsonrpc':'2.0','id':index,'method':'tools/call','params':{'name':'capture','arguments':{
                'source':'hermes','session_id':'native-session','turn_id':'t','role':role,'content':body,
                'message_id':f'm-{index}','source_sequence':index,'final':role=='assistant',
                'source_time':'2026-09-17T10:00:00+08:00'}}})
        raw=('\n'.join(json.dumps(x,ensure_ascii=False) for x in messages)+'\n').encode('utf-8')
        cp=subprocess.run([sys.executable,'-m','memleaf.mcp_server','--vault',str(vault)],
                          input=raw,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=15)
        self.assertEqual(cp.returncode,0,cp.stderr.decode('utf-8','replace'))
        replies=[json.loads(line) for line in cp.stdout.decode('utf-8').splitlines()]
        self.assertEqual([r['id'] for r in replies],[1,2,3])
        self.assertTrue(all('error' not in r and not r.get('result',{}).get('isError',False) for r in replies),replies)
        captured=b'\n'.join(p.read_bytes() for p in service.vault.inbox_path.rglob('*.md'))
        self.assertIn('项目甲明天反馈。'.encode('utf-8'),captured)
        self.assertEqual(service.vault.list_markdown('knowledge'),[])


if __name__=='__main__':unittest.main()

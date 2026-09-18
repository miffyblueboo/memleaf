"""Native-process smoke check of installed entry points and packaged transport.

This tests the bundled Hermes transport, not a running Hermes installation.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import sys
import types
from unittest.mock import patch


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scripts',type=Path,required=True)
    args=parser.parse_args(argv)
    from memleaf import Memleaf
    # Hermes is not installed in this check. Only its two public base symbols
    # are stubbed; the packaged transport and the installed MCP process are real.
    base = types.ModuleType('agent.memory_provider')
    base.MemoryProvider = type('MemoryProvider', (), {})
    base.RecallStatus = type('RecallStatus', (), {})
    with patch.dict(sys.modules, {'agent': types.ModuleType('agent'), 'agent.memory_provider': base}):
        from memleaf.hermes_provider._mcp_client import _MCPClient
    suffix='.exe' if os.name=='nt' else ''
    command=args.scripts/('memleaf-mcp'+suffix)
    with tempfile.TemporaryDirectory(prefix='ml-smoke-') as tmp:
        root=Path(tmp)/'中文 空格 vault'
        service=Memleaf.initialize(root)
        service.create_memory(memory_id='portable-task',title='便携验证事项',body='保留中文和 emoji：🔒。',
                              type='todo',status='active',scopes=['global'])
        from memleaf.host_runtime import HostRuntime
        token=HostRuntime(service,'hermes').open_retrieval_turn('portable','raw-read')
        raw=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2024-11-05','capabilities':{},'clientInfo':{'name':'portable-test','version':'1'}}},
             {'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'list_todos','arguments':{'as_of':'2026-09-17','retrieval_id':token}}}]
        # Override the test runner's UTF-8 setting. The protocol must configure
        # its own encoding, not accidentally work only on UTF-8 Linux shells.
        env={**os.environ,'PYTHONUTF8':'0','PYTHONIOENCODING':'cp1252'}
        wire=('\n'.join(json.dumps(v,ensure_ascii=False) for v in raw)+'\n').encode('utf-8')
        for _ in range(2):
            child=subprocess.run([str(command),'--vault',str(root)],input=wire,capture_output=True,env=env,timeout=30)
            if child.returncode:raise RuntimeError('mcp_process_failed')
            messages=[json.loads(line) for line in child.stdout.decode('utf-8').splitlines() if line.strip()]
            result=next(m['result'] for m in messages if m.get('id')==2)
            if result.get('isError'):raise RuntimeError('mcp_tool_failed')
            value=result['structuredContent']
            assert value['results'][0]['memory_id']=='portable-task'
            assert value['results'][0]['title']=='便携验证事项'
        client=_MCPClient(str(command),str(root),timeout=10)
        try:
            catalog=client.call_tool('scope_catalog',{'source':'hermes','session_id':'portable','turn_id':'read-1'})
            token=catalog['retrieval_id']
            found=client.call_tool('search',{'query':'便携验证事项','scope':'global','retrieval_id':token})
            assert any(m['memory_id']=='portable-task' for m in found['results'])
            page=client.call_tool('read',{'memory_id':'portable-task','retrieval_id':token})
            assert page['body']=='保留中文和 emoji：🔒。'
        finally:
            client.close()
        assert service.read('portable-task').status=='active'
    print(json.dumps({'installed_mcp_process':'passed','packaged_hermes_transport':'passed',
                      'installed_hermes_host':'not_run','live_model':'not_run','switch_authorized':False}))
    return 0


if __name__=='__main__':raise SystemExit(main())

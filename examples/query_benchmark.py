"""Isolated local query benchmark; no production Vault or model is accepted.

Run against an installed wheel, or set PYTHONPATH=src for a source checkout.
The first-page measurements are not timings of a full multi-page enumeration.
"""
from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import math
from pathlib import Path
import platform
import statistics
import tempfile
import time
from unittest.mock import patch

import memleaf
from memleaf import Memleaf, Memory
from memleaf import query_scan

FIXTURE_VERSION = 1
FIXTURE_TIME = '2026-09-17T10:00:00+08:00'


def summary(samples: list[float]) -> dict:
    ordered = sorted(samples)
    return {'samples_seconds': samples, 'p50_seconds': statistics.median(samples),
            'p95_seconds': ordered[math.ceil(.95 * len(samples))-1],
            'percentile_method': 'nearest_rank', 'sample_count': len(samples)}


def fixture(service: Memleaf, count: int) -> tuple[str, int]:
    """Populate a new synthetic Vault, outside timed operations.

    Direct fixture writes intentionally avoid measuring index rebuilds on each
    insertion. Public query methods still use the real scanner and service.
    """
    digest = hashlib.sha256(); size = 0
    for i in range(count):
        memory = Memory(memory_id=f'mem-{i:06d}', title=f'验收独立任务 {i:06d}',
            body='需要交付一份独立材料，保留责任与明确期限。' * 8,
            type='todo', status='active', scopes=['project:Atlas'],
            created=FIXTURE_TIME, updated=FIXTURE_TIME)
        raw = memory.to_markdown().encode('utf-8')
        digest.update(len(raw).to_bytes(8, 'big')); digest.update(raw); size += len(raw)
        service.vault.memory_path(memory.memory_id).write_bytes(raw)
    return digest.hexdigest(), size


def benchmark(sizes=(100, 1000), repeat=5) -> dict:
    if (not isinstance(sizes, (list, tuple)) or not 1 <= len(sizes) <= 3
            or any(type(n) is not int or not 1 <= n <= 10000 for n in sizes)
            or len(set(sizes)) != len(sizes)
            or type(repeat) is not int or not 1 <= repeat <= 20):
        raise ValueError('invalid_benchmark_bounds')
    implementation = {}
    for module in (query_scan, inspect.getmodule(Memleaf)):
        path = Path(module.__file__)
        implementation[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    report = {'version': 1, 'fixture_version': FIXTURE_VERSION,
        'environment': {'python': platform.python_version(), 'system': platform.system(),
                        'machine': platform.machine(), 'package_version': memleaf.__version__},
        'implementation_sha256': implementation, 'model_calls': 0,
        'cache_regime': 'OS cache unmanaged; warmup excluded; no persistent application cache',
        'limits': {'max_memories': 10000, 'max_repeats': 20}, 'results': [],
        'limitations': ['synthetic fixture, not user workload', 'small-sample empirical percentiles',
                       'no provider latency, tokens, or native-host acceptance',
                       'public operations measure first page only']}
    for count in sizes:
        with tempfile.TemporaryDirectory(prefix='memleaf-query-benchmark-') as tmp:
            service = Memleaf.initialize(Path(tmp)/'测试 vault')
            fixture_hash, byte_count = fixture(service, count)
            times = {name: [] for name in ('scan', 'recheck', 'scan_and_recheck',
                'todo_first_page', 'search_first_page', 'read_first_page')}
            observations = {}
            # One warmup follows the same operations but does not enter samples.
            for iteration in range(repeat+1):
                start = time.perf_counter(); snapshot = query_scan.scan_memories(service.vault)
                split = time.perf_counter(); query_scan.ensure_scan_current(service.vault, snapshot)
                end = time.perf_counter()
                if len(snapshot.records) != count or snapshot.report()['status'] != 'complete':
                    raise RuntimeError('invalid_benchmark_scan')
                if iteration:
                    times['scan'].append(split-start); times['recheck'].append(end-split)
                    times['scan_and_recheck'].append(end-start)
                for name, operation in (
                    ('todo_first_page', lambda: service.list_todos(scope='project:Atlas', as_of='2026-09-17', timezone='UTC')),
                    ('search_first_page', lambda: service.search_candidates('验收独立任务', scope='project:Atlas')),
                    ('read_first_page', lambda: service.read_page('mem-000000')),
                ):
                    started=time.perf_counter(); value=operation(); elapsed=time.perf_counter()-started
                    if value is None or value['scan_status']['status'] != 'complete':
                        raise RuntimeError('invalid_benchmark_public_result')
                    observations[name] = {
                        'scan_status': value['scan_status']['status'],
                        'result_count': len(value.get('results', [])) if name != 'read_first_page' else 1,
                        'has_more': value.get('has_more'),
                        'generation': value['knowledge_generation'],
                    }
                    if name != 'read_first_page' and not value['results']:
                        raise RuntimeError('benchmark_query_returned_no_candidates')
                    if iteration: times[name].append(elapsed)
            # Diagnostic instrumentation is separate from the timings above.
            with patch.object(query_scan, 'parse_frontmatter', wraps=query_scan.parse_frontmatter) as parser:
                snapshot=query_scan.scan_memories(service.vault); first=parser.call_count
                query_scan.ensure_scan_current(service.vault,snapshot); second=parser.call_count-first
            report['results'].append({'memory_count': count, 'fixture_sha256': fixture_hash,
                'fixture_bytes': byte_count, 'timings': {k: summary(v) for k,v in times.items()},
                'parse_calls': {'scan': first, 'unchanged_recheck': second},
                'observations': observations})
    return report


def main(argv=None) -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sizes',nargs='+',type=int,default=[100,1000])
    parser.add_argument('--repeat',type=int,default=5)
    args=parser.parse_args(argv)
    try:
        result=benchmark(args.sizes,args.repeat)
    except (ValueError,OSError,RuntimeError) as error:
        # No Vault paths, config contents or arbitrary exception prose in output.
        print(json.dumps({'status':'failed','code':type(error).__name__}))
        return 1
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0


if __name__=='__main__':
    raise SystemExit(main())

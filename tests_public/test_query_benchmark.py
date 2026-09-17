from __future__ import annotations
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch


def load_benchmark():
    path=Path(__file__).resolve().parents[1]/'examples/query_benchmark.py'
    spec=importlib.util.spec_from_file_location('memleaf_query_benchmark_example',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


class QueryBenchmarkTests(unittest.TestCase):
    def test_small_benchmark_reports_samples_and_correct_result(self):
        module=load_benchmark()
        with patch('urllib.request.urlopen',side_effect=AssertionError('no network')):
            value=module.benchmark([3],2)
        self.assertEqual(value['model_calls'],0)
        row=value['results'][0]
        self.assertEqual(row['parse_calls'],{'scan':3,'unchanged_recheck':0})
        self.assertEqual(row['observations']['todo_first_page']['result_count'],3)
        for timing in row['timings'].values():
            self.assertEqual(timing['sample_count'],2)
            self.assertEqual(len(timing['samples_seconds']),2)
            self.assertGreaterEqual(timing['p95_seconds'],timing['p50_seconds'])

    def test_same_fixture_is_reproducible_across_temporary_roots(self):
        module=load_benchmark();a=module.benchmark([2],1);b=module.benchmark([2],1)
        self.assertEqual(a['results'][0]['fixture_sha256'],b['results'][0]['fixture_sha256'])
        self.assertEqual(a['results'][0]['fixture_bytes'],b['results'][0]['fixture_bytes'])
        self.assertEqual(a['results'][0]['observations'],b['results'][0]['observations'])

    def test_bounds_reject_before_creating_vault(self):
        module=load_benchmark()
        with patch.object(module.Memleaf,'initialize',side_effect=AssertionError('no setup')):
            for sizes,repeats in (([],1),([0],1),([10001],1),([1,1],1),([1,2,3,4],1),
                                  ([True],1),([1],0),([1],21),([1],True)):
                with self.subTest(sizes=sizes,repeats=repeats),self.assertRaises(ValueError):
                    module.benchmark(sizes,repeats)

    def test_cli_has_no_existing_vault_or_model_input(self):
        module=load_benchmark()
        with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit) as error:
            module.main(['--vault','/existing'])
        self.assertEqual(error.exception.code,2)

    def test_cli_prints_bounded_result(self):
        module=load_benchmark();stream=io.StringIO()
        with contextlib.redirect_stdout(stream):code=module.main(['--sizes','2','--repeat','1'])
        self.assertEqual(code,0);self.assertEqual(json.loads(stream.getvalue())['results'][0]['memory_count'],2)
        self.assertNotIn('memleaf-query-benchmark-',stream.getvalue())

    def test_failed_measurement_is_not_a_successful_or_partial_benchmark(self):
        module=load_benchmark();stream=io.StringIO()
        with patch.object(module,'fixture',side_effect=OSError('private data')),contextlib.redirect_stdout(stream):
            code=module.main(['--sizes','2'])
        self.assertEqual(code,1);self.assertNotIn('private data',stream.getvalue())

    def test_percentiles_are_empirical_not_confidence_bounds(self):
        module=load_benchmark();value=module.summary([5.,1.,4.,2.,3.])
        self.assertEqual(value['p50_seconds'],3.)
        self.assertEqual(value['p95_seconds'],5.)
        self.assertEqual(value['percentile_method'],'nearest_rank')

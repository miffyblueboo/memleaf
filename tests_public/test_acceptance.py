"""Harness and end-to-end execution checks; not real-model semantic acceptance."""
from __future__ import annotations

from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from acceptance_oracles import ORACLES
from memleaf.acceptance import (AcceptanceError, execute_suite, main, plan_suite,
                                read_suite, validate_suite, _Meter)
from memleaf.llm import ModelError


ROOT = Path(__file__).resolve().parents[1]
SUITE = ROOT / 'examples' / 'incremental_acceptance.json'


class Oracle:
    single_pass_safe = True
    provider = 'test'
    model = 'fixed-oracle'

    def __init__(self, scripts):
        self.scripts = iter(deepcopy(scripts))
        self.calls = []

    def complete(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        value = json.loads(prompt)
        script = next(self.scripts)
        if isinstance(script, BaseException):
            raise script
        if isinstance(script, str):
            return script
        users = [e['ref'] for e in value['evidence'] if e['use'] == 'new' and e['role'] == 'user']
        assistants = [e['ref'] for e in value['evidence'] if e['use'] == 'new' and e['role'] == 'assistant']
        def replace(obj):
            if isinstance(obj, dict): return {k: replace(v) for k, v in obj.items()}
            if isinstance(obj, list): return [replace(v) for v in obj]
            return users[0] if obj == '$user' else obj
        items = replace(script)
        for item in items:
            if 'target_title' in item:
                title = item.pop('target_title')
                candidates = [m for m in value['memories'] if m['title'] == title]
                if len(candidates) != 1: raise AssertionError('fixture target missing or ambiguous')
                item['target'] = candidates[0]['ref']
        items += [{'action':'NO_MEMORY','evidence':[ref]} for ref in assistants]
        return json.dumps({'items':items}, ensure_ascii=False)


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.suite = read_suite(SUITE)
        self.small = deepcopy(self.suite)
        self.small['cases'] = [self.small['cases'][1]]

    def run_fixture(self, suite=None, scripts=None, **kwargs):
        backend = Oracle(scripts if scripts is not None else [[{'action':'NO_MEMORY','evidence':['$user']}]])
        value = execute_suite(suite or self.small, output=self.root/'output', backend=backend,
                              max_requests=kwargs.pop('max_requests', 4), repeats=kwargs.pop('repeats', 1), fixture=True, **kwargs)
        return value, backend

    def test_plan_does_not_initialize_or_call_model(self):
        with patch('memleaf.acceptance.Memleaf.initialize', side_effect=AssertionError):
            plan = plan_suite(self.suite)
        self.assertEqual((plan['cases'],plan['repeats'],plan['model_calls']), (12,5,0))
        self.assertEqual((plan['normal_request_upper_bound'],plan['recovery_request_upper_bound']), (90,180))
        self.assertFalse(plan['switch_authorized'])

    def test_fixture_is_not_live_or_semantic_success(self):
        result, _ = self.run_fixture()
        self.assertEqual(result['structural_status'], 'passed')
        self.assertEqual(result['semantic_status'], 'not_run')
        self.assertEqual(result['model_calls'], 0)
        self.assertEqual(result['backend_reservations'], 1)
        self.assertFalse(result['switch_authorized'])

    def test_real_backend_requires_explicit_authorization(self):
        with self.assertRaisesRegex(AcceptanceError,'authorization'):
            execute_suite(self.small, output=self.root/'out', backend=Oracle([]), max_requests=4, repeats=1)
        self.assertFalse((self.root/'out').exists())

    def test_successful_stub_with_live_label_still_needs_semantic_review(self):
        r=execute_suite(self.small, output=self.root/'out', backend=Oracle([[{'action':'NO_MEMORY','evidence':['$user']}]]),
                        max_requests=4, repeats=1, authorize_model=True)
        self.assertEqual(r['semantic_status'],'not_reviewed')
        self.assertIsNone(r['model_calls'])
        self.assertEqual(r['confirmed_responses'],1)

    def test_expectations_and_checklist_never_enter_prompt(self):
        self.small['cases'][0]['semantic_checks']=['CHECKLIST_PRIVATE_SENTINEL']
        r,b=self.run_fixture()
        prompt=json.loads(b.calls[0][0])
        self.assertNotIn('expected',prompt)
        self.assertNotIn('CHECKLIST_PRIVATE_SENTINEL',b.calls[0][0]+b.calls[0][1]['system'])
        self.assertEqual(r['structural_status'],'passed')

    def test_repeat_uses_independent_empty_vaults_and_no_cached_answers(self):
        r,b=self.run_fixture(repeats=3,scripts=[[{'action':'NO_MEMORY','evidence':['$user']}]]*3)
        self.assertEqual((len(b.calls),r['case_repetitions_run']), (3,3))
        paths=list((self.root/'output').glob('*/vault'))
        self.assertEqual(len(paths),3)
        self.assertEqual({json.loads(p)['memories'].__len__() for p,_ in b.calls},{0})

    def test_same_id_lifecycle_uses_real_committer(self):
        s=deepcopy(self.suite);s['cases']=[s['cases'][0]]
        r,_=self.run_fixture(suite=s,scripts=ORACLES[0],max_requests=6)
        self.assertEqual(r['structural_status'],'passed',r['results'])
        observations=json.loads((self.root/'output/task_lifecycle-01/observations.json').read_text(encoding="utf-8"))
        memories=[v['memories'][0] for v in observations['steps']]
        self.assertEqual(len({r['memory_id'] for r in memories}),1)
        self.assertEqual([r['status'] for r in memories],['active','completed','completed'])

    def test_all_public_trajectories_fixed_oracle(self):
        scripts=[row for case in ORACLES for row in case]
        r,_=self.run_fixture(self.suite,scripts=scripts,max_requests=36)
        self.assertEqual(r['structural_status'],'passed',r['results'])
        self.assertEqual(r['case_repetitions_run'],12)
        self.assertEqual(r['backend_reservations'],18)

    def test_error_case_does_not_drop_previous_successes(self):
        s=deepcopy(self.suite);s['cases']=s['cases'][:2]
        r,_=self.run_fixture(s,scripts=ORACLES[0]+['{','{'],max_requests=10)
        self.assertEqual(r['results'][0]['status'],'passed')
        self.assertEqual(r['results'][1]['status'],'failed')
        self.assertEqual(r['structural_status'],'failed')

    def test_global_cap_includes_runtime_retry(self):
        r,b=self.run_fixture(scripts=['{','{'],max_requests=1)
        self.assertEqual(len(b.calls),1)
        self.assertTrue(r['budget_exhausted'])
        self.assertEqual(r['backend_reservations'],1)
        self.assertEqual(json.loads((self.root/'output/requests.json').read_text(encoding="utf-8"))['reservations'],1)

    def test_exact_budget_completion_is_not_false_exhaustion(self):
        r,b=self.run_fixture(max_requests=1)
        self.assertEqual(r['structural_status'],'passed')
        self.assertFalse(r['budget_exhausted'])

    def test_global_cap_stops_later_cases(self):
        r,b=self.run_fixture(self.suite,scripts=[ORACLES[0][0]],max_requests=1)
        self.assertEqual(len(b.calls),1)
        self.assertEqual(r['execution_status'],'incomplete')
        self.assertLess(r['case_repetitions_run'],r['case_repetitions_expected'])

    def test_existing_output_never_resets_budget(self):
        self.run_fixture()
        raw=(self.root/'output/requests.json').read_bytes()
        with self.assertRaisesRegex(AcceptanceError,'new_output'):
            self.run_fixture()
        self.assertEqual((self.root/'output/requests.json').read_bytes(),raw)

    def test_durable_reservation_precedes_backend(self):
        folder=self.root/'o';folder.mkdir()
        class B:
            single_pass_safe=True
            def complete(_, *_a, **_kw):
                value=json.loads((folder/'requests.json').read_text(encoding="utf-8"))
                self.assertEqual(value['reservations'],1)
                self.assertEqual(value['attempts'][0]['outcome'],'reserved')
                return '{}'
        meter=_Meter(B(),folder,1);meter.complete('{}')
        self.assertEqual(meter.attempts[0]['outcome'],'returned')

    def test_reservation_failure_spends_no_backend_call(self):
        folder=self.root/'o';folder.mkdir();b=Oracle(['{}']);meter=_Meter(b,folder,1)
        with patch('memleaf.acceptance.atomic_write_json',side_effect=OSError):
            with self.assertRaises(OSError):meter.complete('{}')
        self.assertEqual(b.calls,[])

    def test_sensitive_exception_not_in_public_report(self):
        r,_=self.run_fixture(scripts=[RuntimeError('api_key=private-sentinel')])
        self.assertNotIn('private-sentinel',json.dumps(r))
        self.assertNotIn('private-sentinel',(self.root/'output/requests.json').read_text(encoding="utf-8"))

    def test_missing_usage_stays_unknown(self):
        self.run_fixture()
        row=json.loads((self.root/'output/requests.json').read_text(encoding="utf-8"))['attempts'][0]
        self.assertIsNone(row['usage'])
        self.assertNotIn('prompt',row)

    def test_private_output_modes(self):
        self.run_fixture()
        if os.name!='nt':
            self.assertEqual(stat.S_IMODE((self.root/'output').stat().st_mode),0o700)
            self.assertEqual(stat.S_IMODE((self.root/'output/report.json').stat().st_mode),0o600)

    def test_invalid_repeats_and_limits_have_no_side_effects(self):
        for bad in (0,True,-1,21):
            with self.subTest(value=bad):
                with self.assertRaises(AcceptanceError):plan_suite(self.small,repeats=bad)
        for bad in (0,True,-1,2049):
            with self.subTest(value=bad):
                with self.assertRaises(AcceptanceError):self.run_fixture(max_requests=bad)
        self.assertFalse((self.root/'output').exists())

    def test_invalid_inputs_rejected_before_model(self):
        variants=[]
        s=deepcopy(self.small);s['cases'][0]['id']='../../other';variants.append(s)
        s=deepcopy(self.small);s['cases'][0]['turns'][0]['messages'][-1].pop('final');variants.append(s)
        s=deepcopy(self.small);s['cases'][0]['turns'][0]['messages'][0]['source_time']='2026-09-17T10:00:00';variants.append(s)
        s=deepcopy(self.small);s['cases'][0]['turns'][0]['expected']['same_ids_as']=0;variants.append(s)
        s=deepcopy(self.small);s['cases'][0]['turns'][0]['messages'][0]['role']='tool';variants.append(s)
        for value in variants:
            with self.subTest(value=value):
                with self.assertRaises(AcceptanceError):validate_suite(value)

    def test_read_suite_refuses_duplicate_keys(self):
        path=self.root/'bad.json';path.write_text('{"version":1,"version":1}')
        with self.assertRaises(AcceptanceError):read_suite(path)

    def test_expected_blocking_is_separate_from_semantic_success(self):
        self.small['cases'][0]['turns'][0]['expected']['execution_status']='completed_with_unresolved'
        self.small['cases'][0]['turns'][0]['expected']['coverage_status']='partial'
        r,_=self.run_fixture(scripts=[[{'action':'DEFERRED','evidence':['$user'],'reason':'missing_identity','need':'Identify the task'}]])
        self.assertEqual(r['structural_status'],'passed')
        self.assertEqual(r['semantic_status'],'not_run')

    def test_cli_plan_does_not_read_backend_config(self):
        out=io.StringIO()
        with redirect_stdout(out),patch('memleaf.acceptance.load_config',side_effect=AssertionError):
            code=main(['--suite',str(SUITE)])
        self.assertEqual(code,0)
        self.assertEqual(json.loads(out.getvalue())['model_calls'],0)

    def test_cli_no_implicit_model_execution(self):
        out=io.StringIO()
        with redirect_stdout(out),patch('memleaf.acceptance.load_config',side_effect=AssertionError):
            code=main(['--suite',str(SUITE),'--execute','--output',str(self.root/'out')])
        self.assertEqual(code,1)
        self.assertFalse((self.root/'out').exists())

    def test_cli_plan_rejects_ignored_live_options(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--suite',str(SUITE),'--backend-config','private.yaml']),1)

    def test_holdout_label_is_retained_not_manufactured(self):
        self.small['cases'][0]['split']='holdout'
        r,_=self.run_fixture()
        self.assertEqual(r['results'][0]['split'],'holdout')
        self.assertEqual(r['semantic_status'],'not_run')

"""Independent audit assertions; no legacy semantic-fixture adapters.
Run with baseline's src in PYTHONPATH. Assertions now require the isolated follow-up fixes.
All credentials/text are synthetic; all Vaults use TemporaryDirectory.
"""
import copy, json, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from memleaf import Memleaf
from memleaf.admission import (EvidenceUnit, parse_coverage,
    resolve_omitted_candidate_event_ids, split_gate_envelope,
    split_semantic_envelope, validate_bindings, validate_coverage_bindings)
from memleaf.model_execution import ModelExecutor
from memleaf import model_execution as execution
from memleaf.config import save_config
from memleaf.process_jobs import _safe_model_metrics, _aggregate_model_metrics
from memleaf.validation import ModelOutputError, parse_gate_output, parse_strict_json

EXAMPLE = {'candidates':[{'candidate_id':'c1','memory':'Alpha applies.','duplicate':False,
 'worth':True,'type':'fact','scopes':['global'],'scope_source':'model'}],
 'coverage':[{'unit_id':'u1','decision':'CANDIDATE','candidate_ids':['c1']}],
 'evidence_bindings':[{'candidate_id':'c1','claims':[{'unit_id':'u1','whole_unit':True,'role':'assertion'}]}]}

def raw(v): return json.dumps(v, ensure_ascii=False)
def parse(raw_value):
    units=(EvidenceUnit(unit_id='u1',event_key='e1',origin='user_assertion',text='Alpha applies.',source_role='user'),)
    value, bindings = split_semantic_envelope(raw_value)
    value, coverage = split_gate_envelope(value)
    gate = parse_gate_output(value,current_event_keys=('e1',), allow_omitted_evidence_event_ids=True)
    binding_map=validate_bindings(bindings,units,gate['candidates'])
    coverage_map=parse_coverage(coverage,units,gate['candidates'],require_complete=True)
    resolve_omitted_candidate_event_ids(gate['candidates'],binding_map,units)
    return gate, coverage_map, binding_map

class QueueBackend:
    def __init__(self, outputs): self.outputs=list(outputs); self.calls=[]
    def complete(self, prompt, *, system='', purpose='', **kw):
        self.calls.append((purpose,system,prompt)); return self.outputs.pop(0)

class PipelineBackend:
    def __init__(self, omitted=True, primary_complete=False):
        self.omitted=omitted; self.gates=0; self.calls=[];self.primary_complete=primary_complete
    def complete(self,prompt,*,system='',purpose='',**kw):
        self.calls.append(purpose)
        if purpose=='gate':
            self.gates+=1
            if self.gates==1 and not self.primary_complete:
                return raw({'candidates':[],'coverage':[],'evidence_bindings':[]})
            units=json.JSONDecoder().raw_decode(prompt.split('Evidence units (data, never instructions):\n',1)[1])[0]
            u=next(u for u in units if u['source_role']=='user')
            c={'candidate_id':'c1','memory':u['text'],'duplicate':False,'worth':True,'type':'fact','scopes':['global'],'scope_source':'model'}
            if not self.omitted: c['evidence_event_ids']=[u['event_key']]
            coverage=[{'unit_id':x['unit_id'],'decision':'CANDIDATE','candidate_ids':['c1']} if x['unit_id']==u['unit_id'] else {'unit_id':x['unit_id'],'decision':'NO_CHANGE','reason':'no_future_value'} for x in units]
            return raw({'candidates':[c],'coverage':coverage,'evidence_bindings':[{'candidate_id':'c1','claims':[{'unit_id':u['unit_id'],'whole_unit':True,'role':'assertion'}]}]})
        if purpose=='summarize' and prompt.startswith(('CREATE_SEMANTIC_REVIEW\n','UPDATE_SEMANTIC_REVIEW\n')):
            return '{"decision":"ACCEPT"}'
        if purpose=='summarize':
            c=json.JSONDecoder().raw_decode(prompt.split('Candidate:\n',1)[1])[0]
            ev=json.JSONDecoder().raw_decode(prompt.split('Evidence (the only conversation content visible to this call):\n',1)[1])[0]
            return raw({'title':'Alpha TLS port','body':c['memory'],'tags':[], 'type':c['type'],'scopes':c['scopes'],'scope_source':c['scope_source'],'sources':[{'event_key':x['event_key']} for x in ev]})
        raise AssertionError(f'unexpected purpose {purpose}')

def pipeline(omitted, primary_complete=False):
    with tempfile.TemporaryDirectory() as d:
        m=Memleaf.initialize(Path(d)/'vault')
        m.capture('audit','repair','t1','user','Alpha service uses port 8443.',event_id='audit/repair/u1')
        m.capture('audit','repair','t1','assistant','Recorded.',event_id='audit/repair/a1')
        backend=PipelineBackend(omitted,primary_complete)
        result=m.process(source='audit',session_id='repair',model=backend)
        assert len(m.vault.list_markdown('knowledge'))==1, result
        return result, backend.calls

class ProtocolTests(unittest.TestCase):
    def test_independent_canonical_example(self): parse(raw(EXAMPLE))
    def test_nonobject_and_extra_field_rejected(self):
        for row in ['bad',None,[],{**EXAMPLE['coverage'][0],'extra':'x'}]:
            with self.subTest(row=row),self.assertRaises(ModelOutputError):
                v=copy.deepcopy(EXAMPLE);v['coverage']=[row];parse(raw(v))
    def test_unknown_unit_dangling_candidate_mapping_and_binding_rejected(self):
        variants=[]
        v=copy.deepcopy(EXAMPLE);v['coverage'][0]['unit_id']='u-missing';variants.append(v)
        v=copy.deepcopy(EXAMPLE);v['coverage'][0]['candidate_ids']=['c-missing'];variants.append(v)
        v=copy.deepcopy(EXAMPLE);v['candidates'][0]['evidence_event_ids']=['e-missing'];variants.append(v)
        v=copy.deepcopy(EXAMPLE);v['evidence_bindings'][0]['claims']=[{'unit_id':'u1','quote':'Beta applies.','role':'assertion'}];variants.append(v)
        for i,v in enumerate(variants):
            with self.subTest(case=i), self.assertRaises(ModelOutputError): parse(raw(v))
    def test_duplicate_keys_and_boolean_candidate_type_rejected(self):
        with self.assertRaises(ModelOutputError): parse_strict_json('{"candidates":[],"candidates":[]}')
        v=copy.deepcopy(EXAMPLE);v['candidates'][0]['worth']=1
        with self.assertRaises(ModelOutputError):parse(raw(v))
    def test_failed_calls_not_output_validation_failures(self):
        backend=QueueBackend(['{"coverage": ["bad"]}']*3)
        executor=ModelExecutor(SimpleNamespace(vault=SimpleNamespace(config=lambda:{'llm':{'diagnostic_logging':False}})))
        with self.assertRaises(ModelOutputError):executor._complete_json_stage(backend,'input',system='system',purpose='gate',parser=parse)
        self.assertEqual(executor.metrics()['total']['failed_calls'],0)
        self.assertEqual(executor.metrics()['total']['call_count'],3)
    def test_primary_gate_omitted_event_ids_full_pipeline_control(self): pipeline(True,True)
    def test_coverage_repair_explicit_event_ids_full_pipeline_control(self): pipeline(False)
    def test_coverage_repair_omitted_event_ids_full_pipeline(self): pipeline(True)

@unittest.skipUnless(hasattr(execution,'_validate_coverage_shape_repair'),'candidate-only feature')
class CandidateTests(unittest.TestCase):
    def guard(self,a,b):execution._validate_coverage_shape_repair(a,b)
    def invalid(self):
        v=copy.deepcopy(EXAMPLE);v['coverage'][0]['extra']='data';return v
    def executor(self):return ModelExecutor(SimpleNamespace(vault=SimpleNamespace(config=lambda:{'llm':{'diagnostic_logging':False}})))
    def test_allowed_field_deletion_control(self):self.guard(raw(self.invalid()),raw(EXAMPLE))
    def test_semantic_mutations_rejected(self):
        modifications=[lambda x:x['candidates'][0].update(memory='Beta'),lambda x:x['candidates'][0].update(scopes=['unscoped']),lambda x:x['candidates'][0].update(update_memory_id='target'),lambda x:x['evidence_bindings'][0]['claims'][0].update(unit_id='u2'),lambda x:x['coverage'][0].update(candidate_ids=['c2']),lambda x:x.update(candidates=[])]
        for i,change in enumerate(modifications):
            x=copy.deepcopy(EXAMPLE);change(x)
            with self.subTest(case=i),self.assertRaises(ModelOutputError):self.guard(raw(self.invalid()),raw(x))
    def test_object_key_order_and_unicode_escape_are_equivalent(self):
        a=self.invalid();b=copy.deepcopy(EXAMPLE)
        a['candidates'][0]['memory']=b['candidates'][0]['memory']='中文 café'
        b={k:b[k] for k in reversed(list(b))}
        self.guard(raw(a),json.dumps(b,ensure_ascii=True))
    def test_list_order_changes_rejected(self):
        a=self.invalid();b=copy.deepcopy(EXAMPLE)
        a['candidates'][0]['scopes']=b['candidates'][0]['scopes']=['global','domain:alpha']
        b['candidates'][0]['scopes']=list(reversed(b['candidates'][0]['scopes']))
        with self.assertRaises(ModelOutputError):self.guard(raw(a),raw(b))
        # Distinct rows in each top-level ordered list must not be reordered.
        for field, extra in (
            ('candidates', {**EXAMPLE['candidates'][0], 'candidate_id':'c2','memory':'Beta applies.'}),
            ('coverage', {'unit_id':'u2','decision':'NO_CHANGE','reason':'no_future_value'}),
            ('evidence_bindings', {'candidate_id':'c2','claims':[{'unit_id':'u2','whole_unit':True,'role':'assertion'}]}),
        ):
            before=self.invalid();after=copy.deepcopy(EXAMPLE)
            before[field].append(copy.deepcopy(extra));after[field].append(copy.deepcopy(extra))
            after[field].reverse()
            with self.subTest(ordered_field=field),self.assertRaises(ModelOutputError):
                self.guard(raw(before),raw(after))
    def test_sensitive_key_not_written_to_real_diagnostic_log(self):
        sentinel='sk_SYNTHETIC_SECRET_AUDIT_012345'
        invalid=self.invalid();del invalid['coverage'][0]['extra'];invalid['coverage'][0][sentinel]='value'
        with tempfile.TemporaryDirectory() as d:
            m=Memleaf.initialize(Path(d)/'vault');cfg=m.vault.config();cfg['llm']['diagnostic_logging']=True;save_config(m.vault.config_path,cfg)
            executor=ModelExecutor(m);executor._complete_json_stage(QueueBackend([raw(invalid),raw(EXAMPLE)]),'input',system='sys',purpose='gate',parser=parse)
            logfile=m.vault.logs_path/'model-diagnostics.jsonl'
            self.assertTrue(logfile.exists())
            self.assertNotIn(sentinel,logfile.read_text())
    def test_semantic_retry_metrics_survive_job_projection(self):
        invalid=copy.deepcopy(EXAMPLE);invalid['coverage']=['bad']
        e=self.executor();e._complete_json_stage(QueueBackend([raw(invalid),raw(EXAMPLE)]),'input',system='sys',purpose='gate',parser=parse)
        m=e.metrics();self.assertEqual(m['operations']['gate_semantic_retry']['call_count'],1)
        p=_safe_model_metrics(m);self.assertEqual(p['total']['call_count'],2)
        self.assertIn('gate_semantic_retry',p['operations'])
        self.assertEqual(len(p['calls']),2)
        self.assertIn('gate_semantic_retry',_aggregate_model_metrics([m])['operations'])
    def test_lossless_guard_rejects_boolean_number_change(self):
        a=self.invalid();b=copy.deepcopy(EXAMPLE)
        # Keep F06 independent of the old permissive F02 coverage shape.
        # A canonical candidate boolean must not compare equal to a JSON number.
        b['candidates'][0]['duplicate']=0
        with self.assertRaises(ModelOutputError):self.guard(raw(a),raw(b))
    def test_downstream_blocks_boolean_number_candidate_mutation(self):
        a=self.invalid();b=copy.deepcopy(EXAMPLE);b['candidates'][0]['worth']=1
        with self.assertRaises(ModelOutputError): self.guard(raw(a),raw(b))
        with self.assertRaises(ModelOutputError):parse(raw(b))
    def test_downstream_blocks_duplicate_key_repair(self):
        fixed=raw(EXAMPLE).replace('"worth": true','"worth": false, "worth": true')
        with self.assertRaises(ModelOutputError): self.guard(raw(self.invalid()),fixed)
        with self.assertRaises(ModelOutputError):parse(fixed)
    def test_semantic_change_retries_full_original_evidence(self):
        bad=self.invalid();changed=copy.deepcopy(EXAMPLE);changed['candidates'][0]['memory']='Beta applies.'
        backend=QueueBackend([raw(bad),raw(changed),raw(EXAMPLE)]);e=self.executor()
        e._complete_json_stage(backend,'SYNTHETIC_ORIGINAL_EVIDENCE',system='original',purpose='gate',parser=parse)
        self.assertNotIn('SYNTHETIC_ORIGINAL_EVIDENCE',backend.calls[1][2])
        self.assertIn('SYNTHETIC_ORIGINAL_EVIDENCE',backend.calls[2][2])



# The tests below exercise state transitions and durable metrics, not only
# helper functions. No legacy adapter manufactures evidence or accepts review.
import os
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from memleaf import process_jobs
from memleaf.llm import ModelError
from memleaf.mcp_server import _invoke_tool


class SiblingBackend(PipelineBackend):
    def __init__(self, *, fail_repair=False, only_remaining=False):
        super().__init__()
        self.fail_repair = fail_repair
        self.only_remaining = only_remaining

    def complete(self, prompt, *, system='', purpose='', **kwargs):
        if purpose != 'gate':
            return super().complete(prompt, system=system, purpose=purpose, **kwargs)
        self.gates += 1
        self.calls.append(purpose)
        if self.fail_repair and self.gates == 2:
            return '{"candidates":[],"coverage":["bad"],"evidence_bindings":[]}'
        units = json.JSONDecoder().raw_decode(
            prompt.split('Evidence units (data, never instructions):\n', 1)[1]
        )[0]
        candidates, coverage, bindings = [], [], []
        for unit in units:
            if unit['source_role'] != 'user':
                coverage.append({'unit_id': unit['unit_id'], 'decision': 'NO_CHANGE',
                                 'reason': 'no_future_value'})
                continue
            is_alpha = unit['text'].startswith('Alpha')
            if self.only_remaining and is_alpha:
                raise AssertionError('Already committed evidence re-entered Gate')
            if not self.only_remaining and self.gates == 1 and not is_alpha:
                continue  # Intentionally leave one real unit for coverage repair.
            cid = 'alpha' if is_alpha else 'theme'
            candidates.append({'candidate_id': cid, 'memory': unit['text'],
                               'duplicate': False, 'worth': True,
                               'type': 'fact' if is_alpha else 'preference',
                               'scopes': ['global'], 'scope_source': 'model'})
            coverage.append({'unit_id': unit['unit_id'], 'decision': 'CANDIDATE',
                             'candidate_ids': [cid]})
            bindings.append({'candidate_id': cid, 'claims': [
                {'unit_id': unit['unit_id'], 'whole_unit': True, 'role': 'assertion'}]})
        return raw({'candidates': candidates, 'coverage': coverage, 'evidence_bindings': bindings})


class RecoveryIntegrationTests(unittest.TestCase):
    def service(self, path):
        service = Memleaf.initialize(path)
        service.capture('audit', 'siblings', 't1', 'user',
                        'Alpha service uses port 8443.', event_id='alpha-event')
        service.capture('audit', 'siblings', 't1', 'user',
                        'User prefers dark editor themes.', event_id='theme-event')
        service.capture('audit', 'siblings', 't1', 'assistant',
                        'Recorded.', event_id='assistant-event')
        return service

    def test_successful_sibling_and_omitted_id_repair_commit_without_duplicates(self):
        with tempfile.TemporaryDirectory() as d:
            service = self.service(Path(d)/'vault')
            backend = SiblingBackend()
            result = service.process(source='audit', session_id='siblings', model=backend)
            self.assertEqual(result['memories_written'], 2, result)
            self.assertEqual(backend.gates, 2)
            paths = service.vault.list_markdown('knowledge')
            self.assertEqual(len(paths), 2)
            before = {str(p): p.read_bytes() for p in paths}
            again = service.process(source='audit', session_id='siblings', model=backend)
            self.assertEqual(again['memories_written'], 0)
            self.assertEqual(before, {str(p): p.read_bytes() for p in paths})
            self.assertEqual(len(service.vault.list_markdown('history')), 0)
            self.assertEqual(result['model_metrics']['total']['invalid_output_count'], 0)

    def test_failed_repair_preserves_sibling_and_retries_only_unresolved_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            service = self.service(Path(d)/'vault')
            result = service.process(source='audit', session_id='siblings',
                                     model=SiblingBackend(fail_repair=True))
            self.assertEqual(result['memories_written'], 1, result)
            self.assertGreater(result['unresolved_evidence_count'], 0, result)
            paths = service.vault.list_markdown('knowledge')
            self.assertEqual(len(paths), 1)
            before = paths[0].read_bytes()
            metrics = result['model_metrics']
            self.assertEqual(metrics['operations']['gate_coverage_repair']['invalid_output_count'], 1)
            self.assertEqual(metrics['total']['failed_calls'], 0)
            repaired = service.process(source='audit', session_id='siblings',
                                       model=SiblingBackend(only_remaining=True))
            self.assertEqual(repaired['memories_written'], 1, repaired)
            self.assertEqual(len(service.vault.list_markdown('knowledge')), 2)
            self.assertEqual(paths[0].read_bytes(), before)
            self.assertEqual(len(service.vault.list_markdown('history')), 0)
            self.assertEqual(repaired['unresolved_evidence_count'], 0)

    def test_failed_primary_commits_nothing_and_can_retry(self):
        with tempfile.TemporaryDirectory() as d:
            service = Memleaf.initialize(Path(d)/'vault')
            service.capture('audit', 'repair', 't1', 'user',
                            'Alpha service uses port 8443.', event_id='event-user')
            service.capture('audit', 'repair', 't1', 'assistant', 'Recorded.', event_id='event-assistant')
            with self.assertRaises(ModelOutputError) as error:
                service.process(source='audit', session_id='repair',
                                model=QueueBackend(['{"coverage":["bad"]}']*3))
            self.assertEqual(len(service.vault.list_markdown('knowledge')), 0)
            self.assertEqual(error.exception.model_metrics['total']['invalid_output_count'], 3)
            result = service.process(source='audit', session_id='repair',
                                     model=PipelineBackend(primary_complete=True))
            self.assertEqual(result['memories_written'], 1, result)


class MetricsRoundtripTests(unittest.TestCase):
    def executor(self):
        return ModelExecutor(SimpleNamespace(vault=SimpleNamespace(
            config=lambda: {'llm': {'diagnostic_logging': False}})))

    def test_output_failures_and_transport_failures_are_independent(self):
        executor = self.executor()
        invalid = copy.deepcopy(EXAMPLE)
        invalid['coverage'] = ['bad']
        with self.assertRaises(ModelOutputError):
            executor._complete_json_stage(QueueBackend([raw(invalid)]*3), 'input',
                system='sys', purpose='gate', parser=parse)
        m = executor.metrics()
        self.assertEqual(m['total']['call_count'], 3)
        self.assertEqual(m['total']['failed_calls'], 0)
        self.assertEqual(m['total']['invalid_output_count'], 3)
        self.assertTrue(all(c['invalid_output'] and not c['failed'] for c in m['calls']))
        self.assertEqual(m['operations']['gate_primary']['invalid_output_count'], 1)
        self.assertEqual(m['operations']['gate_semantic_retry']['invalid_output_count'], 2)

        class BrokenBackend:
            def complete(self, *args, **kwargs):
                raise RuntimeError('synthetic transport failure')
        other = self.executor()
        with self.assertRaises(ModelError):
            other._complete_json_stage(BrokenBackend(), 'input', system='sys', purpose='gate', parser=parse)
        metrics = other.metrics()
        self.assertGreater(metrics['total']['failed_calls'], 0)
        self.assertEqual(metrics['total']['invalid_output_count'], 0)
        self.assertTrue(all(c['failed'] and not c['invalid_output'] for c in metrics['calls']))

    def test_guard_and_parser_failures_count_each_response_once(self):
        invalid = copy.deepcopy(EXAMPLE)
        invalid['coverage'][0]['explanation'] = 'synthetic'
        changed = copy.deepcopy(EXAMPLE)
        changed['candidates'][0]['memory'] = 'Beta applies.'
        executor = self.executor()
        executor._complete_json_stage(QueueBackend([raw(invalid), raw(changed), raw(EXAMPLE)]),
            'input', system='sys', purpose='gate', parser=parse)
        m = executor.metrics()
        self.assertEqual(m['total']['invalid_output_count'], 2)
        self.assertEqual([c['invalid_output'] for c in m['calls']], [True, True, False])
        self.assertEqual(m['operations']['gate_format_repair']['invalid_output_count'], 1)
        self.assertEqual(m['operations']['gate_semantic_retry']['invalid_output_count'], 0)

    def test_all_emitted_operations_survive_projection(self):
        executor = self.executor()
        stages = ('gate', 'summarize', 'semantic_review', 'coordination', 'target_reconciliation', 'other')
        expected = {f'{stage}_{suffix}' for stage in stages for suffix in ('primary', 'format_repair')}
        expected |= {'gate_semantic_retry', 'gate_coverage_repair'}
        for operation in sorted(expected):
            stage = next(stage for stage in stages if operation.startswith(stage + '_'))
            executor._complete(QueueBackend(['{}']), 'p', system='s', purpose=stage,
                               metric_stage=stage, metric_operation=operation)
        metrics = executor.metrics()
        projected = process_jobs._safe_model_metrics(metrics)
        self.assertEqual(set(metrics['operations']), expected)
        self.assertEqual(set(projected['operations']), expected)
        self.assertEqual(len(projected['calls']), len(expected))
        self.assertEqual(projected, process_jobs._safe_model_metrics(projected))

    def test_concurrent_rejections_attributed_to_exact_call(self):
        executor = self.executor()
        def run(i):
            context = {}
            executor._complete(QueueBackend(['{}']), 'p', system='s', purpose='summarize',
                               metric_context=context)
            if i % 2:
                executor._record_invalid_output(context)
                executor._record_invalid_output(context)  # idempotent counting
            return context['call_index'], bool(i % 2)
        with ThreadPoolExecutor(max_workers=4) as pool:
            expected = dict(pool.map(run, range(24)))
        metrics = executor.metrics()
        self.assertEqual(metrics['total']['invalid_output_count'], 12)
        self.assertEqual({c['call_index']: c['invalid_output'] for c in metrics['calls']}, expected)

    def test_counts_remain_exact_after_call_detail_limit(self):
        executor = self.executor()
        with patch.object(execution, '_MAX_METRIC_CALLS', 2):
            for _ in range(5):
                context = {}
                executor._complete(QueueBackend(['{}']), 'p', system='s', purpose='gate', metric_context=context)
                executor._record_invalid_output(context)
        metrics = executor.metrics()
        self.assertEqual(metrics['total']['call_count'], 5)
        self.assertEqual(metrics['total']['invalid_output_count'], 5)
        self.assertEqual(len(metrics['calls']), 2)
        projected = process_jobs._safe_model_metrics(metrics)
        self.assertEqual(projected['total']['invalid_output_count'], 5)

    def test_durable_failed_and_success_attempts_roundtrip_through_mcp(self):
        invalid = copy.deepcopy(EXAMPLE)
        invalid['coverage'] = ['bad']
        failed_executor = self.executor()
        with self.assertRaises(ModelOutputError) as caught:
            failed_executor._complete_json_stage(QueueBackend([raw(invalid)]*3),
                'PRIVATE-PROMPT', system='PRIVATE-SYSTEM', purpose='gate', parser=parse)
        error = caught.exception
        error.model_metrics = failed_executor.metrics()
        successful_executor = self.executor()
        successful_executor._complete_json_stage(QueueBackend([raw(invalid), raw(EXAMPLE)]),
            'PRIVATE-PROMPT', system='PRIVATE-SYSTEM', purpose='gate', parser=parse)
        with tempfile.TemporaryDirectory() as d, patch(
            'memleaf.process_jobs._launch', return_value=SimpleNamespace(pid=os.getpid(), wait=lambda: 0)):
            service = Memleaf.initialize(Path(d)/'vault')
            job = process_jobs.enqueue(service.vault.root, source='audit', session_id='status')
            queued_again = process_jobs.enqueue(service.vault.root, source='audit', session_id='status')
            self.assertTrue(queued_again['rerun_requested'])
            self.assertTrue(process_jobs._finish(
                service.vault, job['job_id'], status_value='failed', error=error))
            failed = _invoke_tool(service, 'process_status', {'job_id': job['job_id']})['structuredContent']
            self.assertEqual(failed['status'], 'running')
            self.assertEqual(failed['attempts'][0]['error']['model_metrics']['total']['invalid_output_count'], 3)
            process_jobs._finish(service.vault, job['job_id'], status_value='succeeded',
                                 result={'model_metrics': successful_executor.metrics()})
            result = _invoke_tool(service, 'process_status', {'job_id': job['job_id']})['structuredContent']
            metrics = result['aggregate_result']['model_metrics']
            self.assertEqual(metrics['total']['call_count'], 5)
            self.assertEqual(metrics['total']['failed_calls'], 0)
            self.assertEqual(metrics['total']['invalid_output_count'], 4)
            self.assertEqual(metrics['operations']['gate_semantic_retry']['call_count'], 3)
            self.assertEqual(len(metrics['calls']), 5)
            self.assertEqual(sum(c['invalid_output'] for c in metrics['calls']), 4)
            persisted = (service.vault.state_path/'process_jobs.json').read_text()
            self.assertNotIn('PRIVATE-PROMPT', persisted)
            self.assertNotIn('PRIVATE-SYSTEM', persisted)


class StrictRepairBoundaryTests(unittest.TestCase):
    def invalid(self):
        result = copy.deepcopy(EXAMPLE)
        result['coverage'][0]['explanation'] = 'synthetic extra field'
        return result

    def test_bool_int_float_and_high_precision_decimal_mutations_rejected(self):
        for old, new in ((True, 1), (False, 0), (1, 1.0), (1.0, True)):
            a, b = self.invalid(), copy.deepcopy(EXAMPLE)
            a['coverage'][0]['reason'], b['coverage'][0]['reason'] = old, new
            with self.subTest(old=old, new=new), self.assertRaises(ModelOutputError):
                execution._validate_coverage_shape_repair(raw(a), raw(b))
        a, b = self.invalid(), copy.deepcopy(EXAMPLE)
        a['coverage'][0]['reason'] = b['coverage'][0]['reason'] = 'NUMERIC'
        before = raw(a).replace('"NUMERIC"', '1.00000000000000001')
        after = raw(b).replace('"NUMERIC"', '1.0')
        with self.assertRaises(ModelOutputError):
            execution._validate_coverage_shape_repair(before, after)

    def test_duplicate_keys_in_either_side_and_nonfinite_constants_rejected(self):
        a, b = raw(self.invalid()), raw(EXAMPLE)
        dup_a = a.replace('"worth": true', '"worth": false, "worth": true')
        dup_b = b.replace('"worth": true', '"worth": false, "worth": true')
        for before, after in ((dup_a, b), (a, dup_b)):
            with self.subTest(side=before == dup_a), self.assertRaises(ModelOutputError):
                execution._validate_coverage_shape_repair(before, after)
        for constant in ('NaN', 'Infinity', '-Infinity'):
            x, y = self.invalid(), copy.deepcopy(EXAMPLE)
            x['coverage'][0]['reason'] = y['coverage'][0]['reason'] = 'NONFINITE'
            with self.subTest(constant=constant), self.assertRaises(ModelOutputError):
                execution._validate_coverage_shape_repair(
                    raw(x).replace('"NONFINITE"', constant), raw(y).replace('"NONFINITE"', constant))

    def test_repair_input_limits_and_extreme_exponents_fail_closed(self):
        a, b = raw(self.invalid()), raw(EXAMPLE)
        oversized = ' ' * (execution._GATE_STRUCTURE_REPAIR_MAX_BYTES + 1)
        for before, after in ((a + oversized, b), (a, b + oversized)):
            with self.subTest(oversized_side=before != a), self.assertRaises(ModelOutputError):
                execution._validate_coverage_shape_repair(before, after)
        x, y = self.invalid(), copy.deepcopy(EXAMPLE)
        x['coverage'][0]['reason'] = y['coverage'][0]['reason'] = 'HUGE'
        with self.assertRaises(ModelOutputError):
            execution._validate_coverage_shape_repair(
                raw(x).replace('"HUGE"', '1e99999999999999999999999999'),
                raw(y).replace('"HUGE"', '1e99999999999999999999999999'))

    def test_equivalent_decimal_lexemes_remain_allowed(self):
        a, b = self.invalid(), copy.deepcopy(EXAMPLE)
        a['coverage'][0]['reason'] = b['coverage'][0]['reason'] = 'DECIMAL'
        execution._validate_coverage_shape_repair(
            raw(a).replace('"DECIMAL"', '1e0'), raw(b).replace('"DECIMAL"', '1.0'))

    def test_unicode_normalization_is_not_silent_semantic_repair(self):
        a, b = self.invalid(), copy.deepcopy(EXAMPLE)
        a['candidates'][0]['memory'] = 'café'
        b['candidates'][0]['memory'] = 'cafe\u0301'
        with self.assertRaises(ModelOutputError):
            execution._validate_coverage_shape_repair(raw(a), raw(b))

    def test_all_untrusted_field_names_are_suppressed_but_counted(self):
        markers = ['sk_SYNTHETIC_012345', '业务正文', 'x'*100, 'client_account_Alpha']
        with tempfile.TemporaryDirectory() as d:
            service = Memleaf.initialize(Path(d)/'vault')
            cfg = service.vault.config()
            cfg['llm']['diagnostic_logging'] = True
            save_config(service.vault.config_path, cfg)
            invalid = self.invalid()
            for marker in markers:
                invalid['coverage'][0][marker] = 'SYNTHETIC_PRIVATE_VALUE'
            executor = ModelExecutor(service)
            executor._complete_json_stage(QueueBackend([raw(invalid), raw(EXAMPLE)]),
                'SYNTHETIC_PRIVATE_PROMPT', system='system', purpose='gate', parser=parse)
            content = (service.vault.logs_path/'model-diagnostics.jsonl').read_text()
            diagnostic = json.loads(content.splitlines()[0])
            self.assertEqual(diagnostic['coverage_unexpected_field_count'], 5)
            self.assertEqual(diagnostic['coverage_unexpected_fields'], ['explanation'])
            for marker in markers + ['SYNTHETIC_PRIVATE_VALUE', 'SYNTHETIC_PRIVATE_PROMPT']:
                self.assertNotIn(marker, content)


if __name__ == '__main__':
    unittest.main(verbosity=2)

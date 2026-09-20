"""G2 boundary regressions. Synthetic input, temporary Vaults, no network/model."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from memleaf import Memleaf
from memleaf.extraction_work_state import extraction_work_id, reserve_model_request
from memleaf.inbox import parse_inbox
from memleaf.index import EVENT_V2_BLOCK
from memleaf.models import utc_now
from memleaf.process_common import ProcessingError
from memleaf.process_journal import ProcessJournal
from memleaf.single_pass_memory_planner import SinglePassMemoryPlanner
from memleaf.turn_plan import FrozenTurn, input_digest, turn_plan_key
from memleaf.memory_writer import MemoryWriter
from memleaf.memory_commit import MemoryCommitter
from memleaf.turn_audit import TurnAudit
from test_incremental_execution import Backend, output


class StubBackend:
    single_pass_safe = True
    single_pass_protocol = True
    def __init__(self):
        self.calls = 0
    def complete(self, *args, **kw):
        self.calls += 1
        return '{}'


def no_writes(planner, backend, turn, state, **kw):
    """Test orchestration only; this is not a semantic-quality model fixture."""
    backend.complete('contract fixture', purpose='single_pass')
    return [], []


class SourceCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.s = Memleaf.initialize(Path(self.temp.name) / 'vault')
        self.path = self.s.vault.processed_state_path

    def capture(self, role='user', content='A sustained task.', mid='u', rev='r1', **kw):
        return self.s.capture('host', 's', 't', role, content,
                              message_id=mid, message_revision=rev, **kw)

    def pair(self):
        self.capture(source_sequence=1)
        self.capture('assistant', 'Acknowledged.', 'a', source_sequence=2,
                     previous_message_id='u', final=True)
        return parse_inbox(self.s.vault)[0]

    def state(self):
        return json.loads(self.path.read_text())

    def save(self, value):
        self.path.write_text(json.dumps(value), encoding='utf-8')

    def processed(self, turn, **extra):
        state = self.state()
        state['sessions']['host/s'].update(watermark=1, processed_watermark=1,
            processed_turns=[dict(turn_key=turn.turn_key, turn_index=1,
                event_keys=list(turn.event_keys), memory_ids=['mem-prior'],
                eligible_cleanup_at='2020-01-01T00:00:00Z', **extra)])
        self.save(state)

    def snapshot(self):
        return ProcessJournal(self.s)._snapshot(source='host', session_id='s',
                                                now=utc_now(), cleanup_hours=24)[0]

    def revise(self):
        self.capture(content='Revised task.', rev='r2', previous_message_revision='r1', source_sequence=1)
        self.capture('assistant', 'Revised reply.', 'a', rev='r2', previous_message_revision='r1',
                     previous_message_id='u', source_sequence=2, final=True)


class ExplicitIntentTests(SourceCase):
    @staticmethod
    def no_memory_backend(*responses):
        values = responses or (output({"action": "NO_MEMORY"}),)
        return Backend(*values)

    def test_public_remember_forwards_intent_and_retransmission_is_zero_call(self):
        model = self.no_memory_backend()
        first = self.s.remember('Retain this.', intent_id='user-action', model=model)
        second = self.s.remember('Retain this.', intent_id='user-action', model=model)
        self.assertEqual(first['intent_id'], 'user-action')
        self.assertEqual(second['processed_turns'], 0)
        self.assertEqual(len(model.calls), 1)

    def test_same_intent_with_different_text_is_not_a_duplicate(self):
        model = self.no_memory_backend()
        self.s.remember('A', intent_id='one', model=model)
        with self.assertRaisesRegex(ValueError, 'remember_binding_or_pipeline_changed'):
            self.s.remember('B', intent_id='one', model=Backend())
        self.assertEqual(len(model.calls), 1)

    def test_same_intent_with_changed_scope_is_rejected(self):
        model = self.no_memory_backend()
        self.s.remember('A', intent_id='one', scopes=['global'], model=model)
        with self.assertRaisesRegex(ValueError, 'remember_binding_or_pipeline_changed'):
            self.s.remember('A', intent_id='one', scopes=['project:Other'], model=Backend())
        self.assertEqual(len(model.calls), 1)

    def test_new_intent_in_same_host_turn_has_independent_receipt(self):
        model = self.no_memory_backend(
            output({"action": "NO_MEMORY"}),
            output({"action": "NO_MEMORY"}),
        )
        for intent in ('one', 'two'):
            result = self.s.remember('A', intent_id=intent, turn_id='same-turn',
                                     event_id='same-source', model=model)
            self.assertEqual(result['processed_turns'], 1)
        self.assertEqual(len(model.calls), 2)

    def test_stable_event_retry_reuses_incremental_intent(self):
        model = self.no_memory_backend()
        first = self.s.remember('A', event_id='stable-call', model=model)
        second = self.s.remember('A', event_id='stable-call', model=model)
        self.assertEqual(first['intent_id'], second['intent_id'])
        self.assertEqual(len(model.calls), 1)

    def test_frozen_explicit_retry_keeps_original_time(self):
        j = ProcessJournal(self.s)
        args = dict(content='Tomorrow', source='memleaf', session_id='remember', turn_id='explicit',
                    event_key_value='a'*64, scopes=None, cleanup_hours=24)
        first, _, turn, _ = j._remember_turn(**args, now='2026-09-17T00:00:00Z')
        frozen = FrozenTurn.build(turn, []).to_dict()
        j._mark_failed([first], RuntimeError('simulated stop'))
        _, _, retried, _ = j._remember_turn(**args, now='2026-09-18T00:00:00Z')
        self.assertEqual(retried.events[0].source_time, turn.events[0].source_time)
        self.assertEqual(input_digest(turn), input_digest(retried))
        self.assertEqual(FrozenTurn.restore(frozen, retried)['requests'], [])

    def test_completed_incremental_intent_without_receipt_is_not_rebound(self):
        model = self.no_memory_backend()
        self.s.remember('A', intent_id='one', model=model)
        state = self.state()
        state['events'] = {}
        self.save(state)
        with self.assertRaisesRegex(ValueError, 'remember_source_binding_unavailable'):
            self.s.remember('A', intent_id='one', model=Backend())
        self.assertEqual(self.state()['events'], {})



class ImmutableSourceTests(SourceCase):
    def test_same_revision_cannot_change_time(self):
        self.capture(source_time='2026-09-17T10:00:00+08:00')
        with self.assertRaisesRegex(ValueError, 'different payload'):
            self.capture(source_time='2026-09-18T10:00:00+08:00')

    def test_same_revision_cannot_change_sequence(self):
        self.capture(source_sequence=1)
        with self.assertRaisesRegex(ValueError, 'different payload'):
            self.capture(source_sequence=2)

    def test_same_revision_cannot_upgrade_final(self):
        self.capture('assistant', 'Reply', 'a', final=False)
        with self.assertRaisesRegex(ValueError, 'different payload'):
            self.capture('assistant', 'Reply', 'a', final=True)

    def test_same_revision_cannot_change_predecessor(self):
        self.capture(previous_message_id='previous')
        with self.assertRaisesRegex(ValueError, 'different payload'):
            self.capture(previous_message_id='different')

    def test_same_revision_changed_text_after_cleanup_is_rejected(self):
        self.capture()
        self.s.vault.session_path('host', 's').unlink()
        with self.assertRaisesRegex(ValueError, 'different payload'):
            self.capture(content='Changed without revision')

    def test_identical_revision_after_cleanup_is_duplicate(self):
        self.capture()
        self.s.vault.session_path('host', 's').unlink()
        self.assertTrue(self.capture().duplicate)

    def test_real_revision_after_cleanup_uses_receipt_predecessor(self):
        turn = self.pair()
        self.processed(turn, cleanup_done_at=utc_now())
        self.s.vault.session_path('host', 's').unlink()
        result = self.capture(content='Revised after cleanup', rev='r2', previous_message_revision='r1', source_sequence=1)
        self.assertTrue(result.stored)
        self.assertEqual(self.state()['sessions']['host/s']['revised_turns'][0]['memory_ids'], ['mem-prior'])

    def test_revision_chain_is_not_sorted_by_label_or_event_hash(self):
        self.capture(rev='z')
        self.capture(content='second', rev='a', previous_message_revision='z')
        self.capture(content='third', rev='m', previous_message_revision='a')
        self.s.vault.session_path('host', 's').unlink()
        self.assertTrue(self.capture(content='fourth', rev='b', previous_message_revision='m').stored)
        with self.assertRaisesRegex(ValueError, 'predecessor mismatch'):
            self.capture(content='fork', rev='fork', previous_message_revision='a')


class RevisionDeliveryTests(SourceCase):
    def test_predecessor_can_arrive_after_reply(self):
        self.capture('assistant', 'Final', 'a', source_sequence=2, previous_message_id='u', final=True)
        self.capture(source_sequence=1)
        turn = parse_inbox(self.s.vault)[0]
        self.assertTrue(turn.complete)
        self.assertEqual([e.message_id for e in turn.events], ['u', 'a'])

    def test_late_initial_predecessor_does_not_drop_user_chain(self):
        self.capture(content='Second', mid='u2', source_sequence=2, previous_message_id='u')
        self.capture('assistant', 'Final', 'a', source_sequence=3, previous_message_id='u2', final=True)
        self.capture(source_sequence=1)
        turn = parse_inbox(self.s.vault)[0]
        self.assertTrue(turn.complete)
        self.assertEqual([e.message_id for e in turn.events], ['u', 'u2', 'a'])

    def test_new_source_envelope_without_final_is_not_complete(self):
        self.capture(source_sequence=1)
        self.capture('assistant', 'Still processing', 'a', source_sequence=2)
        self.assertFalse(parse_inbox(self.s.vault)[0].complete)

    def test_legacy_capture_without_source_metadata_remains_complete(self):
        self.s.capture('legacy', 's', 't', 'user', 'Question')
        self.s.capture('legacy', 's', 't', 'assistant', 'Answer')
        self.assertTrue(parse_inbox(self.s.vault)[0].complete)

    def test_actual_revision_still_invalidates_prior_reply(self):
        self.pair()
        self.capture(content='Edited', rev='r2', previous_message_revision='r1', source_sequence=1)
        self.assertFalse(parse_inbox(self.s.vault)[0].complete)

    def test_pending_revision_is_not_deleted_by_old_cleanup(self):
        turn = self.pair()
        self.processed(turn)
        self.revise()
        snapshots = self.snapshot()
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0].turn.events[0].content, 'Revised task.')
        self.assertTrue(self.s.vault.session_path('host', 's').exists())

    def test_cleanup_removes_only_recorded_event_versions(self):
        old = self.pair()
        self.revise()
        path = self.s.vault.session_path('host', 's')
        ProcessJournal(self.s)._remove_turn_blocks(path, dict(turn_key=old.turn_key, turn_index=1,
                                                            event_keys=list(old.event_keys)))
        turn = parse_inbox(self.s.vault)[0]
        self.assertEqual([e.message_revision for e in turn.events], ['r2', 'r2'])

    def test_revision_and_deferred_queues_do_not_schedule_twice(self):
        turn = self.pair()
        self.processed(turn, deferred_evidence=[dict(decision='DEFERRED', reason='coverage_omitted')])
        self.revise()
        self.assertEqual(len(self.snapshot()), 1)

    def test_failed_receipt_save_is_recovered_by_identical_capture(self):
        turn = self.pair()
        snapshots = self.snapshot()
        token = snapshots[0].token
        with patch('memleaf.capture.atomic_write_json', side_effect=OSError('injected receipt failure')):
            with self.assertRaises(OSError):
                self.capture(content='Edit', rev='r2', previous_message_revision='r1', source_sequence=1)
        self.assertEqual(self.state()['sessions']['host/s']['processing']['token'], token)
        self.assertTrue(self.capture(content='Edit', rev='r2', previous_message_revision='r1', source_sequence=1).duplicate)
        state = self.state()['sessions']['host/s']
        self.assertEqual(state['processing']['reason'], 'source_revision_changed')
        self.assertTrue(state['revised_turns'])
        self.assertEqual(len(self.state()['events']), 3)

    def test_commit_checks_disk_even_before_receipt_recovery(self):
        self.pair()
        snapshot = self.snapshot()[0]
        with patch('memleaf.capture.atomic_write_json', side_effect=OSError('injected receipt failure')):
            with self.assertRaises(OSError):
                self.capture(content='Edit', rev='r2', previous_message_revision='r1', source_sequence=1)
        writer = MemoryWriter(self.s)
        journal = ProcessJournal(self.s)
        committer = MemoryCommitter(self.s, writer=writer, audit=TurnAudit(), journal=journal)
        with self.assertRaisesRegex(ProcessingError, 'source revision changed'):
            committer._commit_success([snapshot], [], now=utc_now(), cleanup_hours=24)
        self.assertEqual(self.s.vault.list_markdown('knowledge'), [])

    def test_snapshot_repairs_receipts_without_another_capture(self):
        self.pair()
        self.processed(parse_inbox(self.s.vault)[0])
        with patch('memleaf.capture.atomic_write_json', side_effect=OSError('injected receipt failure')):
            with self.assertRaises(OSError):
                self.capture(content='Edit', rev='r2', previous_message_revision='r1', source_sequence=1)
        self.snapshot()
        state = self.state()['sessions']['host/s']
        self.assertTrue(state['revised_turns'])
        self.assertTrue(self.s.vault.session_path('host', 's').exists())

    def test_pending_applied_target_is_kept_for_revision_coordination(self):
        turn = self.pair()
        state = self.state()
        state['pending_operations'] = {'op': dict(source='host', session_id='s', turn_key=turn.turn_key,
                                                 memory_id='mem-applied-before-crash')}
        self.save(state)
        self.capture(content='Edit', rev='r2', previous_message_revision='r1', source_sequence=1)
        self.assertIn('mem-applied-before-crash', self.state()['sessions']['host/s']['revised_turns'][0]['memory_ids'])


class WorkAndCompatibilityTests(SourceCase):
    def test_partial_commit_keeps_remaining_request_budget(self):
        self.pair()
        model = StubBackend()
        def partial(planner, backend, turn, state, **kw):
            backend.complete('fixture', purpose='single_pass')
            ref = (turn.source, turn.session_id, turn.turn_key)
            planner.audit._evidence_by_turn[ref] = [dict(unit_id='e1', decision='DEFERRED', reason='coverage_omitted')]
            return [], []
        with patch.object(SinglePassMemoryPlanner, '_collect_turn_outputs', partial):
            self.assertEqual(self.s.process(source='host', session_id='s', model=model)['coverage_status'], 'partial')
        ledger = json.loads((self.s.vault.state_path/'extraction_request_budget.json').read_text())
        row = next(iter(next(iter(ledger['works'].values()))['turns'].values()))
        self.assertFalse(row['completed'])
        with patch.object(SinglePassMemoryPlanner, '_collect_turn_outputs', no_writes):
            self.s.process(source='host', session_id='s', model=model, scope=['global'])
        self.assertEqual(model.calls, 2)

    def legacy_turn(self):
        self.pair()
        path = self.s.vault.session_path('host', 's')
        fields = ('message_id', 'message_revision', 'previous_message_revision', 'source_sequence',
                  'previous_message_id', 'source_time', 'captured_at', 'final')
        def strip(match):
            metadata = json.loads(match.group('meta'))
            for field in fields: metadata.pop(field, None)
            return match.group(0).replace(match.group('meta'), json.dumps(metadata))
        path.write_text(EVENT_V2_BLOCK.sub(strip, path.read_text()))
        # This represents a pre-G2 capture ledger, not corrupting a new receipt.
        value = self.state()
        for row in value['events'].values():
            for field in (*fields, 'payload_digest'): row.pop(field, None)
        self.save(value)
        return parse_inbox(self.s.vault)[0]

    def test_pre_g2_frozen_input_digest_restores_unchanged_legacy_source(self):
        turn = self.legacy_turn()
        stored = FrozenTurn.build(turn, []).to_dict()
        payload = json.loads(stored['payload']); payload.pop('input_schema_version', None)
        events = [dict(event_key=e.event_key, role=e.role, content=e.content, tool_evidence=list(e.tool_evidence)) for e in turn.events]
        raw = json.dumps([turn.source, turn.session_id, turn.turn_key, events], ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        payload['input_digest'] = hashlib.sha256(raw.encode()).hexdigest()
        stored['payload'] = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        stored['checksum'] = hashlib.sha256(stored['payload'].encode()).hexdigest()
        self.assertEqual(FrozenTurn.restore(stored, turn)['requests'], [])
        from dataclasses import replace
        changed = replace(turn, events=(replace(turn.events[0], source_time='2026-09-17T00:00:00Z'), *turn.events[1:]))
        with self.assertRaises(ValueError):
            FrozenTurn.restore(stored, changed)

    def test_legacy_job_budget_migration_preserves_exhaustion(self):
        turn = self.legacy_turn()
        old_id = f'{turn.source}/{turn.session_id}/{turn.turn_key}'
        path = self.s.vault.state_path/'extraction_request_budget.json'
        path.write_text(json.dumps(dict(version=1, works={'job-old': {'turns': {old_id: 3}}}, order=['job-old'])))
        model = StubBackend()
        with patch.object(SinglePassMemoryPlanner, '_collect_turn_outputs', no_writes):
            with self.assertRaises(Exception):
                self.s.process(source='host', session_id='s', model=model)
        self.assertEqual(model.calls, 0)
        migrated = json.loads(path.read_text())
        work = extraction_work_id(turn, request_kind='automatic', intent_id='automatic')
        self.assertEqual(migrated['works'][work]['turns'][old_id]['requests'], 3)

    def test_missing_source_time_is_still_unknown(self):
        self.capture()
        self.assertIsNone(parse_inbox(self.s.vault)[0].events[0].source_time)

    def test_private_revised_message_body_never_enters_receipt_or_inbox(self):
        self.capture(record=False)
        result = self.capture(content='PRIVATE-BODY-MARKER', rev='r2', previous_message_revision='r1')
        self.assertTrue(result.suppressed)
        for file in self.s.vault.root.rglob('*'):
            if file.is_file(): self.assertNotIn(b'PRIVATE-BODY-MARKER', file.read_bytes())


if __name__ == '__main__':
    unittest.main()

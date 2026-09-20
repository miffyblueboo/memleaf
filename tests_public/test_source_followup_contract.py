"""Source-order, legacy-budget and adapter boundary regressions; no network."""
from __future__ import annotations

import json
from unittest.mock import patch

from memleaf.extraction_work_state import ExtractionWorkStateError, extraction_work_id, reserve_model_request
from memleaf.inbox import parse_inbox
from memleaf.models import utc_now
from test_a083_regressions import SourceCase
from test_incremental_execution import Backend, output
from memleaf.llm.base import ModelError


class BudgetFollowupTests(SourceCase):
    def legacy_budget(self, turn, count=3):
        tid = f"{turn.source}/{turn.session_id}/{turn.turn_key}"
        path = self.s.vault.state_path / 'extraction_request_budget.json'
        path.write_text(json.dumps(dict(version=1, works={'job-old': {'turns': {tid: count}}}, order=['job-old'])))
        return path, tid

    def test_unbound_legacy_budget_blocks_incremental_without_new_allowance(self):
        turn = self.pair()
        path, _ = self.legacy_budget(turn)
        before = path.read_bytes()
        model = Backend(output({"action": "NO_MEMORY"}))
        result = self.s.process(source='host', session_id='s', model=model)
        self.assertEqual(result['coverage_status'], 'partial')
        self.assertIn('budget_state_or_migration_required',
                      [row.get('code') for row in result['results']])
        self.assertEqual(len(model.calls), 0)
        self.assertEqual(path.read_bytes(), before)

    def test_new_explicit_intent_does_not_inherit_unbound_automatic_budget(self):
        self.legacy_budget(self.pair())
        model = Backend(output({"action": "NO_MEMORY"}))
        self.s.remember('Remember explicitly', intent_id='new-user-intent', model=model)
        self.assertEqual(len(model.calls), 1)

    def test_existing_work_does_not_hide_remaining_legacy_consumption(self):
        turn = self.pair()
        path, tid = self.legacy_budget(turn, 2)
        wid = extraction_work_id(turn, request_kind='automatic', intent_id='automatic')
        data = json.loads(path.read_text())
        data['works'][wid] = {'turns': {tid: 1}}
        data['order'].append(wid)
        path.write_text(json.dumps(data))
        # Direct primitive call explicitly confirms the legacy identity.
        ordinal = reserve_model_request(self.s.vault, work_id=wid, turn_id=tid,
                                        legacy_turn_id=tid, request_limit=3)
        self.assertIsNone(ordinal)
        data = json.loads(path.read_text())
        self.assertEqual(data['works'][wid]['turns'][tid]['requests'], 3)
        self.assertNotIn('job-old', data['works'])
        self.assertIsNone(reserve_model_request(self.s.vault, work_id=wid, turn_id=tid,
                                               legacy_turn_id=tid, request_limit=3))


class OrderedBackend:
    single_pass_safe = True

    def __init__(self, *, fail_on_call=None):
        self.calls = []
        self.seen = []
        self.fail_on_call = fail_on_call

    def complete(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        payload = json.loads(prompt)
        user_text = next(
            item['text'] for item in payload.get('evidence', [])
            if item.get('role') == 'user'
        )
        self.seen.append(user_text)
        if self.fail_on_call is not None and len(self.calls) == self.fail_on_call:
            raise ModelError('temporary', code='model_timeout')
        return output({"action": "NO_MEMORY"})


class SourceOrderTests(SourceCase):
    def turn(self, number, *, final=True, sequence=True):
        for role, offset in [('user', 0), ('assistant', 1)]:
            self.s.capture('ordered', 's', f't{number}', role, f'{role}-{number}',
                           message_id=f'{number}/{role}', final=final if role == 'assistant' else None,
                           source_sequence=number * 2 + offset if sequence else None)

    def test_cross_turn_parse_uses_source_order_without_reassigning_local_indices(self):
        self.turn(2); self.turn(1)
        turns = parse_inbox(self.s.vault)
        self.assertEqual([t.events[0].content for t in turns], ['user-1', 'user-2'])
        self.assertEqual([t.turn_index for t in turns], [2, 1])

    def test_cross_turn_snapshot_uses_source_order(self):
        self.turn(2); self.turn(1)
        shots, _ = ProcessJournal(self.s)._snapshot(source='ordered', session_id='s', now=utc_now(), cleanup_hours=24)
        self.assertEqual([s.turn.events[0].content for s in shots], ['user-1', 'user-2'])

    def test_backlog_cap_does_not_skip_lower_local_indices(self):
        for i in range(6, 0, -1): self.turn(i)
        backend = OrderedBackend()
        first = self.s.process(source='ordered', session_id='s', model=backend)
        second = self.s.process(source='ordered', session_id='s', model=backend)
        third = self.s.process(source='ordered', session_id='s', model=backend)
        self.assertEqual(first['processed_turns'], 4)
        self.assertEqual(second['processed_turns'], 2)
        self.assertEqual(third['processed_turns'], 0)
        self.assertEqual(backend.seen, [f'user-{i}' for i in range(1, 7)])

    def test_transport_failure_after_first_turn_keeps_second_turn_pending(self):
        self.turn(2); self.turn(1)
        backend = OrderedBackend(fail_on_call=2)
        first = self.s.process(source='ordered', session_id='s', model=backend)
        self.assertEqual(first['processed_turns'], 1)
        self.assertEqual(first['coverage_status'], 'partial')
        self.assertEqual(backend.seen, ['user-1', 'user-2'])

        second = self.s.process(source='ordered', session_id='s', model=backend)
        self.assertEqual(second['processed_turns'], 0)
        self.assertEqual(backend.seen, ['user-1', 'user-2'])

        recovered = self.s.process(source='ordered', session_id='s', model=backend, recover=True)
        self.assertEqual(recovered['processed_turns'], 1)
        self.assertEqual(backend.seen, ['user-1', 'user-2', 'user-2'])

    def test_known_earlier_incomplete_turn_prevents_later_overtake(self):
        self.turn(2); self.turn(1, final=False)
        model = OrderedBackend()
        result = self.s.process(source='ordered', session_id='s', model=model)
        self.assertEqual(result['processed_turns'], 0)
        self.assertEqual(len(model.calls), 0)

    def test_overlapping_source_ranges_do_not_invent_cross_turn_order(self):
        self.turn(2)
        self.s.capture('ordered', 's', 'overlap', 'user', 'overlap-user',
                       message_id='ou', source_sequence=3)
        self.s.capture('ordered', 's', 'overlap', 'assistant', 'overlap-final',
                       message_id='oa', source_sequence=6, final=True)
        self.assertEqual([t.events[0].content for t in parse_inbox(self.s.vault)], ['user-2', 'overlap-user'])

    def test_zero_source_sequence_is_a_real_position(self):
        self.turn(1); self.turn(0)
        shots, _ = ProcessJournal(self.s)._snapshot(source='ordered', session_id='s', now=utc_now(), cleanup_hours=24)
        self.assertEqual([s.turn.events[0].content for s in shots], ['user-0', 'user-1'])

    def test_unknown_source_order_keeps_legacy_arrival_order(self):
        self.turn(2, sequence=False); self.turn(1, sequence=False)
        self.assertEqual([t.events[0].content for t in parse_inbox(self.s.vault)], ['user-2', 'user-1'])


class CompletionPromptTests(SourceCase):
    def test_summary_does_not_require_observation_as_completion_time(self):
        from memleaf.prompts import SUMMARIZE_SYSTEM
        self.assertNotIn('completed requires completed_at grounded in the admitted event timestamp', SUMMARIZE_SYSTEM)
        self.assertIn('actual completion time', SUMMARIZE_SYSTEM)


if __name__ == '__main__':
    import unittest
    unittest.main()

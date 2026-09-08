"""Review context must preserve qualifications without widening source authority."""
import unittest

from memleaf.admission import analyze_turn_evidence, validate_bindings
from memleaf.update_coordinator import UpdateCoordinator
from memleaf.update_review import build_create_review_prompt


class ReviewSourceContextTests(unittest.TestCase):
    def test_narrow_quote_keeps_current_message_qualification_for_review(self):
        source = 'Cedar needs an export. This request was later cancelled.'
        events = [{'role': 'assistant', 'event_key': 'a', 'content': source,
                   'tool_evidence': [{'content': 'RAW_PAYLOAD_SECRET'}]},
                  {'role': 'tool', 'event_key': 't', 'content': 'RAW_TOOL_SECRET'}]
        units = analyze_turn_evidence(events)
        unit = next(u for u in units if u.source_role == 'assistant')
        candidate = {'candidate_id': 'c', 'evidence_event_ids': ['a']}
        candidate['_evidence_bindings'] = validate_bindings([
            {'candidate_id': 'c', 'claims': [{'unit_id': unit.unit_id,
              'quote': 'Cedar needs an export.', 'role': 'assertion'}]}], units, [candidate])['c']
        projected = UpdateCoordinator._projected_request_evidence(
            {'candidate_id': 'c'}, candidates={'c': candidate}, evidence_units=units, events=events)
        self.assertEqual(projected[0]['content'], 'Cedar needs an export.')
        self.assertEqual(projected[0]['source_context'], source)
        prompt = build_create_review_prompt(projected, {})
        self.assertIn('This request was later cancelled.', prompt)
        self.assertNotIn('RAW_PAYLOAD_SECRET', prompt)
        self.assertNotIn('RAW_TOOL_SECRET', prompt)

    def test_multiple_quotes_share_one_context_without_replacing_bound_spans(self):
        source = 'Cedar is the project. Build an export. It remains unassigned.'
        events = [{'role': 'assistant', 'event_key': 'a', 'content': source}]
        units = analyze_turn_evidence(events)
        candidate = {'candidate_id': 'c', 'evidence_event_ids': ['a']}
        quotes = ['Cedar is the project.', 'Build an export.']
        candidate['_evidence_bindings'] = validate_bindings([
            {'candidate_id': 'c', 'claims': [{'unit_id': units[0].unit_id,
                'quote': q, 'role': 'assertion'} for q in quotes]}], units, [candidate])['c']
        projected = UpdateCoordinator._projected_request_evidence(
            {'candidate_id': 'c'}, candidates={'c': candidate}, evidence_units=units, events=events)
        self.assertEqual([row['content'] for row in projected], quotes)
        self.assertEqual(sum('source_context' in row for row in projected), 1)


if __name__ == '__main__':
    unittest.main()

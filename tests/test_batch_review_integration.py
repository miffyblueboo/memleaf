from __future__ import annotations

import json
import unittest

from memleaf.admission import EvidenceUnit
from memleaf.inbox import InboxEvent, InboxTurn
from memleaf.turn_audit import TurnAudit
from memleaf.update_coordinator import UpdateCoordinator


class BatchAcceptExecutor:
    def __init__(self):
        self.calls: list[str] = []

    def _complete_json_stage(
        self,
        backend,
        prompt,
        *,
        system,
        purpose,
        parser,
        diagnostic_context=None,
        metric_stage=None,
    ):
        self.calls.append(prompt)
        if not prompt.startswith("CREATE_SEMANTIC_REVIEW_BATCH\n"):
            raise AssertionError("integration path unexpectedly used a single review")
        payload = json.loads(prompt.split("\n", 1)[1].split("\n\nReview each row", 1)[0])
        raw = json.dumps({
            "reviews": [
                {"review_id": row["review_id"], "decision": "ACCEPT"}
                for row in reversed(payload["reviews"])
            ]
        })
        return parser(raw)


def build_create_case(count: int):
    turn_events = []
    units = []
    candidates = {}
    requests = []
    for index in range(count):
        event_key = f"event-{index}"
        unit_id = f"unit-{index}"
        candidate_id = f"candidate-{index}"
        text = f"Synthetic fact {index}."
        turn_events.append(InboxEvent(
            "p3-synthetic",
            "session",
            "turn",
            1,
            "user",
            event_key,
            text,
            timestamp="2026-09-10T06:00:00Z",
        ))
        units.append(EvidenceUnit(
            unit_id,
            event_key,
            "user_assertion",
            text,
            source_role="user",
        ))
        candidates[candidate_id] = {
            "candidate_id": candidate_id,
            "memory": text,
            "evidence_event_ids": [event_key],
            "evidence_unit_ids": [unit_id],
            "_evidence_bindings": [{
                "unit_id": unit_id,
                "quote": text,
                "role": "assertion",
            }],
            "type": "fact",
            "scopes": ["global"],
            "scope_source": "model",
        }
        requests.append({
            "summary": {
                "title": text,
                "body": text,
                "tags": [],
                "type": "fact",
                "scopes": ["global"],
                "scope_source": "model",
                "sources": [{"event_key": event_key}],
            },
            "turn": None,
            "candidate_id": candidate_id,
            "memory_id": f"memory-{index}",
            "evidence_unit_ids": [unit_id],
        })
    turn = InboxTurn("p3-synthetic", "session", "turn", 1, tuple(turn_events))
    for request in requests:
        request["turn"] = turn
    event_payload = [
        {
            "event_key": event.event_key,
            "role": event.role,
            "content": event.content,
            "timestamp": event.timestamp,
        }
        for event in turn.events
    ]
    return requests, candidates, units, event_payload


def run_create_case(count: int):
    requests, candidates, units, events = build_create_case(count)
    executor = BatchAcceptExecutor()
    result = UpdateCoordinator(executor, TurnAudit(), lambda memory_id: None).resolve(
        requests,
        candidates=candidates,
        evidence_units=units,
        events=events,
        backend="synthetic-backend",
        scope_registry={},
        validation_scope_registry={},
    )
    return result, executor


class BatchReviewIntegrationTests(unittest.TestCase):
    def test_four_independent_creates_use_one_review_call(self):
        result, executor = run_create_case(4)
        self.assertEqual(len(result), 4)
        self.assertEqual(len(executor.calls), 1)

    def test_five_independent_creates_use_two_review_calls(self):
        result, executor = run_create_case(5)
        self.assertEqual(len(result), 5)
        self.assertEqual(len(executor.calls), 2)


if __name__ == "__main__":
    unittest.main()

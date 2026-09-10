from __future__ import annotations

import json
import unittest

from memleaf.batch_review import review_create_batch, review_update_batch


class _BatchBackend:
    structured_batch_safe = True


_BATCH_BACKEND = _BatchBackend()


class ScriptedExecutor:
    def __init__(self, batch_raw, singles=()):
        self.batch_raw = batch_raw
        self.singles = list(singles)
        self.calls = []

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
        self.calls.append({
            "prompt": prompt,
            "system": system,
            "purpose": purpose,
            "metric_stage": metric_stage,
        })
        if "_BATCH\n" in prompt:
            raw = self.batch_raw
        else:
            raw = self.singles.pop(0)
        return parser(raw)


def create_spec(review_id: str):
    return {
        "review_id": review_id,
        "admitted_source": [{
            "event_key": f"event-{review_id}",
            "timestamp": "2026-09-10T06:00:00Z",
            "role": "user",
            "content": f"Fact {review_id}.",
            "evidence_origin": "user_assertion",
            "unit_id": f"unit-{review_id}",
            "section_path": [],
        }],
        "proposed_summary": {
            "title": f"Fact {review_id}",
            "body": f"Fact {review_id}.",
            "tags": [],
            "type": "fact",
            "scopes": ["global"],
            "scope_source": "model",
            "sources": [{"event_key": f"event-{review_id}"}],
        },
        "parse_summary": lambda value: dict(value),
        "diagnostic_context": {"source": "src", "session_id": "s", "turn_index": 1},
    }


def update_spec(review_id: str):
    value = create_spec(review_id)
    value["target"] = {
        "memory_id": f"mem-{review_id}",
        "title": f"Fact {review_id}",
        "body": "Old fact.",
        "type": "fact",
        "scopes": ["global"],
    }
    value["proposed_summary"]["update_memory_id"] = f"mem-{review_id}"
    return value


class BatchReviewTests(unittest.TestCase):
    def test_four_create_reviews_share_one_model_call(self):
        ids = ["c1", "c2", "c3", "c4"]
        raw = json.dumps({
            "reviews": [
                {"review_id": review_id, "decision": "ACCEPT"}
                for review_id in reversed(ids)
            ]
        })
        executor = ScriptedExecutor(raw)
        outcomes = review_create_batch(executor, _BATCH_BACKEND, [create_spec(value) for value in ids])
        self.assertEqual(outcomes, [{"decision": "ACCEPT"}] * 4)
        self.assertEqual(len(executor.calls), 1)
        self.assertTrue(executor.calls[0]["prompt"].startswith("CREATE_SEMANTIC_REVIEW_BATCH\n"))
        self.assertEqual(executor.calls[0]["metric_stage"], "semantic_review")

    def test_partial_invalid_row_retries_only_that_review(self):
        raw = json.dumps({
            "reviews": [
                {"review_id": "c1", "decision": "ACCEPT"},
                {"review_id": "c2", "decision": "BROKEN"},
                {"review_id": "c3", "decision": "NO_CHANGE"},
            ]
        })
        executor = ScriptedExecutor(raw, [json.dumps({"decision": "ACCEPT"})])
        outcomes = review_create_batch(
            executor,
            _BATCH_BACKEND,
            [create_spec("c1"), create_spec("c2"), create_spec("c3")],
        )
        self.assertEqual(outcomes, [
            {"decision": "ACCEPT"},
            {"decision": "ACCEPT"},
            {"decision": "NO_CHANGE"},
        ])
        self.assertEqual(len(executor.calls), 2)
        self.assertTrue(executor.calls[1]["prompt"].startswith("CREATE_SEMANTIC_REVIEW\n"))

    def test_missing_row_retries_only_missing_review(self):
        raw = json.dumps({
            "reviews": [
                {"review_id": "c1", "decision": "ACCEPT"},
                {"review_id": "c3", "decision": "ACCEPT"},
            ]
        })
        executor = ScriptedExecutor(raw, [json.dumps({"decision": "NO_CHANGE"})])
        outcomes = review_create_batch(
            executor,
            _BATCH_BACKEND,
            [create_spec("c1"), create_spec("c2"), create_spec("c3")],
        )
        self.assertEqual(outcomes[0], {"decision": "ACCEPT"})
        self.assertEqual(outcomes[1], {"decision": "NO_CHANGE"})
        self.assertEqual(outcomes[2], {"decision": "ACCEPT"})
        self.assertEqual(len(executor.calls), 2)

    def test_unassociated_batch_failure_falls_back_to_all_legacy_singles(self):
        raw = json.dumps({"reviews": [{"decision": "ACCEPT"}]})
        executor = ScriptedExecutor(
            raw,
            [
                json.dumps({"decision": "ACCEPT"}),
                json.dumps({"decision": "NO_CHANGE"}),
                json.dumps({"decision": "ACCEPT"}),
            ],
        )
        outcomes = review_create_batch(
            executor,
            _BATCH_BACKEND,
            [create_spec("c1"), create_spec("c2"), create_spec("c3")],
        )
        self.assertEqual(outcomes, [
            {"decision": "ACCEPT"},
            {"decision": "NO_CHANGE"},
            {"decision": "ACCEPT"},
        ])
        self.assertEqual(len(executor.calls), 4)

    def test_backend_without_batch_capability_uses_legacy_single_reviews(self):
        executor = ScriptedExecutor(
            "unused",
            [json.dumps({"decision": "ACCEPT"}), json.dumps({"decision": "NO_CHANGE"})],
        )
        outcomes = review_create_batch(
            executor,
            object(),
            [create_spec("c1"), create_spec("c2")],
        )
        self.assertEqual(outcomes, [{"decision": "ACCEPT"}, {"decision": "NO_CHANGE"}])
        self.assertEqual(len(executor.calls), 2)
        self.assertTrue(all("_BATCH\n" not in call["prompt"] for call in executor.calls))

    def test_update_batch_keeps_review_id_mapping_and_revision_parser(self):
        revised = dict(update_spec("u2")["proposed_summary"], body="Revised u2.")
        raw = json.dumps({
            "reviews": [
                {"review_id": "u2", "decision": "REVISE", "summary": revised},
                {"review_id": "u1", "decision": "ACCEPT"},
            ]
        })
        executor = ScriptedExecutor(raw)
        outcomes = review_update_batch(
            executor,
            _BATCH_BACKEND,
            [update_spec("u1"), update_spec("u2")],
        )
        self.assertEqual(outcomes[0], {"decision": "ACCEPT"})
        self.assertEqual(outcomes[1]["decision"], "REVISE")
        self.assertEqual(outcomes[1]["summary"]["body"], "Revised u2.")
        self.assertEqual(len(executor.calls), 1)
        self.assertTrue(executor.calls[0]["prompt"].startswith("UPDATE_SEMANTIC_REVIEW_BATCH\n"))


if __name__ == "__main__":
    unittest.main()

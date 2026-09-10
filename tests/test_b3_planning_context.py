from __future__ import annotations

import unittest
from types import SimpleNamespace

from memleaf.planning_context import PlanningContext
from memleaf.process_common import _RELATED_MAX_BODY_CHARS, _RELATED_MAX_ITEMS


def local(memory_id: str, *, body: str = "state") -> dict:
    return {
        "memory_id": memory_id,
        "title": memory_id,
        "body": body,
        "type": "fact",
        "scopes": ["global"],
    }


class FakeContext(PlanningContext):
    def __init__(self, result):
        self.result = result
        self.kwargs = None

    def _related_query(self, *args, **kwargs):
        self.kwargs = kwargs
        return self.result


class B3PlanningContextTests(unittest.TestCase):
    def test_small_related_projection_is_complete(self):
        selected, complete = PlanningContext._bound_related_with_status(
            [local("m1"), local("m2")]
        )
        self.assertTrue(complete)
        self.assertEqual([row["memory_id"] for row in selected], ["m1", "m2"])

    def test_item_limit_marks_projection_incomplete(self):
        selected, complete = PlanningContext._bound_related_with_status(
            [local(f"m{i}") for i in range(_RELATED_MAX_ITEMS + 1)]
        )
        self.assertFalse(complete)
        self.assertEqual(len(selected), _RELATED_MAX_ITEMS)

    def test_body_truncation_marks_projection_incomplete(self):
        selected, complete = PlanningContext._bound_related_with_status(
            [local("m1", body="x" * (_RELATED_MAX_BODY_CHARS + 20))]
        )
        self.assertFalse(complete)
        self.assertTrue(selected[0]["body"].endswith("…"))

    def test_scope_correction_prefetch_is_noop_without_explicit_marker(self):
        turn = SimpleNamespace(events=[SimpleNamespace(role="user", content="Alpha ordinary update")])
        context = object.__new__(PlanningContext)
        rows, complete = context._single_pass_scope_correction_context(turn)
        self.assertEqual(rows, [])
        self.assertTrue(complete)

    def test_single_pass_create_requires_complete_unambiguous_context(self):
        turn = SimpleNamespace(events=[SimpleNamespace(role="user", content="Alpha changed")])
        complete = FakeContext(([], ["project:Alpha"], [], None, True))
        result = complete._single_pass_related(turn, {}, physical_units=())
        self.assertTrue(result[-1])
        self.assertTrue(complete.kwargs["return_bound_status"])

        truncated = FakeContext(([], ["project:Alpha"], [], None, False))
        self.assertFalse(truncated._single_pass_related(turn, {}, physical_units=())[-1])

        ambiguous = FakeContext(([], ["project:Alpha"], [], ([object(), object()], True), True))
        self.assertFalse(ambiguous._single_pass_related(turn, {}, physical_units=())[-1])


if __name__ == "__main__":
    unittest.main()

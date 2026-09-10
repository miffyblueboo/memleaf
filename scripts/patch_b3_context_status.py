from __future__ import annotations

from pathlib import Path


ROOT = Path.cwd()
path = ROOT / "src/memleaf/planning_context.py"
text = path.read_text(encoding="utf-8")

start = text.index("    def _related_query(")
body = text.index("        if isinstance(query, str):", start)
new_header = '''    def _related_query(
        self,
        turn: InboxTurn,
        state: Mapping[str, Any],
        query: str | Iterable[str],
        explicit_scope: Any = None,
        *,
        overlay: Iterable[Mapping[str, Any]] = (),
        strict_relevance: bool = False,
        priority_memory_ids: Iterable[str] = (),
        priority_only: bool = False,
        scope_records: Optional[list[Any]] = None,
        native_query: Optional[str] = None,
        return_bound_status: bool = False,
    ) -> Any:
'''
text = text[:start] + new_header + text[body:]

old_bound_call = '''        related = self._bound_related(
            related,
            priority_memory_ids=priority_memory_ids,
        )
'''
new_bound_call = '''        related, bound_complete = self._bound_related_with_status(
            related,
            priority_memory_ids=priority_memory_ids,
        )
'''
if text.count(old_bound_call) != 1:
    raise SystemExit(f"expected one related bound call, found {text.count(old_bound_call)}")
text = text.replace(old_bound_call, new_bound_call, 1)

old_return = "        return related, scope, native_refs, scope_fallback\n"
new_return = '''        if return_bound_status:
            return related, scope, native_refs, scope_fallback, bound_complete
        return related, scope, native_refs, scope_fallback
'''
if text.count(old_return) != 1:
    raise SystemExit(f"expected one related query return, found {text.count(old_return)}")
text = text.replace(old_return, new_return, 1)

bound_start = text.index("    @classmethod\n    def _bound_related(")
bound_end = text.index("\n\n    def _scope_records_unlocked", bound_start)
new_bound = '''    @classmethod
    def _bound_related_with_status(
        cls,
        related: Iterable[Mapping[str, Any]],
        *,
        priority_memory_ids: Iterable[str] = (),
    ) -> tuple[list[dict[str, Any]], bool]:
        """Return the legacy bounded projection plus whether it stayed complete.

        B3 needs a proof that local comparison context was not dropped before it
        may treat an absent target as CREATE-safe.  Legacy callers keep using
        ``_bound_related`` and therefore retain the exact list-only API.
        """

        priority = {
            value.casefold()
            for value in priority_memory_ids
            if isinstance(value, str) and value
        }
        values: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for item in related:
            if not isinstance(item, Mapping):
                continue
            value = dict(item)
            memory_id = value.get("memory_id")
            if isinstance(memory_id, str):
                key = memory_id.casefold()
                if key in seen_ids:
                    continue
                seen_ids.add(key)
            values.append(value)
        values.sort(
            key=lambda value: (
                isinstance(value.get("memory_id"), str)
                and value["memory_id"].casefold() in priority,
            ),
            reverse=True,
        )
        selected: list[dict[str, Any]] = []
        used = 2
        complete = True
        for value in values:
            if len(selected) >= _RELATED_MAX_ITEMS:
                complete = False
                break
            body = value.get("body")
            if isinstance(body, str) and len(body) > _RELATED_MAX_BODY_CHARS:
                value["body"] = body[: _RELATED_MAX_BODY_CHARS - 1].rstrip() + "…"
                complete = False
            size = cls._related_payload_size(value)
            if size < 0:
                complete = False
                continue
            additional = size + (1 if selected else 0)
            if used + additional > _RELATED_MAX_CHARS:
                memory_id = value.get("memory_id")
                if not (
                    isinstance(memory_id, str)
                    and memory_id.casefold() in priority
                ):
                    complete = False
                    continue
                minimal = {
                    key: value[key]
                    for key in ("memory_id", "title", "body", "type", "scopes", "due_date")
                    if key in value
                }
                size = cls._related_payload_size(minimal)
                if size < 0 or used + size + (1 if selected else 0) > _RELATED_MAX_CHARS:
                    complete = False
                    continue
                value = minimal
                additional = size + (1 if selected else 0)
                complete = False
            selected.append(value)
            used += additional
        if len(selected) != len(values):
            complete = False
        return selected, complete

    @classmethod
    def _bound_related(
        cls,
        related: Iterable[Mapping[str, Any]],
        *,
        priority_memory_ids: Iterable[str] = (),
    ) -> list[dict[str, Any]]:
        """Keep the legacy model related-memory projection unchanged."""

        selected, _ = cls._bound_related_with_status(
            related,
            priority_memory_ids=priority_memory_ids,
        )
        return selected
'''
text = text[:bound_start] + new_bound + text[bound_end:]

insert_at = text.index("\n\n    @staticmethod\n    def _physical_query_texts", text.index("    def _related("))
single_pass_method = '''

    def _single_pass_related(
        self,
        turn: InboxTurn,
        state: Mapping[str, Any],
        explicit_scope: Any = None,
        *,
        overlay: Iterable[Mapping[str, Any]] = (),
        physical_units: Iterable[Any] = (),
    ) -> tuple[
        list[dict[str, Any]],
        Any,
        list[dict[str, str]],
        Optional[tuple[list[Any], bool]],
        bool,
    ]:
        """Read B3 comparison context before the single model call.

        ``create_allowed`` is true only when the bounded related projection is
        complete and a scoped fallback did not discover multiple ambiguous
        records. UPDATE/NO_CHANGE may still use returned local targets when
        CREATE is disabled.
        """

        visible = " ".join(
            event.content for event in turn.events if isinstance(event.content, str)
        ).strip()
        physical_units = tuple(physical_units or ())
        physical_queries = self._physical_query_texts(physical_units)
        scope = _safe_scope_background(state, explicit_scope)
        scoped_reply_context = self._has_specific_scope(scope) and any(
            getattr(unit, "can_support", False) is True
            and getattr(unit, "origin", None) == "assistant_report"
            for unit in physical_units
        )
        query: str | list[str] = (
            [visible, *physical_queries]
            if physical_queries and self._has_specific_scope(scope)
            else visible
        )
        related, scope_background, native_refs, scope_fallback, bound_complete = self._related_query(
            turn,
            state,
            query,
            explicit_scope,
            overlay=overlay,
            strict_relevance=not scoped_reply_context,
            native_query=visible,
            return_bound_status=True,
        )
        fallback_ambiguous = bool(
            scope_fallback is not None
            and len(scope_fallback) == 2
            and scope_fallback[1] is True
        )
        create_allowed = bool(bound_complete and not fallback_ambiguous)
        return related, scope_background, native_refs, scope_fallback, create_allowed
'''
text = text[:insert_at] + single_pass_method + text[insert_at:]
path.write_text(text, encoding="utf-8")


test_path = ROOT / "tests/test_b3_planning_context.py"
test_path.write_text(r'''from __future__ import annotations

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

    def test_single_pass_create_requires_complete_unambiguous_context(self):
        turn = SimpleNamespace(events=[SimpleNamespace(content="Alpha changed")])
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
''', encoding="utf-8")

workflow_path = ROOT / ".github/workflows/b3-single-pass.yml"
workflow = workflow_path.read_text(encoding="utf-8")
old_compile = "            tests/test_b3_single_pass_plan.py\n"
new_compile = "            tests/test_b3_single_pass_plan.py \\\n            tests/test_b3_planning_context.py\n"
if workflow.count(old_compile) != 1:
    raise SystemExit("B3 compile anchor missing")
workflow = workflow.replace(old_compile, new_compile, 1)
old_test = "run: python -m unittest tests.test_b3_single_pass_plan -v"
new_test = "run: python -m unittest tests.test_b3_single_pass_plan tests.test_b3_planning_context -v"
if workflow.count(old_test) != 1:
    raise SystemExit("B3 unittest anchor missing")
workflow = workflow.replace(old_test, new_test, 1)
workflow_path.write_text(workflow, encoding="utf-8")

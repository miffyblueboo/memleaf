from __future__ import annotations

from pathlib import Path


# 1) Semantic-review batching is opt-in at the backend boundary, matching the
# CREATE-summary batching safety contract.
path = Path("src/memleaf/batch_review.py")
source = path.read_text(encoding="utf-8")
anchor = "    specs = _validated_specs(items, update=update)\n    if len(specs) == 1:\n"
assert source.count(anchor) == 1
source = source.replace(
    anchor,
    "    specs = _validated_specs(items, update=update)\n"
    "    if getattr(backend, \"structured_batch_safe\", False) is not True:\n"
    "        return [_single(model_executor, backend, spec, update=update) for spec in specs]\n"
    "    if len(specs) == 1:\n",
    1,
)
path.write_text(source, encoding="utf-8")


# 2) Unit tests that intentionally exercise batching must opt into the same
# backend capability. Add one explicit legacy-backend fallback test.
path = Path("tests/test_batch_review.py")
source = path.read_text(encoding="utf-8")
class_anchor = "\n\nclass ScriptedExecutor:\n"
assert source.count(class_anchor) == 1
source = source.replace(
    class_anchor,
    "\n\nclass _BatchBackend:\n"
    "    structured_batch_safe = True\n\n\n"
    "_BATCH_BACKEND = _BatchBackend()\n"
    + class_anchor,
    1,
)
source = source.replace('executor, "backend",', 'executor, _BATCH_BACKEND,')
assert 'executor, "backend",' not in source
method_anchor = "    def test_update_batch_keeps_review_id_mapping_and_revision_parser(self):\n"
assert source.count(method_anchor) == 1
legacy_test = '''    def test_backend_without_batch_capability_uses_legacy_single_reviews(self):
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
        self.assertTrue(all("_BATCH\\n" not in call["prompt"] for call in executor.calls))

'''
source = source.replace(method_anchor, legacy_test + method_anchor, 1)
path.write_text(source, encoding="utf-8")


# 3) Coordinator integration: 5 reviews legitimately produce one four-item
# batch plus one legacy single. The fake executor must accept both shapes.
path = Path("tests/test_batch_review_integration.py")
source = path.read_text(encoding="utf-8")
executor_anchor = "\n\nclass BatchAcceptExecutor:\n"
assert source.count(executor_anchor) == 1
source = source.replace(
    executor_anchor,
    "\n\nclass _BatchBackend:\n"
    "    structured_batch_safe = True\n"
    "    parallel_safe = False\n"
    + executor_anchor,
    1,
)
old = '''        self.calls.append(prompt)
        if not prompt.startswith("CREATE_SEMANTIC_REVIEW_BATCH\\n"):
            raise AssertionError("integration path unexpectedly used a single review")
        payload = json.loads(prompt.split("\\n", 1)[1].split("\\n\\nReview each row", 1)[0])
        raw = json.dumps({
            "reviews": [
                {"review_id": row["review_id"], "decision": "ACCEPT"}
                for row in reversed(payload["reviews"])
            ]
        })
        return parser(raw)
'''
new = '''        self.calls.append(prompt)
        if prompt.startswith("CREATE_SEMANTIC_REVIEW_BATCH\\n"):
            payload = json.loads(prompt.split("\\n", 1)[1].split("\\n\\nReview each row", 1)[0])
            raw = json.dumps({
                "reviews": [
                    {"review_id": row["review_id"], "decision": "ACCEPT"}
                    for row in reversed(payload["reviews"])
                ]
            })
            return parser(raw)
        if prompt.startswith("CREATE_SEMANTIC_REVIEW\\n"):
            return parser(json.dumps({"decision": "ACCEPT"}))
        raise AssertionError("integration path used an unexpected review prompt")
'''
assert source.count(old) == 1
source = source.replace(old, new, 1)
assert source.count('backend="synthetic-backend",') == 1
source = source.replace('backend="synthetic-backend",', 'backend=_BatchBackend(),', 1)
path.write_text(source, encoding="utf-8")

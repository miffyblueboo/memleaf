from __future__ import annotations

import unittest

from benchmarks.p1.run_baseline import BudgetedModel
from memleaf.llm.router import ModelRouter


class Backend:
    parallel_safe = True
    structured_batch_safe = True
    provider = "test"
    model = "test"

    def __init__(self, *, single_pass_safe: bool):
        self.single_pass_safe = single_pass_safe

    def complete(self, prompt, *, system="", purpose="", temperature=0.0):
        return "{}"


class B3BenchmarkHarnessTests(unittest.TestCase):
    def test_budget_wrapper_preserves_single_pass_capability(self):
        wrapped = BudgetedModel(Backend(single_pass_safe=True), 3)
        self.assertTrue(wrapped.single_pass_safe)
        self.assertTrue(wrapped.structured_batch_safe)
        self.assertTrue(wrapped.parallel_safe)

    def test_budget_wrapper_does_not_invent_single_pass_capability(self):
        wrapped = BudgetedModel(Backend(single_pass_safe=False), 3)
        self.assertFalse(wrapped.single_pass_safe)

    def test_safe_api_router_remains_single_pass_safe_through_budget_wrapper(self):
        router = ModelRouter(mode="api", api=Backend(single_pass_safe=True))
        self.assertTrue(router.single_pass_safe)
        wrapped = BudgetedModel(router, 3)
        self.assertTrue(wrapped.single_pass_safe)


if __name__ == "__main__":
    unittest.main()

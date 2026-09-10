from __future__ import annotations

from pathlib import Path


def main() -> None:
    router = Path("src/memleaf/llm/router.py")
    source = router.read_text(encoding="utf-8")
    marker = "    @staticmethod\n    def _coerce_host(value: Any) -> Optional[ModelBackend]:\n"
    prop = (
        "    @property\n"
        "    def structured_batch_safe(self) -> bool:\n"
        "        \"\"\"Expose prompt-level batching only when routing is fixed to a safe API backend.\"\"\"\n\n"
        "        if self.mode == \"api\":\n"
        "            return getattr(self.api, \"structured_batch_safe\", False) is True\n"
        "        if self.mode == \"auto\" and self.host is None:\n"
        "            return getattr(self.api, \"structured_batch_safe\", False) is True\n"
        "        return False\n\n"
    )
    assert source.count(marker) == 1
    router.write_text(source.replace(marker, prop + marker, 1), encoding="utf-8")

    runner = Path("benchmarks/p1/run_baseline.py")
    source = runner.read_text(encoding="utf-8")
    marker = "    @property\n    def calls(self) -> int:\n"
    prop = (
        "    @property\n"
        "    def structured_batch_safe(self) -> bool:\n"
        "        return getattr(self.backend, \"structured_batch_safe\", False) is True\n\n"
    )
    assert source.count(marker) == 1
    runner.write_text(source.replace(marker, prop + marker, 1), encoding="utf-8")

    batch = Path("src/memleaf/summary_batch.py")
    source = batch.read_text(encoding="utf-8")
    old = (
        "BATCH MODE\n"
        "Apply the single-candidate Summary contract independently to every supplied\n"
        "item."
    )
    new = (
        "BATCH MODE\n"
        "This batch envelope replaces only the outer single-item return shape. Apply the\n"
        "single-candidate Summary contract independently to every supplied item."
    )
    assert source.count(old) == 1
    batch.write_text(source.replace(old, new, 1), encoding="utf-8")

    tests = Path("tests/test_p3_summary_batch.py")
    source = tests.read_text(encoding="utf-8")
    old_import = "from memleaf.summary_batch import run_summary_jobs_with_create_batching\n"
    new_import = (
        "from memleaf.llm.router import ModelRouter\n"
        "from memleaf.summary_batch import BATCH_SUMMARIZE_SYSTEM, run_summary_jobs_with_create_batching\n"
    )
    assert source.count(old_import) == 1
    source = source.replace(old_import, new_import, 1)
    marker = "    def test_backend_without_explicit_batch_capability_uses_original_single_calls(self):\n"
    new_test = (
        "    def test_router_exposes_batch_capability_only_for_fixed_safe_api_route(self):\n"
        "        api = _BatchBackend()\n"
        "        api.complete = lambda *args, **kwargs: \"{}\"\n"
        "        api.provider = \"synthetic-api\"\n"
        "        api.model = \"synthetic-model\"\n"
        "        api_router = ModelRouter(mode=\"api\", api=api)\n"
        "        self.assertTrue(api_router.structured_batch_safe)\n"
        "        auto_api_router = ModelRouter(mode=\"auto\", api=api)\n"
        "        self.assertTrue(auto_api_router.structured_batch_safe)\n"
        "        host = _LegacyBackend()\n"
        "        host.complete = lambda *args, **kwargs: \"{}\"\n"
        "        host.provider = \"host\"\n"
        "        host.model = \"host-model\"\n"
        "        auto_host_router = ModelRouter(mode=\"auto\", host=host, api=api)\n"
        "        self.assertFalse(auto_host_router.structured_batch_safe)\n"
        "        self.assertIn(\"replaces only the outer single-item return shape\", BATCH_SUMMARIZE_SYSTEM)\n\n"
    )
    assert source.count(marker) == 1
    tests.write_text(source.replace(marker, new_test + marker, 1), encoding="utf-8")


if __name__ == "__main__":
    main()

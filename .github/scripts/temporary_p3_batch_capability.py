from __future__ import annotations

from pathlib import Path


def main() -> None:
    base = Path("src/memleaf/llm/base.py")
    source = base.read_text(encoding="utf-8")
    old_protocol = "    parallel_safe: bool\n\n    def complete("
    new_protocol = "    parallel_safe: bool\n    structured_batch_safe: bool\n\n    def complete("
    assert source.count(old_protocol) == 1
    source = source.replace(old_protocol, new_protocol, 1)
    old_callable = "    parallel_safe = False\n\n    def __init__(self, callback:"
    new_callable = "    parallel_safe = False\n    structured_batch_safe = False\n\n    def __init__(self, callback:"
    assert source.count(old_callable) == 1
    source = source.replace(old_callable, new_callable, 1)
    old_http = '    provider = "api"\n    parallel_safe = False\n\n    def __init__('
    new_http = '    provider = "api"\n    parallel_safe = False\n    structured_batch_safe = True\n\n    def __init__('
    assert source.count(old_http) == 1
    source = source.replace(old_http, new_http, 1)
    base.write_text(source, encoding="utf-8")

    helper = Path("src/memleaf/summary_batch.py")
    source = helper.read_text(encoding="utf-8")
    marker = "    if not jobs:\n        return []\n\n    batchable_indexes = ["
    replacement = (
        "    if not jobs:\n"
        "        return []\n"
        "    if getattr(backend, \"structured_batch_safe\", False) is not True:\n"
        "        return run_ordered_keyed_jobs(\n"
        "            model_executor,\n"
        "            backend,\n"
        "            [(str(job[\"key\"]), job[\"call\"]) for job in jobs],\n"
        "        )\n\n"
        "    batchable_indexes = ["
    )
    assert source.count(marker) == 1
    helper.write_text(source.replace(marker, replacement, 1), encoding="utf-8")

    tests = Path("tests/test_p3_summary_batch.py")
    source = tests.read_text(encoding="utf-8")
    marker = "class _FakeExecutor:\n"
    backend = (
        "class _BatchBackend:\n"
        "    structured_batch_safe = True\n"
        "    parallel_safe = True\n\n\n"
        "class _LegacyBackend:\n"
        "    parallel_safe = True\n\n\n"
    )
    assert source.count(marker) == 1
    source = source.replace(marker, backend + marker, 1)
    old_call = "run_summary_jobs_with_create_batching(executor, object(), jobs)"
    assert source.count(old_call) == 4
    source = source.replace(
        old_call,
        "run_summary_jobs_with_create_batching(executor, _BatchBackend(), jobs)",
    )
    test_marker = "    def test_non_batch_update_jobs_keep_same_key_order(self):\n"
    new_test = (
        "    def test_backend_without_explicit_batch_capability_uses_original_single_calls(self):\n"
        "        singles: list[str] = []\n"
        "        executor = _FakeExecutor([])\n"
        "        jobs = [\n"
        "            _job(\"c1\", title=\"one\", batchable=True, singles=singles),\n"
        "            _job(\"c2\", title=\"two\", batchable=True, singles=singles),\n"
        "        ]\n"
        "        result = run_summary_jobs_with_create_batching(executor, _LegacyBackend(), jobs)\n"
        "        self.assertEqual(singles, [\"c1\", \"c2\"])\n"
        "        self.assertEqual(executor.calls, [])\n"
        "        self.assertTrue(all(item[\"summary\"][\"single\"] for item in result))\n\n"
    )
    assert source.count(test_marker) == 1
    source = source.replace(test_marker, new_test + test_marker, 1)
    tests.write_text(source, encoding="utf-8")


if __name__ == "__main__":
    main()

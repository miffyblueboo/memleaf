"""Bounded scheduling for independent model work.

This module owns no Vault state. Callers prepare immutable jobs first, then
apply all audit, request and commit side effects on their original thread after
results return.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, TypeVar


_T = TypeVar("_T")


def _worker_limit(model_executor: Any, backend: Any, count: int) -> int:
    if count <= 1:
        return max(0, count)
    max_parallel = getattr(model_executor, "max_parallel_calls", None)
    if not callable(max_parallel):
        return 1
    try:
        value = max_parallel(backend)
    except Exception:
        return 1
    if isinstance(value, bool) or not isinstance(value, int):
        return 1
    return max(1, min(count, value))


def run_ordered_jobs(
    model_executor: Any,
    backend: Any,
    jobs: list[Callable[[], _T]],
) -> list[_T]:
    """Run independent jobs with bounded parallelism and original result order."""

    if not jobs:
        return []
    workers = _worker_limit(model_executor, backend, len(jobs))
    if workers <= 1:
        return [job() for job in jobs]
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="memleaf-model") as pool:
        futures = [pool.submit(job) for job in jobs]
        return [future.result() for future in futures]


def run_ordered_keyed_jobs(
    model_executor: Any,
    backend: Any,
    jobs: list[tuple[str, Callable[[], _T]]],
) -> list[_T]:
    """Parallelize independent keys while keeping each key strictly ordered.

    A caller should use one stable key for every operation that targets the same
    active memory. Targetless independent CREATE proposals should use distinct
    keys. Results always map one-for-one to the input job order.
    """

    if not jobs:
        return []
    workers = _worker_limit(model_executor, backend, len(jobs))
    if workers <= 1:
        return [job() for _key, job in jobs]

    groups: dict[str, list[tuple[int, Callable[[], _T]]]] = {}
    for index, (key, job) in enumerate(jobs):
        groups.setdefault(key, []).append((index, job))
    if len(groups) <= 1:
        return [job() for _key, job in jobs]

    results: list[Any] = [None] * len(jobs)

    def run_group(items: list[tuple[int, Callable[[], _T]]]) -> list[tuple[int, _T]]:
        return [(index, job()) for index, job in items]

    with ThreadPoolExecutor(
        max_workers=min(workers, len(groups)),
        thread_name_prefix="memleaf-model",
    ) as pool:
        futures = [pool.submit(run_group, items) for items in groups.values()]
        for future in futures:
            for index, value in future.result():
                results[index] = value
    return results


__all__ = ["run_ordered_jobs", "run_ordered_keyed_jobs"]

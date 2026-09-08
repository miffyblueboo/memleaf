from __future__ import annotations

import json
import threading
import time

from memleaf.model_execution import ModelExecutor
from memleaf.process_jobs import _safe_result
from memleaf.update_coordinator import UpdateCoordinator
from memleaf.update_review import CREATE_SEMANTIC_REVIEW_SYSTEM, UPDATE_SEMANTIC_REVIEW_SYSTEM
from memleaf.validation import ModelOutputError


class _VaultStub:
    def __init__(self, concurrency: int = 3):
        self._concurrency = concurrency

    def config(self):
        return {
            "process": {"model_concurrency": self._concurrency},
            "llm": {"diagnostic_logging": False},
        }


class _ServiceStub:
    def __init__(self, concurrency: int = 3):
        self.vault = _VaultStub(concurrency)


class _ParallelBackend:
    parallel_safe = True

    def __init__(self, outputs: list[str] | None = None):
        self.outputs = list(outputs or ['{"ok":true}'])
        self._lock = threading.Lock()

    def complete(self, prompt, *, system="", purpose="", temperature=0.0):
        with self._lock:
            if len(self.outputs) > 1:
                return self.outputs.pop(0)
            return self.outputs[0]


class _UnsafeBackend:
    parallel_safe = False


def test_model_metrics_count_retry_lengths_and_never_include_text():
    executor = ModelExecutor(_ServiceStub())
    backend = _ParallelBackend(["bad", '{"ok":true}'])

    def parse(raw: str):
        if raw == "bad":
            raise ModelOutputError("invalid", validation_detail="root_shape")
        return json.loads(raw)

    result = executor._complete_json_stage(
        backend,
        "TOP-SECRET-PROMPT",
        system="TOP-SECRET-SYSTEM",
        purpose="summarize",
        metric_stage="semantic_review",
        parser=parse,
    )
    assert result == {"ok": True}
    metrics = executor.metrics()
    stage = metrics["stages"]["semantic_review"]
    assert stage["call_count"] == 2
    assert stage["retry_count"] == 1
    assert stage["failed_calls"] == 0
    assert stage["input_chars"] > 0
    assert stage["input_bytes"] >= stage["input_chars"]
    assert stage["output_chars"] == len("bad") + len('{"ok":true}')
    serialized = json.dumps(metrics, ensure_ascii=False)
    assert "TOP-SECRET-PROMPT" not in serialized
    assert "TOP-SECRET-SYSTEM" not in serialized
    assert "bad" not in serialized


def _concurrency_jobs(count: int, active: dict[str, int], lock: threading.Lock):
    jobs = []
    for index in range(count):
        def job(value=index):
            with lock:
                active["current"] += 1
                active["peak"] = max(active["peak"], active["current"])
            try:
                time.sleep(0.04)
                return {"decision": "ACCEPT", "value": value}
            finally:
                with lock:
                    active["current"] -= 1
        jobs.append(job)
    return jobs


def test_review_scheduler_uses_configured_limit_and_preserves_result_order():
    executor = ModelExecutor(_ServiceStub(concurrency=3))
    coordinator = UpdateCoordinator(executor, audit=None, read_target=lambda _memory_id: None)
    active = {"current": 0, "peak": 0}
    lock = threading.Lock()
    outcomes = coordinator._run_review_jobs(
        _concurrency_jobs(7, active, lock),
        backend=_ParallelBackend(),
    )
    assert active["peak"] == 3
    assert [item["value"] for item in outcomes] == list(range(7))


def test_review_scheduler_keeps_unsafe_backend_serial():
    executor = ModelExecutor(_ServiceStub(concurrency=3))
    coordinator = UpdateCoordinator(executor, audit=None, read_target=lambda _memory_id: None)
    active = {"current": 0, "peak": 0}
    lock = threading.Lock()
    coordinator._run_review_jobs(
        _concurrency_jobs(4, active, lock),
        backend=_UnsafeBackend(),
    )
    assert active["peak"] == 1


def test_background_result_projects_only_structural_model_metrics():
    result = _safe_result({
        "processed_turns": 1,
        "model_metrics": {
            "total": {
                "call_count": 4,
                "retry_count": 1,
                "request_duration_ms": 1234,
                "wall_clock_ms": 800,
                "input_chars": 100,
                "input_bytes": 120,
                "output_chars": 40,
                "output_bytes": 45,
                "failed_calls": 0,
                "max_in_flight": 3,
                "prompt": "DO-NOT-PERSIST",
            },
            "stages": {
                "semantic_review": {"call_count": 3, "max_in_flight": 3},
                "evil-stage": {"call_count": 99, "body": "DO-NOT-PERSIST"},
            },
            "response": "DO-NOT-PERSIST",
        },
    })
    assert result["model_metrics"]["total"]["call_count"] == 4
    assert result["model_metrics"]["total"]["max_in_flight"] == 3
    assert result["model_metrics"]["stages"] == {
        "semantic_review": {"call_count": 3, "max_in_flight": 3}
    }
    serialized = json.dumps(result, ensure_ascii=False)
    assert "DO-NOT-PERSIST" not in serialized
    assert "evil-stage" not in serialized


def test_semantic_review_contract_requires_completeness_not_only_non_invention():
    for system in (CREATE_SEMANTIC_REVIEW_SYSTEM, UPDATE_SEMANTIC_REVIEW_SYSTEM):
        assert "Semantic completeness is as important as non-invention" in system
        assert "meaning-defining" in system
        assert "business/workstream/background context" in system
        assert "Scope metadata alone does not substitute for a named subject" in system
        assert "implementation context" in system
        assert "Never add an owner" in system

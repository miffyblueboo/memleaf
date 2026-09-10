from __future__ import annotations

from pathlib import Path


def replace_exact(path: str, old: str, new: str) -> None:
    file = Path(path)
    source = file.read_text(encoding="utf-8")
    assert source.count(old) == 1, (path, source.count(old))
    file.write_text(source.replace(old, new, 1), encoding="utf-8")


# Batch-format/model-output failures may safely degrade to legacy singles.
# Transport/provider ModelError has already exhausted the normal model-call
# policy and must propagate instead of amplifying one failed batch into N calls.
replace_exact(
    "src/memleaf/batch_review.py",
    "    except (ModelError, ModelOutputError, TypeError, ValueError):\n"
    "        return [_single(model_executor, backend, spec, update=update) for spec in specs]\n",
    "    except (ModelOutputError, TypeError, ValueError):\n"
    "        return [_single(model_executor, backend, spec, update=update) for spec in specs]\n",
)
replace_exact(
    "src/memleaf/summary_batch.py",
    "    except (ModelError, ModelOutputError, TypeError, ValueError):\n"
    "        # A malformed/failed whole envelope must not strand either candidate.\n"
    "        return [(int(item[\"index\"]), item[\"call\"]()) for item in items]\n",
    "    except (ModelOutputError, TypeError, ValueError):\n"
    "        # A malformed whole envelope falls back to legacy singles. Transport/provider\n"
    "        # ModelError propagates so one failed batch cannot fan out into N new calls.\n"
    "        return [(int(item[\"index\"]), item[\"call\"]()) for item in items]\n",
)

# Permanent semantic-review regression.
path = Path("tests/test_batch_review.py")
source = path.read_text(encoding="utf-8")
assert "from memleaf.llm import ModelError\n" not in source
source = source.replace(
    "from memleaf.batch_review import review_create_batch, review_update_batch\n",
    "from memleaf.batch_review import review_create_batch, review_update_batch\n"
    "from memleaf.llm import ModelError\n",
    1,
)
marker = "    def test_backend_without_batch_capability_uses_legacy_single_reviews(self):\n"
assert source.count(marker) == 1
case = '''    def test_batch_transport_failure_propagates_without_single_fanout(self):
        class FailingExecutor(ScriptedExecutor):
            def _complete_json_stage(self, *args, **kwargs):
                self.calls.append({"prompt": args[1], "system": kwargs.get("system")})
                raise ModelError("network failed", code="model_network_error", stage="summarize")

        executor = FailingExecutor("unused")
        with self.assertRaises(ModelError):
            review_create_batch(
                executor,
                _BATCH_BACKEND,
                [create_spec("c1"), create_spec("c2"), create_spec("c3")],
            )
        self.assertEqual(len(executor.calls), 1)

'''
source = source.replace(marker, case + marker, 1)
path.write_text(source, encoding="utf-8")

# Permanent CREATE-summary regression.
path = Path("tests/test_p3_summary_batch.py")
source = path.read_text(encoding="utf-8")
if "from memleaf.llm import ModelError" not in source:
    anchor = "from memleaf.llm.router import ModelRouter\n"
    assert source.count(anchor) == 1
    source = source.replace(anchor, "from memleaf.llm import ModelError\n" + anchor, 1)
marker = "    def test_router_exposes_batch_capability_only_for_fixed_safe_api_route(self):\n"
assert source.count(marker) == 1
case = '''    def test_summary_batch_transport_failure_propagates_without_single_fanout(self):
        class FailingExecutor:
            def __init__(self):
                self.calls = 0

            def max_parallel_calls(self, _backend):
                return 1

            def _complete_json_stage(self, *args, **kwargs):
                self.calls += 1
                raise ModelError("timeout", code="model_timeout", stage="summarize")

        singles = []
        jobs = []
        for index in range(2):
            def single(index_value=index):
                singles.append(index_value)
                return {"status": "ok", "summary": {"index": index_value}}
            jobs.append({
                "key": f"create:{index}",
                "call": single,
                "batchable": True,
                "item_id": f"c{index}",
                "prompt": f"prompt-{index}",
                "parser": lambda raw: {"parsed": raw},
                "diagnostic_context": {},
            })

        executor = FailingExecutor()
        with self.assertRaises(ModelError):
            run_summary_jobs_with_create_batching(executor, _BatchBackend(), jobs)
        self.assertEqual(executor.calls, 1)
        self.assertEqual(singles, [])

'''
source = source.replace(marker, case + marker, 1)
path.write_text(source, encoding="utf-8")

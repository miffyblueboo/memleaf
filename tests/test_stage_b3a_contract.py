import copy
import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from memleaf import Memleaf
from memleaf.compaction import (
    CompactionError,
    Compactor,
    estimate_active_tokens,
    estimate_memory_tokens,
)
from memleaf.config import save_config
from memleaf.models import Memory
from memleaf.validation import ModelOutputError, parse_compact_output


class ProbeBackend:
    provider = "fake"
    model = "b3a-contract"

    def __init__(self, response):
        self.response = response
        self.calls = []

    def complete(self, prompt, *, system="", purpose="", temperature=0.0):
        self.calls.append({"prompt": prompt, "system": system, "purpose": purpose})
        if callable(self.response):
            return self.response(prompt)
        return self.response


class StageB3AContractTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.service = Memleaf(Path(self.tempdir.name) / "vault")

    def tearDown(self):
        self.tempdir.cleanup()

    def set_process(self, *, threshold=None, ratio=None):
        config = self.service.vault.config()
        if threshold is not None:
            config["process"]["memory_compact_threshold_tokens"] = threshold
        if ratio is not None:
            config["process"]["memory_compact_candidate_ratio"] = ratio
        save_config(self.service.vault.config_path, config)

    def add_memory(
        self,
        memory_id,
        body,
        *,
        hit_count=0,
        last_hit_at=None,
        area="knowledge",
        sources=None,
    ):
        return self.service.create_memory(
            memory_id=memory_id,
            title=memory_id,
            body=body,
            tags=["contract"],
            type="fact",
            scopes=["global"],
            scope_source="model",
            hit_count=hit_count,
            last_hit_at=last_hit_at,
            sources=[] if sources is None else sources,
            area=area,
        )

    @staticmethod
    def compact_item(source_ids, **overrides):
        item = {
            "title": "Merged fact",
            "body": "one smaller fact",
            "tags": ["merged"],
            "type": "fact",
            "scopes": ["global"],
            "scope_source": "model",
            "aliases": [],
            "keywords": [],
            "source_memory_ids": list(source_ids),
        }
        item.update(overrides)
        return item

    def test_estimate_and_stats_are_stable_and_history_is_excluded(self):
        active = self.add_memory("active", "short active")
        self.add_memory("history", "H" * 10000, area="history")
        expected = estimate_memory_tokens(active)
        self.assertGreaterEqual(expected, 1)
        self.assertEqual(expected, estimate_memory_tokens(active))
        self.assertEqual(expected, estimate_active_tokens([active]))

        self.set_process(threshold=expected + 1)
        stats = self.service.stats()
        self.assertEqual(stats["active_tokens_estimate"], expected)
        self.assertEqual(stats["threshold"], expected + 1)
        self.assertEqual(stats["compaction_threshold_tokens"], expected + 1)
        self.assertFalse(stats["compaction_due"])

        backend = ProbeBackend('{"memories": []}')
        result = self.service.compact(model=backend)
        self.assertEqual(result["status"], "not_due")
        self.assertEqual(backend.calls, [])
        self.assertEqual(result["active_tokens_before"], expected)

    def test_candidate_order_and_ceiling_ratio(self):
        self.add_memory("m-never-low", "never low", hit_count=0)
        self.add_memory("m-never-high", "never high", hit_count=2)
        self.add_memory(
            "m-old",
            "old hit",
            hit_count=0,
            last_hit_at="2020-01-01T00:00:00Z",
        )
        self.add_memory(
            "m-recent",
            "recent hit",
            hit_count=0,
            last_hit_at="2026-01-01T00:00:00Z",
        )
        self.set_process(threshold=1, ratio=0.30)
        backend = ProbeBackend('{"memories": []}')

        result = self.service.compact(model=backend)

        self.assertEqual(result["status"], "noop")
        self.assertEqual(result["candidates"], ["m-never-low", "m-never-high"])
        self.assertEqual(len(result["candidates"]), 2)  # ceil(0.30 * 4)
        self.assertEqual(len(backend.calls), 1)

    def test_strict_compact_contract_rejects_invalid_outputs(self):
        valid = self.compact_item(["m1"])
        self.assertEqual(parse_compact_output('{"memories": []}', ["m1"]), {"memories": []})

        invalid_values = []
        unknown_top = {"memories": [], "extra": True}
        invalid_values.append(json.dumps(unknown_top))
        missing = dict(valid)
        del missing["keywords"]
        invalid_values.append(json.dumps({"memories": [missing]}))
        unknown_replacement = dict(valid, unexpected="x")
        invalid_values.append(json.dumps({"memories": [unknown_replacement]}))
        invalid_values.append(json.dumps({"memories": [dict(valid, type="not-a-type")]}))
        invalid_values.append(json.dumps({"memories": [dict(valid, scopes=["project:"])]}))
        invalid_values.append(json.dumps({"memories": [dict(valid, scope_source="untrusted")]}))
        invalid_values.append(json.dumps({"memories": [dict(valid, scopes=["global", "unscoped"], scope_source="model")]}))
        invalid_values.append(json.dumps({"memories": [dict(valid, source_memory_ids=[])]}))
        invalid_values.append(json.dumps({"memories": [dict(valid, source_memory_ids=["m1", "m1"])]}))
        invalid_values.append(json.dumps({"memories": [dict(valid, source_memory_ids=["outside"])]}))
        invalid_values.append(json.dumps({"memories": [dict(valid, type="fact", status="active")]}))
        invalid_values.append(json.dumps({"memories": [dict(valid, type="todo", status="completed")] }))
        second = self.compact_item(["m1"])
        invalid_values.append(json.dumps({"memories": [valid, second]}))
        invalid_values.extend(
            [
                "```json\n{\"memories\": []}\n```",
                '{"memories": []} trailing',
                '{"memories": [], "memories": []}',
            ]
        )

        for raw in invalid_values:
            with self.subTest(raw=raw):
                with self.assertRaises(ModelOutputError):
                    parse_compact_output(raw, ["m1"])

    def test_compact_sources_do_not_fabricate_readme_fields(self):
        first = Memory.new(memory_id="m1", title="one", body="one", sources=[])
        second = Memory.new(
            memory_id="m2",
            title="two",
            body="two",
            sources=[{"event_key": "e1", "session_id": "s1"}],
        )

        merged = Compactor._merge_sources([first, second, second])

        self.assertEqual(merged, [{"event_key": "e1", "session_id": "s1"}])
        self.assertFalse(any("memory_id" in item or "reason" in item for item in merged))

    def test_non_reducing_replacement_is_explicit_error_and_zero_write(self):
        first = self.add_memory("m1", "a")
        second = self.add_memory("m2", "b")
        self.set_process(threshold=1, ratio=1.0)
        response = json.dumps(
            {
                "memories": [
                    self.compact_item(
                        [first.memory_id, second.memory_id],
                        body="z" * 1000,
                    )
                ]
            }
        )
        backend = ProbeBackend(response)
        before_knowledge = {
            path.name: path.read_text(encoding="utf-8")
            for path in self.service.vault.list_markdown("knowledge")
        }

        with self.assertRaises(CompactionError):
            self.service.compact(model=backend)

        after_knowledge = {
            path.name: path.read_text(encoding="utf-8")
            for path in self.service.vault.list_markdown("knowledge")
        }
        self.assertEqual(after_knowledge, before_knowledge)
        self.assertEqual(self.service.vault.list_markdown("history"), [])

    def test_compact_prompt_only_contains_selected_active_candidates(self):
        self.add_memory("selected", "SELECTED_ACTIVE_BODY")
        self.add_memory(
            "unselected",
            "UNSELECTED_ACTIVE_BODY",
            last_hit_at="2026-01-01T00:00:00Z",
            hit_count=5,
        )
        self.add_memory("history-secret", "HISTORY_SECRET_BODY", area="history")
        self.set_process(threshold=1, ratio=0.5)
        backend = ProbeBackend('{"memories": []}')

        result = self.service.compact(model=backend)

        self.assertEqual(result["status"], "noop")
        self.assertEqual(result["candidates"], ["selected"])
        prompt = backend.calls[0]["prompt"]
        self.assertIn("SELECTED_ACTIVE_BODY", prompt)
        self.assertNotIn("UNSELECTED_ACTIVE_BODY", prompt)
        self.assertNotIn("HISTORY_SECRET_BODY", prompt)

    def test_model_callback_can_take_vault_lock_without_deadlock(self):
        self.add_memory("selected", "selected")
        self.set_process(threshold=1, ratio=1.0)
        service = self.service
        finished = threading.Event()

        def callback(prompt):
            def lock_probe():
                with service.vault.lock():
                    pass
                finished.set()

            thread = threading.Thread(target=lock_probe, daemon=True)
            thread.start()
            self.assertTrue(finished.wait(1.0), "model callback could not acquire vault lock")
            thread.join(1.0)
            self.assertFalse(thread.is_alive(), "lock probe did not finish")
            return '{"memories": []}'

        result = service.compact(model=callback)
        self.assertEqual(result["status"], "noop")

    def test_compact_config_rejects_invalid_threshold_and_ratio(self):
        valid_config = copy.deepcopy(self.service.vault.config())
        invalid_process_values = [
            {"memory_compact_threshold_tokens": True},
            {"memory_compact_threshold_tokens": 0},
            {"memory_compact_threshold_tokens": -1},
            {"memory_compact_candidate_ratio": True},
            {"memory_compact_candidate_ratio": 0},
            {"memory_compact_candidate_ratio": -0.1},
            {"memory_compact_candidate_ratio": 1.1},
            {"memory_compact_candidate_ratio": float("nan")},
            {"memory_compact_candidate_ratio": float("inf")},
        ]
        for overrides in invalid_process_values:
            with self.subTest(overrides=overrides):
                config = copy.deepcopy(self.service.vault.config())
                config["process"].update(overrides)
                save_config(self.service.vault.config_path, config)
                with self.assertRaises(ValueError):
                    self.service.vault.config()

                # Restore a valid config before the next subtest.
                save_config(self.service.vault.config_path, valid_config)

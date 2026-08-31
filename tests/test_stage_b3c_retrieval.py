import json
import tempfile
import unittest
from pathlib import Path

from memleaf import Memleaf
from memleaf.config import save_config
from memleaf.index import extract_wikilinks
from memleaf.native_index import NativeIndexer


class QueueBackend:
    provider = "fake"
    model = "b3c-test"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, prompt, *, system="", purpose="", temperature=0.0):
        self.calls.append({"prompt": prompt, "purpose": purpose})
        if not self.responses:
            raise AssertionError("fake backend queue exhausted")
        return self.responses.pop(0)


class StageB3CRetrievalTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.service = Memleaf(self.root / "vault")

    def tearDown(self):
        self.tempdir.cleanup()

    def _set_native(self, sources):
        config = self.service.vault.config()
        config["native_sources"] = sources
        save_config(self.service.vault.config_path, config)
        self.service.refresh_native_sources()

    def test_native_search_is_unbounded_without_explicit_limit(self):
        source = self.root / "many.md"
        source.write_text(
            "".join(
                f"# Segment {index:02d}\nnative-many marker-{index:02d}\n"
                for index in range(40)
            ),
            encoding="utf-8",
        )
        self._set_native(
            {"many": {"agent": "hermes", "path": str(source), "share": True}}
        )

        indexer = NativeIndexer(self.service.vault)
        self.assertEqual(len(indexer.search("native-many")), 40)
        self.assertEqual(len(indexer.search("native-many", limit=37)), 37)

    def test_process_prompt_bounds_related_hits_and_excludes_unrelated_body(self):
        for index in range(10):
            self.service.create_memory(
                memory_id=f"local-{index:02d}",
                title=f"Local {index}",
                body=f"sharedneedle local-marker-{index:02d}",
                scopes=["global"],
            )
        self.service.create_memory(
            memory_id="local-unrelated",
            title="Unrelated local",
            body="unrelated-local-sentinel",
            scopes=["global"],
        )
        source = self.root / "related.md"
        source.write_text(
            "".join(
                f"# Native {index:02d}\nsharedneedle native-marker-{index:02d}\n"
                for index in range(10)
            )
            + "# Unrelated\nunrelated-native-sentinel\n",
            encoding="utf-8",
        )
        self._set_native(
            {"related": {"agent": "codex", "path": str(source), "share": False}}
        )

        self.service.capture("codex", "session", "turn", "user", "sharedneedle")
        self.service.capture("codex", "session", "turn", "assistant", "ack")
        backend = QueueBackend(['{"candidates": []}'])

        result = self.service.process(model=backend)

        self.assertEqual(result["processed_turns"], 1)
        self.assertEqual(len(backend.calls), 1)
        prompt = backend.calls[0]["prompt"]
        for index in range(4, 10):
            self.assertIn(f"local-marker-{index:02d}", prompt)
        for index in range(4):
            self.assertNotIn(f"local-marker-{index:02d}", prompt)
        for index in range(10):
            self.assertNotIn(f"native-marker-{index:02d}", prompt)
        self.assertNotIn("unrelated-local-sentinel", prompt)
        self.assertNotIn("unrelated-native-sentinel", prompt)

    def test_wikilinks_are_indexed_for_target_display_and_history_and_rebuilt(self):
        link = self.service.create_memory(
            memory_id="wikilink-active",
            title="Links",
            body=(
                "[[project:Orion|猎户座]] [[bug]] [[BUG]] [[猎户座]] "
                "[[|ignored]] [[../unsafe]] [[http://unsafe]] "
                "[[" + ("x" * 300) + "]]"
            ),
            scopes=["global"],
        )
        body_only = self.service.create_memory(
            memory_id="wikilink-body-only",
            title="Body only",
            body="bug appears only in body",
            scopes=["global"],
        )
        debugger = self.service.create_memory(
            memory_id="wikilink-debugger",
            title="Debugger",
            body="debugger appears only in body",
            scopes=["global"],
        )
        historical = self.service.create_memory(
            memory_id="wikilink-history",
            title="Historical link",
            body="[[history-target|历史显示]]",
            scopes=["global"],
            area="history",
        )

        self.assertEqual(
            extract_wikilinks(
                "[[project:Orion|猎户座]] [[bug]] [[BUG]] [[猎户座]] [[|ignored]]"
            ),
            ["project:orion", "猎户座", "bug"],
        )
        tags_path = self.service.vault.tags_index_path
        tags_index = json.loads(tags_path.read_text(encoding="utf-8"))
        self.assertEqual(tags_index["wikilinks"]["project:orion"], [link.memory_id])
        self.assertEqual(tags_index["wikilinks"]["猎户座"], [link.memory_id])
        self.assertEqual(tags_index["wikilinks"]["bug"], [link.memory_id])
        self.assertIn("wikilinks", tags_index["history"])
        self.assertEqual(
            [memory.memory_id for memory in self.service.search("Orion")],
            [link.memory_id],
        )
        self.assertEqual(
            [memory.memory_id for memory in self.service.search("猎户")],
            [link.memory_id],
        )
        self.assertEqual(
            [memory.memory_id for memory in self.service.search("bug")],
            [link.memory_id],
        )
        self.assertEqual(
            [memory.memory_id for memory in self.service.search("debugger")],
            [debugger.memory_id],
        )
        self.assertEqual(self.service.search("history-target"), [])
        self.assertEqual(
            [memory.memory_id for memory in self.service.search("历史显示", include_history=True)],
            [historical.memory_id],
        )
        self.assertNotIn(body_only.memory_id, {memory.memory_id for memory in self.service.search("bug")})

        link.body = "manually edited [[manual-target|手工显示]]"
        self.service.vault.memory_path(link.memory_id, "knowledge").write_text(
            link.to_markdown(), encoding="utf-8"
        )
        self.service.rebuild_index()
        self.assertEqual(
            [memory.memory_id for memory in self.service.search("manual-target")],
            [link.memory_id],
        )
        self.assertEqual(
            [memory.memory_id for memory in self.service.search("手工显示")],
            [link.memory_id],
        )

        old_index = json.loads(tags_path.read_text(encoding="utf-8"))
        old_index.pop("wikilinks", None)
        old_index["history"].pop("wikilinks", None)
        tags_path.write_text(json.dumps(old_index), encoding="utf-8")
        self.assertEqual(
            [memory.memory_id for memory in self.service.search("manual-target")],
            [link.memory_id],
        )
        rebuilt = json.loads(tags_path.read_text(encoding="utf-8"))
        self.assertIn("wikilinks", rebuilt)
        self.assertIn("wikilinks", rebuilt["history"])

    def test_first_layer_hit_excludes_fulltext_only_memory_and_fallback_still_works(self):
        indexed = self.service.create_memory(
            memory_id="indexed-bug",
            title="Indexed bug",
            body="an indexed note",
            tags=["bug"],
            scopes=["global"],
        )
        body_only = self.service.create_memory(
            memory_id="body-bug",
            title="Body bug",
            body="bug appears only in body",
            scopes=["global"],
        )
        fallback = self.service.create_memory(
            memory_id="fallback-term",
            title="Fallback",
            body="zzfallbacktoken",
            scopes=["global"],
        )

        self.assertEqual(
            [memory.memory_id for memory in self.service.search("bug")],
            [indexed.memory_id],
        )
        self.assertNotIn(
            body_only.memory_id,
            {memory.memory_id for memory in self.service.search("bug")},
        )
        self.assertEqual(
            [memory.memory_id for memory in self.service.search("zzfallbacktoken")],
            [fallback.memory_id],
        )

    def test_exact_identifier_beats_high_frequency_component_tag_and_keeps_history(self):
        identifier = "MEMLEAF-ISOLATION-20260827-01"
        for index in range(80):
            self.service.create_memory(
                memory_id=f"generic-{index:02d}",
                title=f"Generic memory {index:02d}",
                body="A common memleaf note.",
                tags=["memleaf"],
                hit_count=1000,
                scopes=["global"],
            )
        historical = self.service.create_memory(
            memory_id="history-identifier",
            title=f"{identifier} 负责人是甲",
            body=f"编号 {identifier} 的负责人是甲。",
            tags=["memleaf-isolation"],
            updated="2026-08-27T06:00:00Z",
            scopes=["global"],
            area="history",
        )
        current = self.service.create_memory(
            memory_id="active-identifier",
            title=f"{identifier} 负责人是乙",
            body=f"编号 {identifier} 的负责人是乙。",
            tags=["memleaf-isolation"],
            updated="2026-08-27T07:00:00Z",
            scopes=["global"],
        )

        active = self.service.search(identifier, limit=50)
        self.assertEqual([memory.memory_id for memory in active[:1]], [current.memory_id])
        self.assertNotIn(historical.memory_id, {memory.memory_id for memory in active})

        with_history = self.service.search(identifier, include_history=True, limit=50)
        self.assertEqual(
            [memory.memory_id for memory in with_history[:2]],
            [current.memory_id, historical.memory_id],
        )
        self.assertNotIn(
            historical.memory_id,
            {memory.memory_id for memory in self.service.search(identifier)},
        )

    def test_multiword_tag_query_still_prefers_memory_matching_both_terms(self):
        both = self.service.create_memory(
            memory_id="both-tags",
            title="Release database",
            body="database release is approved",
            tags=["database", "release"],
            scopes=["global"],
        )
        database = self.service.create_memory(
            memory_id="database-tag",
            title="Database policy",
            body="database policy",
            tags=["database"],
            scopes=["global"],
        )
        release = self.service.create_memory(
            memory_id="release-tag",
            title="Release policy",
            body="release policy",
            tags=["release"],
            scopes=["global"],
        )

        results = self.service.search("database release")
        self.assertEqual(results[0].memory_id, both.memory_id)
        self.assertEqual(
            {memory.memory_id for memory in results},
            {both.memory_id, database.memory_id, release.memory_id},
        )


if __name__ == "__main__":
    unittest.main()

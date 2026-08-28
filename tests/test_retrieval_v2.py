import json
import tempfile
import unittest

from pathlib import Path

from memleaf import Memleaf
from memleaf.config import save_config
from memleaf.retrieval import RetrievalError
from memleaf.service import _encode_page_cursor


class RetrievalV2Test(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.service = Memleaf(Path(self.tempdir.name) / "vault")

    def tearDown(self):
        self.tempdir.cleanup()

    def set_scopes(self, scopes):
        config = self.service.vault.config()
        config["scopes"] = scopes
        save_config(self.service.vault.config_path, config)

    def test_scope_catalog_is_metadata_only_and_contains_hierarchy(self):
        self.set_scopes(
            {
                "domain:work": {"aliases": ["工作"]},
                "portfolio:finance": {"parent": "domain:work"},
                "project:alpha": {"parent": "portfolio:finance", "aliases": ["Alpha"]},
            }
        )
        self.service.create_memory(
            memory_id="metadata-scope",
            title="Should not be exposed",
            body="secret body should never enter a scope catalog",
            scopes=["project:observed"],
        )

        result = self.service.scope_catalog()
        by_scope = {item["scope"]: item for item in result["scopes"]}
        self.assertEqual({"global", "domain:work", "portfolio:finance", "project:alpha", "project:observed"}, set(by_scope))
        self.assertEqual("domain:work", by_scope["portfolio:finance"]["parent"])
        self.assertEqual("portfolio:finance", by_scope["project:alpha"]["parent"])
        self.assertEqual(["Alpha"], by_scope["project:alpha"]["aliases"])
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        self.assertLessEqual(len(encoded), 2000)
        self.assertNotIn("metadata-scope", encoded)
        self.assertNotIn("Should not be exposed", encoded)
        self.assertNotIn("secret body", encoded)

    def test_scope_catalog_paginates_and_stale_cursor_is_rejected(self):
        scopes = {f"project:p{index:02d}": {"aliases": [f"p{index:02d}"]} for index in range(27)}
        self.set_scopes(scopes)
        pages = []
        page = self.service.scope_catalog(limit=20)
        pages.append(page)
        while page["has_more"]:
            page = self.service.scope_catalog(cursor=page["next_cursor"], limit=20)
            pages.append(page)
        all_scopes = [item["scope"] for page in pages for item in page["scopes"]]
        self.assertEqual(28, len(all_scopes))  # global plus 27 configured scopes
        self.assertEqual(len(all_scopes), len(set(all_scopes)))
        self.assertFalse(pages[-1]["has_more"])

        first = self.service.scope_catalog(limit=1)
        config = self.service.vault.config()
        config["scopes"]["project:new"] = {}
        save_config(self.service.vault.config_path, config)
        with self.assertRaises(RetrievalError) as raised:
            self.service.scope_catalog(cursor=first["next_cursor"])
        self.assertEqual("stale_cursor", raised.exception.code)

    def test_search_candidates_are_bounded_directory_with_stable_pages(self):
        for index in range(25):
            self.service.create_memory(
                memory_id=f"candidate-{index:02d}",
                title=f"Candidate {index}",
                body=f"private body {index}",
                tags=["shared-query"],
                scopes=["global"],
            )

        pages = []
        page = self.service.search_candidates("shared-query", limit=10)
        pages.append(page)
        self.assertEqual("found", page["status"])
        self.assertEqual(10, len(page["results"]))
        self.assertTrue(page["has_more"])
        self.assertTrue(all(set(item) == {"memory_id", "title", "scopes"} for item in page["results"]))
        first_ids = [item["memory_id"] for item in page["results"]]
        self.assertNotIn("private body", json.dumps(page))
        self.assertLessEqual(len(json.dumps(page, ensure_ascii=False, separators=(",", ":"))), 4000)

        # Page traversal must not be invalidated by read accounting.
        self.service.read_page(first_ids[0])
        while page["has_more"]:
            page = self.service.search_candidates("shared-query", limit=10, cursor=page["next_cursor"])
            pages.append(page)
        ids = [item["memory_id"] for page in pages for item in page["results"]]
        self.assertEqual(25, len(ids))
        self.assertEqual(25, len(set(ids)))
        self.assertEqual("found", pages[-1]["status"])
        self.assertFalse(pages[-1]["has_more"])

    def test_search_candidates_distinguishes_no_match_and_invalid_cursor(self):
        self.assertEqual(
            {"status": "no_match", "results": [], "has_more": False, "next_cursor": None},
            self.service.search_candidates("nothing"),
        )
        with self.assertRaises(RetrievalError) as raised:
            self.service.search_candidates("nothing", cursor="not-a-cursor")
        self.assertEqual("invalid_cursor", raised.exception.code)

    def test_search_candidates_rejects_cursor_at_end_of_nonempty_results(self):
        self.service.create_memory(
            memory_id="end-cursor",
            title="End cursor",
            body="end cursor body",
            tags=["end-cursor-query"],
            scopes=["global"],
        )
        with self.service.vault.lock():
            records = self.service._search_unlocked(
                "end-cursor-query",
                scope=None,
                include_history=False,
                todo_status="active",
                limit=None,
                stable=True,
            )
            fingerprint = self.service._search_fingerprint(
                records,
                query="end-cursor-query",
                scope=None,
                include_history=False,
                todo_status="active",
            )
        cursor = _encode_page_cursor("search_candidates", fingerprint, len(records))
        with self.assertRaises(RetrievalError) as raised:
            self.service.search_candidates("end-cursor-query", cursor=cursor)
        self.assertEqual("invalid_cursor", raised.exception.code)

    def test_scope_filter_happens_before_indexed_first_layer(self):
        self.set_scopes(
            {
                "project:alpha": {"aliases": ["alpha"]},
                "project:beta": {"aliases": ["beta"]},
            }
        )
        alpha = self.service.create_memory(
            memory_id="alpha-unindexed",
            title="Alpha note",
            body="needle appears only in this alpha body",
            scopes=["project:alpha"],
        )
        self.service.create_memory(
            memory_id="beta-indexed",
            title="Beta note",
            body="other beta body",
            tags=["needle"],
            scopes=["project:beta"],
        )
        result = self.service.search_candidates("needle", scope="project:alpha")
        self.assertEqual([alpha.memory_id], [item["memory_id"] for item in result["results"]])

    def test_explicit_project_scope_conflict_is_typed(self):
        self.set_scopes(
            {
                "domain:work": {},
                "portfolio:finance": {"parent": "domain:work"},
                "project:alpha": {"parent": "portfolio:finance", "aliases": ["alpha"]},
                "project:beta": {"parent": "portfolio:finance", "aliases": ["beta"]},
            }
        )
        with self.assertRaises(RetrievalError) as raised:
            self.service.search_candidates("alpha owner", scope="project:beta")
        self.assertEqual("scope_mismatch", raised.exception.code)
        # A broad global search is not an explicit conflicting project scope.
        self.assertEqual("no_match", self.service.search_candidates("alpha owner", scope="global")["status"])
        # A configured ancestor is a valid broad scope for the project.
        self.assertEqual("no_match", self.service.search_candidates("alpha owner", scope="domain:work")["status"])

    def test_long_scope_id_is_not_truncated_in_catalog_or_candidates(self):
        long_scope = "project:" + ("x" * 100)
        self.service.create_memory(
            memory_id="long-scope",
            title="Long scope",
            body="long scope body",
            scopes=[long_scope],
            tags=["long-scope-query"],
        )
        catalog = self.service.scope_catalog()
        self.assertIn(long_scope, {item["scope"] for item in catalog["scopes"]})
        result = self.service.search_candidates("long-scope-query")
        self.assertEqual([long_scope], result["results"][0]["scopes"])
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False, separators=(",", ":"))), 4000)


if __name__ == "__main__":
    unittest.main()

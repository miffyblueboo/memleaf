from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from memleaf import Memleaf


class CandidateNoMatchSemanticsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="memleaf-v2-nomatch-")
        self.service = Memleaf(Path(self.tempdir.name) / "vault")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_short_local_term_is_noise_for_compound_query(self) -> None:
        self.service.create_memory(
            memory_id="generic-project",
            title="通用项目工作方法",
            body="用于处理一般项目事项",
            tags=["项目"],
        )

        self.assertEqual("no_match", self.service.search_candidates("火星仓库项目-XYZ")["status"])
        # The legacy full search remains intentionally broad and unchanged.
        self.assertEqual(["generic-project"], [item.memory_id for item in self.service.search("火星仓库项目-XYZ")])
        self.assertEqual("found", self.service.search_candidates("项目")["status"])

    def test_high_confidence_title_body_alias_and_keyword_matches_exclude_noise(self) -> None:
        self.service.create_memory(
            memory_id="generic-project",
            title="通用项目工作方法",
            body="用于处理一般项目事项",
            tags=["项目"],
        )
        self.service.create_memory(
            memory_id="body-match",
            title="仓库事项",
            body="火星仓库项目-XYZ 的唯一记录",
        )
        self.service.create_memory(
            memory_id="alias-match",
            title="仓储事项",
            body="部署记录",
            aliases=["火星仓库", "XYZ"],
        )
        self.service.create_memory(
            memory_id="keyword-match",
            title="外部事项",
            body="部署记录",
            keywords=["火星仓库-XYZ"],
        )

        result = self.service.search_candidates("火星仓库项目-XYZ")
        self.assertEqual("found", result["status"])
        ids = {item["memory_id"] for item in result["results"]}
        self.assertEqual({"body-match", "alias-match", "keyword-match"}, ids)
        self.assertNotIn("generic-project", ids)

    def test_exact_memory_id_is_still_a_candidate(self) -> None:
        self.service.create_memory(
            memory_id="lookup-id",
            title="Unrelated title",
            body="Unrelated body",
        )
        result = self.service.search_candidates("lookup-id")
        self.assertEqual("found", result["status"])
        self.assertEqual(["lookup-id"], [item["memory_id"] for item in result["results"]])

    def test_explicit_ascii_identifier_is_required_in_a_mixed_query(self) -> None:
        self.service.create_memory(
            memory_id="topic-only",
            title="火星仓库项目负责人",
            body="火星仓库项目负责人记录",
        )
        self.service.create_memory(
            memory_id="topic-and-id",
            title="火星仓库项目负责人 XYZ",
            body="火星仓库项目负责人记录，标识 XYZ",
        )

        result = self.service.search_candidates("火星仓库项目负责人-XYZ")
        self.assertEqual("found", result["status"])
        self.assertEqual(["topic-and-id"], [item["memory_id"] for item in result["results"]])

    def test_hyphenated_ascii_identifier_is_not_replaced_by_a_prefix(self) -> None:
        self.service.create_memory(
            memory_id="prefix-only",
            title="Mars project notes",
            body="Mars project notes",
        )
        self.service.create_memory(
            memory_id="full-identifier",
            title="Mars project-XYZ notes",
            body="Mars project-XYZ notes",
        )

        result = self.service.search_candidates("Mars project-XYZ")
        self.assertEqual("found", result["status"])
        self.assertEqual(["full-identifier"], [item["memory_id"] for item in result["results"]])


if __name__ == "__main__":
    unittest.main()

import unittest

from memleaf.models import Memory
from memleaf.turn_plan import revision_digest


class RevisionDigestTests(unittest.TestCase):
    def _memory(self, **overrides):
        value = {
            "memory_id": "mem-revision-test",
            "title": "项目负责人",
            "body": "项目负责人是甲。",
            "type": "identity",
            "scopes": ["global"],
            "sources": [{"event_key": "source-a"}],
            "created": "2026-09-08T00:00:00Z",
            "updated": "2026-09-08T00:00:01Z",
            "hit_count": 0,
            "last_hit_at": None,
        }
        value.update(overrides)
        return Memory.from_mapping(value)

    def test_generated_times_and_retrieval_counters_do_not_change_revision(self):
        baseline = self._memory()
        rehydrated = self._memory(
            created="2026-09-08T00:01:00Z",
            updated="2026-09-08T00:01:01Z",
            hit_count=9,
            last_hit_at="2026-09-08T00:01:02Z",
        )

        self.assertEqual(revision_digest(baseline), revision_digest(rehydrated))

    def test_authored_semantic_change_changes_revision(self):
        baseline = self._memory()
        changed = self._memory(body="项目负责人是乙。")

        self.assertNotEqual(revision_digest(baseline), revision_digest(changed))

    def test_provenance_change_changes_revision(self):
        baseline = self._memory()
        changed = self._memory(sources=[{"event_key": "source-b"}])

        self.assertNotEqual(revision_digest(baseline), revision_digest(changed))


if __name__ == "__main__":
    unittest.main()

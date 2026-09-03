from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from memleaf.retrieval_gate import (
    MAX_READ_CHARS,
    MAX_READ_ITEMS,
    MAX_READ_PAGE_CHARS,
    RetrievalGateError,
    begin_turn,
    bind_turn_alias,
    continuation_marker,
    find_pending_continuation,
    find_turn,
    guarded_read,
    observe_search,
    request_gate_retry,
    validate_current_turn,
    validate_turn,
)
from memleaf.vault import Vault


class RetrievalGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="memleaf-retrieval-gate-")
        self.vault = Vault(Path(self.tempdir.name) / "vault")
        self.vault.ensure()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_turn_is_idempotent_and_search_calls_are_deduplicated(self) -> None:
        first = begin_turn(self.vault, "codex", "session", "turn-1")
        self.assertEqual(first, begin_turn(self.vault, "codex", "session", "turn-1"))
        self.assertEqual(first, find_turn(self.vault, "codex", "session", "turn-1"))
        self.assertEqual("NOT_SEARCHED", validate_turn(self.vault, first)["status"])

        observe_search(self.vault, first, "found", "call-1")
        observe_search(self.vault, first, "found", "call-1")
        state = validate_turn(self.vault, first)
        self.assertEqual("FOUND", state["status"])
        self.assertEqual(1, state["search_attempts"])

    def test_search_statuses_are_distinct(self) -> None:
        retrieval_id = begin_turn(self.vault, "codex", "session", "turn-1")
        observe_search(self.vault, retrieval_id, "no_match", "call-no-match")
        self.assertEqual("NO_MATCH", validate_turn(self.vault, retrieval_id)["status"])
        observe_search(self.vault, retrieval_id, "error", "call-error")
        self.assertEqual("ERROR", validate_turn(self.vault, retrieval_id)["status"])

    def test_read_requires_found_search_and_does_not_consume_denied_reads(self) -> None:
        for search_status in (None, "no_match", "error"):
            with self.subTest(search_status=search_status):
                retrieval_id = begin_turn(self.vault, "hermes", "session", str(search_status))
                if search_status is not None:
                    observe_search(self.vault, retrieval_id, search_status, f"status-{search_status}")
                reader_calls = 0

                def reader(_: int):
                    nonlocal reader_calls
                    reader_calls += 1
                    return {"body": "should-not-be-read"}

                with self.assertRaises(RetrievalGateError) as error:
                    guarded_read(self.vault, retrieval_id, "memory", reader)
                self.assertEqual("retrieval_search_required", error.exception.code)
                self.assertEqual(0, reader_calls)
                state = validate_turn(self.vault, retrieval_id)
                self.assertEqual(0, state["read_count"])
                self.assertEqual(0, state["read_chars"])

    def test_hermes_current_turn_validation_rejects_old_token(self) -> None:
        first = begin_turn(self.vault, "hermes", "session", "turn-1")
        second = begin_turn(self.vault, "hermes", "session", "turn-2")
        self.assertNotEqual(first, second)
        self.assertEqual(second, validate_current_turn(self.vault, second, "hermes")["retrieval_id"])
        with self.assertRaises(RetrievalGateError) as error:
            validate_current_turn(self.vault, first, "hermes")
        self.assertEqual("retrieval_turn_mismatch", error.exception.code)

    def test_hermes_search_writeback_rechecks_current_turn_atomically(self) -> None:
        first = begin_turn(self.vault, "hermes", "session", "turn-1")
        second = begin_turn(self.vault, "hermes", "session", "turn-2")
        with self.assertRaises(RetrievalGateError) as error:
            observe_search(
                self.vault,
                first,
                "found",
                "stale-search",
                current_source="hermes",
            )
        self.assertEqual("retrieval_turn_mismatch", error.exception.code)
        self.assertEqual("NOT_SEARCHED", validate_turn(self.vault, first)["status"])
        self.assertEqual(0, validate_turn(self.vault, first)["search_attempts"])
        self.assertEqual("NOT_SEARCHED", validate_turn(self.vault, second)["status"])

        observe_search(self.vault, second, "found", "current-search", current_source="hermes")
        self.assertEqual("FOUND", validate_turn(self.vault, second)["status"])

    def test_read_audit_does_not_limit_ids_or_total_chars(self) -> None:
        retrieval_id = begin_turn(self.vault, "codex", "session", "turn-1")
        observe_search(self.vault, retrieval_id, "found", "read-audit-search")
        calls: list[int] = []

        def reader(allowed_chars: int):
            calls.append(allowed_chars)
            return {"body": "x" * allowed_chars}

        for index in range(8):
            page = guarded_read(self.vault, retrieval_id, f"mem-{index}", reader)
            self.assertEqual(MAX_READ_PAGE_CHARS, len(page["body"]))
        state = validate_turn(self.vault, retrieval_id)
        self.assertEqual(8, state["read_count"])
        self.assertEqual(8 * MAX_READ_PAGE_CHARS, state["read_chars"])
        self.assertEqual([MAX_READ_PAGE_CHARS] * 8, calls)
        self.assertIsNone(MAX_READ_ITEMS)
        self.assertIsNone(MAX_READ_CHARS)

    def test_failed_or_empty_read_does_not_consume_budget(self) -> None:
        retrieval_id = begin_turn(self.vault, "codex", "session", "turn-1")
        observe_search(self.vault, retrieval_id, "found", "read-empty-search")

        def empty_reader(allowed_chars: int):
            del allowed_chars
            return {"body": ""}

        self.assertEqual("", guarded_read(self.vault, retrieval_id, "empty", empty_reader)["body"])
        self.assertEqual(0, validate_turn(self.vault, retrieval_id)["read_count"])
        self.assertEqual(0, validate_turn(self.vault, retrieval_id)["read_chars"])

        class ReaderFailure(RuntimeError):
            pass

        def failing_reader(allowed_chars: int):
            del allowed_chars
            raise ReaderFailure("typed reader failure")

        with self.assertRaises(ReaderFailure):
            guarded_read(self.vault, retrieval_id, "failed", failing_reader)
        state = validate_turn(self.vault, retrieval_id)
        self.assertEqual(0, state["read_count"])
        self.assertEqual(0, state["read_chars"])

    def test_expired_id_is_rejected_on_every_direct_operation(self) -> None:
        retrieval_id = begin_turn(self.vault, "codex", "session", "turn-1")
        ledger_path = self.vault.index_path / "retrieval_gate.json"
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        ledger["entries"][retrieval_id]["expires_at"] = 0
        ledger_path.write_text(json.dumps(ledger), encoding="utf-8")

        for operation in (
            lambda: validate_turn(self.vault, retrieval_id),
            lambda: observe_search(self.vault, retrieval_id, "found", "call"),
            lambda: guarded_read(self.vault, retrieval_id, "memory", lambda _: {"body": "x"}),
            lambda: request_gate_retry(self.vault, retrieval_id),
        ):
            with self.subTest(operation=operation):
                with self.assertRaises(RetrievalGateError) as error:
                    operation()
                self.assertEqual("retrieval_id_invalid", error.exception.code)

    def test_continuation_alias_is_exact_and_does_not_create_a_new_budget(self) -> None:
        retrieval_id = begin_turn(self.vault, "codex", "session", "turn-1")
        request_gate_retry(self.vault, retrieval_id)
        marker = continuation_marker(self.vault, retrieval_id)
        self.assertIsNotNone(marker)
        self.assertEqual(
            retrieval_id,
            find_pending_continuation(self.vault, "codex", "session", f"retry [{marker}]"),
        )
        self.assertIsNone(
            find_pending_continuation(self.vault, "codex", "session", "ordinary user input"),
        )
        bind_turn_alias(self.vault, retrieval_id, "turn-2")
        self.assertEqual(retrieval_id, find_turn(self.vault, "codex", "session", "turn-2"))


if __name__ == "__main__":
    unittest.main()

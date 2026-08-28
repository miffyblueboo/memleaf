from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import memleaf.retrieval_gate as gate
from memleaf.retrieval_gate import (
    GATE_TTL_SECONDS,
    MAX_LEDGER_ENTRIES,
    MAX_READ_CHARS,
    MAX_READ_ITEMS,
    MAX_READ_PAGE_CHARS,
    RetrievalGateError,
    begin_turn,
    continuation_marker,
    find_turn,
    find_pending_continuation,
    guarded_read,
    observe_search,
    request_gate_retry,
    validate_turn,
)
from memleaf.vault import Vault


class RetrievalGateV2LimitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="memleaf-v2-gate-")
        self.vault = Vault(Path(self.tempdir.name) / "vault")
        self.vault.ensure()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_ledger_prunes_expired_turns_and_caps_new_entries(self) -> None:
        now = 1_000_000.0
        with patch.object(gate.time, "time", return_value=now):
            expired = begin_turn(self.vault, "codex", "session", "expired")

        fresh_start = now + GATE_TTL_SECONDS + 1
        clock_values = [fresh_start] + [fresh_start + index for index in range(MAX_LEDGER_ENTRIES + 1)]
        with patch.object(gate.time, "time", side_effect=clock_values):
            self.assertIsNone(find_turn(self.vault, "codex", "session", "expired"))

            fresh_ids = [
                begin_turn(self.vault, "codex", "session", f"turn-{index:03}")
                for index in range(MAX_LEDGER_ENTRIES + 1)
            ]
            ledger_path = self.vault.index_path / "retrieval_gate.json"
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            entries = ledger["entries"]

        self.assertNotIn(expired, entries)
        self.assertEqual(MAX_LEDGER_ENTRIES, len(entries))
        self.assertNotIn(fresh_ids[0], entries)
        self.assertEqual(set(fresh_ids[1:]), set(entries))

    def test_turn_identity_and_read_budget_are_isolated_by_source_session_and_turn(self) -> None:
        codex_a = begin_turn(self.vault, "codex", "session-a", "turn-1")
        codex_turn_b = begin_turn(self.vault, "codex", "session-a", "turn-2")
        codex_session_b = begin_turn(self.vault, "codex", "session-b", "turn-1")
        hermes_a = begin_turn(self.vault, "hermes", "session-a", "turn-1")
        for index, retrieval_id in enumerate(
            (codex_a, codex_turn_b, codex_session_b, hermes_a),
            start=1,
        ):
            observe_search(self.vault, retrieval_id, "found", f"budget-search-{index}")

        self.assertEqual(codex_a, find_turn(self.vault, "codex", "session-a", "turn-1"))
        self.assertEqual(codex_turn_b, find_turn(self.vault, "codex", "session-a", "turn-2"))
        self.assertEqual(codex_session_b, find_turn(self.vault, "codex", "session-b", "turn-1"))
        self.assertEqual(hermes_a, find_turn(self.vault, "hermes", "session-a", "turn-1"))
        self.assertIsNone(find_turn(self.vault, "codex", "session-b", "missing"))
        self.assertEqual(4, len({codex_a, codex_turn_b, codex_session_b, hermes_a}))

        def full_page(allowed_chars: int):
            return {"body": "x" * allowed_chars}

        for index in range(MAX_READ_ITEMS):
            guarded_read(self.vault, codex_a, f"codex-memory-{index}", full_page)
        with self.assertRaises(RetrievalGateError) as error:
            guarded_read(self.vault, codex_a, "codex-memory-over-budget", full_page)
        self.assertEqual("retrieval_read_budget_exceeded", error.exception.code)

        # Each identity dimension gets an independent budget, even for the
        # same memory identifier that exhausted the first turn.
        for isolated_id in (codex_turn_b, codex_session_b, hermes_a):
            page = guarded_read(self.vault, isolated_id, "codex-memory-0", full_page)
            self.assertEqual(MAX_READ_PAGE_CHARS, len(page["body"]))

        query_marker = "query-that-must-not-be-persisted"
        body_marker = "body-that-must-not-be-persisted"
        page = guarded_read(
            self.vault,
            codex_turn_b,
            "memory-body-check",
            lambda _: {"body": body_marker, "query": query_marker},
        )
        self.assertEqual(body_marker, page["body"])
        request_gate_retry(self.vault, codex_turn_b)
        marker = continuation_marker(self.vault, codex_turn_b)
        self.assertIsNotNone(marker)
        self.assertEqual(
            codex_turn_b,
            find_pending_continuation(
                self.vault,
                "codex",
                "session-a",
                f"{query_marker} [{marker}]",
            ),
        )
        ledger_text = (self.vault.index_path / "retrieval_gate.json").read_text(encoding="utf-8")
        self.assertNotIn(query_marker, ledger_text)
        self.assertNotIn(body_marker, ledger_text)

        self.assertEqual(MAX_READ_ITEMS, validate_turn(self.vault, codex_a)["read_count"])
        self.assertEqual(2, validate_turn(self.vault, codex_turn_b)["read_count"])
        self.assertEqual(1, validate_turn(self.vault, codex_session_b)["read_count"])
        self.assertEqual(1, validate_turn(self.vault, hermes_a)["read_count"])

    def test_concurrent_reads_share_budget_and_failed_reader_leaks_nothing(self) -> None:
        retrieval_id = begin_turn(self.vault, "codex", "session", "turn-1")
        observe_search(self.vault, retrieval_id, "found", "concurrent-read-search")

        def empty_reader(allowed_chars: int):
            del allowed_chars
            return {"body": ""}

        self.assertEqual("", guarded_read(self.vault, retrieval_id, "empty", empty_reader)["body"])

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

        worker_count = MAX_READ_ITEMS + 3
        start = threading.Barrier(worker_count + 1)
        first_reader_started = threading.Event()
        release_first_reader = threading.Event()
        outcomes: list[tuple[str, int | str]] = []
        outcomes_lock = threading.Lock()
        reader_calls = 0
        reader_calls_lock = threading.Lock()

        def reader(allowed_chars: int):
            nonlocal reader_calls
            with reader_calls_lock:
                reader_calls += 1
                first_call = reader_calls == 1
            if first_call:
                first_reader_started.set()
                if not release_first_reader.wait(timeout=5):
                    raise AssertionError("first guarded read was not released")
            return {"body": "x" * allowed_chars}

        def worker(index: int) -> None:
            start.wait(timeout=5)
            try:
                page = guarded_read(self.vault, retrieval_id, f"memory-{index}", reader)
                outcome: tuple[str, int | str] = ("ok", len(page["body"]))
            except RetrievalGateError as error:
                outcome = ("error", error.code)
            with outcomes_lock:
                outcomes.append(outcome)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(worker_count)]
        for thread in threads:
            thread.start()
        start.wait(timeout=5)
        self.assertTrue(first_reader_started.wait(timeout=5))
        release_first_reader.set()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())

        successes = [outcome for outcome in outcomes if outcome[0] == "ok"]
        failures = [outcome for outcome in outcomes if outcome[0] == "error"]
        self.assertEqual(MAX_READ_ITEMS, len(successes))
        self.assertEqual(worker_count - MAX_READ_ITEMS, len(failures))
        self.assertTrue(all(outcome[1] == MAX_READ_PAGE_CHARS for outcome in successes))
        self.assertTrue(all(outcome[1] == "retrieval_read_budget_exceeded" for outcome in failures))
        final_state = validate_turn(self.vault, retrieval_id)
        self.assertEqual(MAX_READ_ITEMS, final_state["read_count"])
        self.assertEqual(MAX_READ_CHARS, final_state["read_chars"])

        ledger_path = self.vault.index_path / "retrieval_gate.json"
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        self.assertEqual(MAX_READ_CHARS, ledger["entries"][retrieval_id]["read_chars"])
        lock_path = self.vault.index_path / "retrieval_gate.lock"
        self.assertTrue(lock_path.is_file())
        self.assertNotEqual(lock_path, self.vault.lock_path)


if __name__ == "__main__":
    unittest.main()

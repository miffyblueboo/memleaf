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

    def test_turn_identity_and_read_audit_are_isolated_by_source_session_and_turn(self) -> None:
        codex_a = begin_turn(self.vault, "codex", "session-a", "turn-1")
        codex_turn_b = begin_turn(self.vault, "codex", "session-a", "turn-2")
        codex_session_b = begin_turn(self.vault, "codex", "session-b", "turn-1")
        hermes_a = begin_turn(self.vault, "hermes", "session-a", "turn-1")
        for index, retrieval_id in enumerate((codex_a, codex_turn_b, codex_session_b, hermes_a), start=1):
            observe_search(self.vault, retrieval_id, "found", f"audit-search-{index}")
        def full_page(allowed_chars: int):
            return {"body": "x" * allowed_chars}
        for index in range(8):
            guarded_read(self.vault, codex_a, f"codex-memory-{index}", full_page)
        self.assertEqual(8, validate_turn(self.vault, codex_a)["read_count"])
        self.assertEqual(8 * MAX_READ_PAGE_CHARS, validate_turn(self.vault, codex_a)["read_chars"])
        for isolated_id in (codex_turn_b, codex_session_b, hermes_a):
            page = guarded_read(self.vault, isolated_id, "codex-memory-0", full_page)
            self.assertEqual(MAX_READ_PAGE_CHARS, len(page["body"]))
            self.assertEqual(1, validate_turn(self.vault, isolated_id)["read_count"])

    def test_concurrent_reads_are_all_audited_and_failed_reader_leaks_nothing(self) -> None:
        retrieval_id = begin_turn(self.vault, "codex", "session", "turn-1")
        observe_search(self.vault, retrieval_id, "found", "concurrent-read-search")
        class ReaderFailure(RuntimeError):
            pass
        with self.assertRaises(ReaderFailure):
            guarded_read(self.vault, retrieval_id, "failed", lambda _: (_ for _ in ()).throw(ReaderFailure("typed")))
        worker_count = 8
        start = threading.Barrier(worker_count + 1)
        outcomes: list[int] = []
        lock = threading.Lock()
        def worker(index: int) -> None:
            start.wait(timeout=5)
            page = guarded_read(self.vault, retrieval_id, f"memory-{index}", lambda allowed: {"body": "x" * allowed})
            with lock:
                outcomes.append(len(page["body"]))
        threads = [threading.Thread(target=worker, args=(index,)) for index in range(worker_count)]
        for thread in threads:
            thread.start()
        start.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertEqual([MAX_READ_PAGE_CHARS] * worker_count, sorted(outcomes))
        final_state = validate_turn(self.vault, retrieval_id)
        self.assertEqual(worker_count, final_state["read_count"])
        self.assertEqual(worker_count * MAX_READ_PAGE_CHARS, final_state["read_chars"])



if __name__ == "__main__":
    unittest.main()

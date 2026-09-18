from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from memleaf import Memleaf, Memory
from memleaf.incremental_journal import KEY, load_work


class IncrementalFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.s = Memleaf.initialize(Path(self.temp.name) / "vault")
        self.capture()

    def capture(self, turn="t", *, content="Atlas task completed", seq=1):
        self.s.capture("hermes", "s", turn, "user", content, message_id=turn+"-u", source_sequence=seq,
                       source_time="2026-09-17T10:00:00+08:00")
        self.s.capture("hermes", "s", turn, "assistant", "Acknowledged", message_id=turn+"-a",
                       source_sequence=seq+1, final=True)

    def target(self, mid="mem-old", **kwargs):
        args = dict(memory_id=mid, title="Atlas task", body="Deliver the report.", type="todo",
                    status="active", scopes=["global"], assignee="user", waiting_on="approval",
                    due_date="2026-09-20", custom={"keep": True})
        args.update(kwargs)
        # This fixture also simulates a later trusted edit of an existing target.
        # Use the raw replacement API explicitly, not the create-only API.
        return self.s.write_memory(Memory.new(**args), overwrite=True)

    def request(self, items, **args):
        params = dict(source="hermes", session_id="s", turn_id="t")
        params.update(args)
        preview = self.s.preview_incremental(**params)
        return dict(**params, response=json.dumps({"items":items}), expected_snapshot=preview["snapshot_id"], intent_id="intent-1")

    def update(self, **fields):
        return {"action":"UPDATE", "target":"m1", "evidence":["e1"], "patch":fields or {"status":"completed"}}

    def create(self, **fields):
        value = {"type":"todo", "scope":"global", "title":"Atlas task", "body":"Deliver the report.", "status":"active"}
        value.update(fields)
        return {"action":"CREATE", "evidence":["e1"], "memory":value}

    def no_memory(self, ref="e2"):
        return {"action":"NO_MEMORY", "evidence":[ref]}

    def ledger(self):
        return json.loads(self.s.vault.processed_state_path.read_text())

    def works(self):
        state = self.ledger()
        return [load_work(state, key) for key in state.get(KEY, {})]

    def hist(self):
        return self.s.vault.list_markdown("history")

    def revise(self, content="Original assertion withdrawn"):
        self.s.capture("hermes","s","t","user",content,message_id="t-u",message_revision="2",previous_message_revision="1",source_sequence=1)

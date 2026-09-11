import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from memleaf import Memleaf
from memleaf.config import save_config
from memleaf.index import event_key
from memleaf.inbox import parse_inbox
from memleaf.llm import ModelError, ModelUnavailable
from memleaf.memory_writer import MemoryWriter
from memleaf.processing import ProcessingError, Processor
from memleaf.model_execution import ModelExecutor
from memleaf.planning_context import PlanningContext
from memleaf.prompts import RELATIVE_TIME_CORRECTION
from memleaf.validation import ModelOutputError


from tests.semantic_fixtures import semantic_fixture, deferred_target_response

@semantic_fixture
class QueueBackend:
    provider = "fake"
    model = "b2a-test"

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    def complete(self, prompt, *, system="", purpose="", temperature=0.0):
        self.calls.append({"prompt": prompt, "system": system, "purpose": purpose})
        if not self.responses:
            raise ModelError("fake queue exhausted")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            return response(prompt, system=system, purpose=purpose, temperature=temperature)
        return response


class Clock:
    def __init__(self):
        self.value = datetime(2026, 8, 24, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

class StageB2ATestBase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.path = Path(self.tempdir.name) / "vault"

    def tearDown(self):
        self.tempdir.cleanup()

    def service(self, backend=None, native=None, name="vault"):
        path = self.path if name == "vault" else Path(self.tempdir.name) / name
        return Memleaf(path, model=backend, clock=self.clock, native_memory_reader=native)

    @staticmethod
    def gate(candidates):
        return json.dumps({"candidates": candidates})

    @staticmethod
    def candidate(
        candidate_id,
        evidence,
        *,
        memory="a durable fact",
        duplicate=False,
        worth=True,
        type="fact",
        update_memory_id=None,
    ):
        value = {
            "candidate_id": candidate_id,
            "memory": memory,
            "evidence_event_ids": list(evidence),
            "duplicate": duplicate,
            "worth": worth,
            "type": type,
            "scopes": ["global"],
            "scope_source": "model",
        }
        if update_memory_id is not None:
            value["update_memory_id"] = update_memory_id
        return value

    @staticmethod
    def summary(event_key_value, title="Fact", body="A durable fact", type="fact", **extra):
        value = {
            "title": title,
            "body": body,
            "tags": ["b2a"],
            "type": type,
            "scopes": ["global"],
            "scope_source": "model",
            "sources": [
                {
                    "event_key": event_key_value,
                    "session_id": "model-forged-session",
                    "turn_id": "model-forged-turn",
                    "conversation_title": "MODEL_FORGED_TITLE",
                }
            ],
        }
        value.update(extra)
        return json.dumps(value)

    def capture_turn(
        self,
        service,
        *,
        source="codex",
        session="s",
        turn="t1",
        user_event="u1",
        assistant_event="a1",
        user="user visible fact",
        assistant="assistant visible response",
    ):
        service.capture(source, session, turn, "user", user, event_id=user_event)
        if assistant_event is not None:
            service.capture(source, session, turn, "assistant", assistant, event_id=assistant_event)
        return event_key(user_event), event_key(assistant_event) if assistant_event else None

    def processed(self, service):
        return json.loads(service.vault.processed_state_path.read_text(encoding="utf-8"))

    def knowledge(self, service):
        return service._read_memories_unlocked("knowledge")

__all__ = [name for name in globals() if not name.startswith('__')]

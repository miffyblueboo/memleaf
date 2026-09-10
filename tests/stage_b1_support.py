import json
import os
import tempfile
import urllib.error
import unittest
from pathlib import Path
from unittest import mock

from memleaf import Memleaf
from memleaf.config import default_config, load_config, save_config
from memleaf.inbox import complete_turns, parse_inbox
from memleaf.prompts import (
    DUPLICATE_TARGET_CORRECTION,
    GATE_TYPE_CORRECTION,
    GATE_SYSTEM,
    MIXED_PROJECT_SCOPES_CORRECTION,
    SCOPE_GROUNDING_CORRECTION,
    SUMMARY_SCOPE_CORRECTION,
    SUMMARY_TARGET_CORRECTION,
    SUMMARY_TYPE_CORRECTION,
    SUMMARIZE_SYSTEM,
    UPDATE_TARGET_TYPE_CORRECTION,
    gate_prompt,
    summarize_prompt,
)
from memleaf.admission import analyze_turn_evidence, evidence_prompt
from memleaf.validation import (
    ModelOutputError,
    NO_CHANGE_DECISION,
    normalize_relative_calendar_text,
)
from memleaf.index import event_key, turn_key
from memleaf.llm import (
    CallableBackend,
    ClaudeCompatibleBackend,
    FakeBackend,
    ModelError,
    GeminiBackend,
    ModelRouter,
    ModelUnavailable,
    OpenAICompatibleBackend,
)
from memleaf.model_execution import ModelExecutor
from memleaf.process_common import _event_payload
from memleaf.validation import parse_gate_output, parse_summarize_output
from memleaf.mcp_server import _safe_model_diagnostics


class _Response:
    def __init__(self, value):
        self.value = json.dumps(value).encode("utf-8")
        self.closed = False

    def read(self):
        return self.value

    def close(self):
        self.closed = True


class _Opener:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        return _Response(self.response)


class _RawResponse:
    def __init__(self, value):
        self.value = value
        self.closed = False

    def read(self):
        return self.value

    def close(self):
        self.closed = True


class _ErrorOpener:
    def __init__(self, error):
        self.error = error

    def __call__(self, request, timeout):
        del request, timeout
        raise self.error


from tests.semantic_fixtures import semantic_function

class StageB1TestBase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.vault_path = Path(self.tempdir.name) / "vault"
        self.service = Memleaf(self.vault_path)

    def tearDown(self):
        self.tempdir.cleanup()

    def _write_inbox(self, source, session, text):
        path = self.vault_path / "inbox" / source
        path.mkdir(parents=True, exist_ok=True)
        file_path = path / f"{session}.md"
        file_path.write_text(text, encoding="utf-8")
        return file_path

__all__ = [name for name in globals() if not name.startswith('__')]

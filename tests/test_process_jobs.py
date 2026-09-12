from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from memleaf import Memleaf
from memleaf.mcp_server import _invoke_tool
from memleaf.process_jobs import _safe_error, enqueue, status
import memleaf.process_jobs as process_jobs


class _Child:
    def __init__(self, pid: int):
        self.pid = pid

    def wait(self):
        return 0


class ProcessJobsTests(unittest.TestCase):
    def test_error_retains_package_locations_without_source_or_local_values(self):
        filename = Path(process_jobs.__file__).parent / "synthetic_parser.py"
        namespace = {}
        exec(compile(
            "def parse():\n    private_body = 'private-mail-body'\n    return [].get('value')\n",
            str(filename), "exec",
        ), namespace)
        try:
            namespace["parse"]()
        except AttributeError as error:
            result = _safe_error(error)
        self.assertEqual(result["type"], "AttributeError")
        self.assertEqual(result["code_locations"], "synthetic_parser.py:3:parse")
        self.assertNotIn("private-mail-body", str(result))
        self.assertNotIn(str(filename.parent), str(result))
        self.assertNotIn("return [].get", str(result))

    def test_single_pass_metrics_survive_background_status_projection(self):
        private_prompt = "PRIVATE PROMPT BODY MUST NOT PERSIST"
        metrics = {
            "total": {"call_count": 2, "retry_count": 1, "input_bytes": 321},
            "stages": {
                "single_pass": {"call_count": 2, "retry_count": 1, "input_bytes": 321},
            },
            "operations": {
                "single_pass_primary": {"call_count": 1, "input_bytes": 200},
                "single_pass_format_repair": {"call_count": 1, "retry_count": 1, "input_bytes": 121},
            },
            "calls": [
                {
                    "stage": "single_pass",
                    "operation": "single_pass_primary",
                    "call_index": 1,
                    "request_duration_ms": 80,
                    "input_bytes": 200,
                    "retry": False,
                    "failed": False,
                    "invalid_output": False,
                    "thinking_mode": "disabled",
                    "thinking_effective": "disabled",
                    "thinking_control": "deepseek_thinking_effort",
                    "prompt": private_prompt,
                },
                {
                    "stage": "single_pass",
                    "operation": "single_pass_format_repair",
                    "call_index": 2,
                    "request_duration_ms": 40,
                    "input_bytes": 121,
                    "retry": True,
                    "failed": False,
                    "invalid_output": False,
                    "response": private_prompt,
                },
            ],
        }

        safe = process_jobs._safe_model_metrics(metrics)

        self.assertEqual(safe["stages"]["single_pass"]["call_count"], 2)
        self.assertEqual(safe["operations"]["single_pass_primary"]["call_count"], 1)
        self.assertEqual(safe["operations"]["single_pass_format_repair"]["retry_count"], 1)
        self.assertEqual([row["stage"] for row in safe["calls"]], ["single_pass", "single_pass"])
        self.assertEqual(safe["calls"][0]["thinking_effective"], "disabled")
        self.assertNotIn(private_prompt, str(safe))
        self.assertNotIn("prompt", safe["calls"][0])
        self.assertNotIn("response", safe["calls"][1])

    def test_background_mcp_returns_job_and_status_is_readable(self):
        with tempfile.TemporaryDirectory() as temporary:
            service = Memleaf(Path(temporary) / "vault")
            accepted = _invoke_tool(
                service,
                "process",
                {"source": "hermes", "session_id": "session", "background": True},
            )["structuredContent"]
            self.assertTrue(accepted["accepted"])
            self.assertFalse(accepted["completed"])
            self.assertEqual(accepted["status"], "pending")
            self.assertTrue(accepted["job_id"])
            observed = _invoke_tool(
                service, "process_status", {"job_id": accepted["job_id"]}
            )["structuredContent"]
            self.assertEqual(observed["job_id"], accepted["job_id"])
            deadline = time.time() + 5
            while observed["status"] in {"starting", "running"} and time.time() < deadline:
                time.sleep(0.05)
                observed = _invoke_tool(
                    service, "process_status", {"job_id": accepted["job_id"]}
                )["structuredContent"]
            self.assertIn(observed["status"], {"succeeded", "deferred", "failed"})

    def test_worker_completes_empty_session_without_mcp_client(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary) / "vault"
            accepted = enqueue(vault, source="hermes", session_id="session")
            deadline = time.time() + 5
            observed = status(vault, job_id=accepted["job_id"])
            while observed["status"] in {"starting", "running"} and time.time() < deadline:
                time.sleep(0.05)
                observed = status(vault, job_id=accepted["job_id"])
            self.assertEqual(observed["status"], "succeeded")
            self.assertEqual(observed["result"]["processed_turns"], 0)

    def test_same_running_session_requests_rerun_without_second_job(self):
        with tempfile.TemporaryDirectory() as temporary, patch(
            "memleaf.process_jobs._launch", return_value=_Child(os.getpid())
        ):
            vault = Path(temporary) / "vault"
            first = enqueue(vault, source="hermes", session_id="session")
            second = enqueue(vault, source="hermes", session_id="session")
            self.assertEqual(first["job_id"], second["job_id"])
            self.assertTrue(second["rerun_requested"])
            self.assertEqual(status(vault, job_id=first["job_id"])["status"], "running")

    def test_explicit_process_without_background_stays_synchronous(self):
        with tempfile.TemporaryDirectory() as temporary:
            service = Memleaf(Path(temporary) / "vault")
            result = _invoke_tool(
                service, "process", {"source": "hermes", "session_id": "session"}
            )["structuredContent"]
            self.assertEqual(result["processed_turns"], 0)
            self.assertNotIn("job_id", result)


if __name__ == "__main__":
    unittest.main()

"""Native transport envelopes must preserve usable text and failure state."""
import json
import unittest

from memleaf.admission import MAX_EXTERNAL_UNIT_BYTES, analyze_turn_evidence
from tests.test_hermes_provider import load_provider_module


provider = load_provider_module()[0]


def records(name, payload):
    return provider._bounded_current_tool_evidence([
        {"role": "user", "content": "Inspect the supplied records."},
        {"role": "assistant", "tool_calls": [
            {"id": "call-1", "function": {"name": name, "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call-1", "content": json.dumps(payload)},
    ])


def units(evidence):
    return analyze_turn_evidence([{"event_key": "e1", "role": "assistant",
                                  "content": "", "tool_evidence": evidence}])


class HermesTransportEvidenceTests(unittest.TestCase):
    def test_native_output_preserves_record_headers_and_exact_source_spans(self):
        body = ("========\nProject Alpha\n\nReview the migration plan.\n\nConfirm a rollback owner.\n"
                "========\nProject Beta\n\nDeliver the revised specification.\n")
        evidence = records("execute_code", {"status": "success", "exit_code": 0,
                                             "output": body, "stdout_truncated": False})
        self.assertEqual(evidence[0]["content"], body.strip())
        physical = units(evidence)
        self.assertEqual(len(physical), 2)
        self.assertTrue(all(unit.can_support for unit in physical))
        self.assertIn("Project Alpha", physical[0].text)
        self.assertIn("rollback owner", physical[0].text)
        self.assertNotIn("Project Beta", physical[0].text)
        for unit in physical:
            self.assertEqual(body[unit.start:unit.end], unit.text)

    def test_failures_never_authorize_observed_output(self):
        for name, payload in (
            ("terminal", {"exit_code": 2, "output": "partial result"}),
            ("execute_code", {"status": "error", "exit_code": 0, "output": "partial result"}),
            ("terminal", {"exit_code": 0, "error": "failed", "output": "partial result"}),
        ):
            with self.subTest(name=name, payload=payload):
                evidence = records(name, payload)
                self.assertEqual(evidence[0]["execution_status"], "error")
                self.assertTrue(all(not unit.can_support for unit in units(evidence)))

    def test_pending_empty_and_nested_envelopes_do_not_gain_source_authority(self):
        for payload in ({"status": "running", "output": "waiting"},
                        {"status": "success", "output": ""},
                        {"exit_code": 0},
                        {"status": "timeout", "output": "prefix"},
                        json.dumps({"exit_code": 1, "output": "prefix"})):
            with self.subTest(payload=payload):
                self.assertTrue(all(not unit.can_support for unit in units(records("terminal", payload))))

    def test_divider_only_regions_are_not_source_assertions(self):
        evidence = records("terminal", {"exit_code": 0, "output": "========\n========\nA real record\n========"})
        physical = units(evidence)
        self.assertEqual(len(physical), 1)
        self.assertIn("A real record", physical[0].text)

    def test_host_truncation_is_not_complete_even_when_execution_succeeds(self):
        for metadata in ({"stdout_truncated": True}, {"stdout_bytes_omitted": 2}, {"truncated": True}):
            evidence = records("execute_code", {"exit_code": 0, "status": "success",
                                                 "output": "prefix", **metadata})
            self.assertEqual(evidence[0]["execution_status"], "success")
            self.assertEqual(evidence[0]["completeness"], "partial")
            self.assertTrue(all(not unit.can_support for unit in units(evidence)))

    def test_arbitrary_application_json_is_not_mistaken_for_execution_envelope(self):
        payload = {"status": "error", "exit_code": 2, "output": "application data"}
        evidence = records("issue.read", payload)
        self.assertEqual(json.loads(evidence[0]["content"]), payload)
        self.assertEqual(evidence[0]["execution_status"], "success")
        evidence = records("terminal", {"status": {"value": "error"}, "output": "data"})
        self.assertEqual(json.loads(evidence[0]["content"])["output"], "data")

    def test_divided_oversized_legacy_text_remains_bounded_without_span_loss(self):
        body = "========\n" + "漢" * MAX_EXTERNAL_UNIT_BYTES + "\n========\nTail record"
        evidence = [{"tool_name": "external.read", "call_id": "c", "kind": "external_observation",
                     "result_status": "success", "content": body}]
        physical = units(evidence)
        self.assertEqual("".join(unit.text for unit in physical), body)
        self.assertTrue(all(len(unit.text.encode("utf-8")) <= MAX_EXTERNAL_UNIT_BYTES for unit in physical))


if __name__ == "__main__":
    unittest.main()

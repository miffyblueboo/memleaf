"""Opt-in real-model acceptance with synthetic documents and a fresh Vault.

Only the supplied config's llm section is read, in memory. No real inbox,
knowledge, native source, or attachment is read. Credentials are never copied
to the diagnostic Vault. This is excluded from automatic unittest discovery.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from memleaf import Memleaf
from memleaf.config import save_config
from memleaf.frontmatter import load_yaml
from memleaf.llm import ModelRouter


class RecordedRoute:
    def __init__(self, route: ModelRouter, output: Path):
        self.route, self.output, self.calls = route, output, 0

    def complete(self, prompt: str, **kwargs: Any) -> str:
        if self.calls >= 60:
            raise RuntimeError("synthetic acceptance call budget exhausted")
        self.calls += 1
        number = self.calls
        # Inputs and replies contain only the generated synthetic scenario.
        (self.output / f"{number}-request.json").write_text(
            json.dumps({"prompt": prompt, **kwargs}, ensure_ascii=False), encoding="utf-8")
        print(f"call {number}: {kwargs.get('purpose')}", flush=True)
        response = self.route.complete(prompt, **kwargs)
        (self.output / f"{number}-response.json").write_text(response, encoding="utf-8")
        return response


def run(output: Path, backend: RecordedRoute) -> list[dict[str, Any]]:
    core = Memleaf.initialize(output / "vault")
    config = core.vault.config()
    config["native_sources"] = {}
    config["capture"].update(tool_evidence_mode="bounded", include_attachments=False)
    config["scopes"] = {"project:Cedar": {}, "project:Birch": {}}
    save_config(core.vault.config_path, config)
    core.create_memory(memory_id="cedar-checklist", title="Cedar deployment checklist",
        body="The Cedar deployment checklist is awaiting completion.", type="todo",
        scopes=["project:Cedar"], scope_source="user", status="active")
    core.create_memory(memory_id="birch-contact", title="Birch contact",
        body="The Birch contact is Morgan.", type="fact", scopes=["project:Birch"], scope_source="user")

    today = datetime.now(timezone.utc).date()
    report_due, handover_due = (today + timedelta(days=7)).isoformat(), (today + timedelta(days=8)).isoformat()
    actions = {
        0: f"Project Cedar. Please prepare the Cedar acceptance report by {report_due}.",
        1: f"Project Cedar. The Cedar deployment checklist is now complete, confirmed on {today}.",
        2: "Project Birch. The current Birch contact is Morgan.",
        13: f"For Project Cedar, you need to finish the acceptance report by {report_due}.",
        22: f"Project Birch. Please arrange the Birch handover meeting by {handover_due}.",
    }
    records = []
    for number in range(23):
        log = "\n".join(f"run-{number:02d}-{row:03d}, check result=normal, observed value={row:03d};"
                        for row in range(56))
        records.append({"tool_name": "read_file", "call_id": f"synthetic-read-{number}",
            "record_id": f"synthetic-record-{number}", "schema_version": "2",
            "kind": "external_observation", "result_status": "success",
            "execution_status": "success", "completeness": "complete", "source_type": "document",
            "content": json.dumps({"record": number, "body": actions.get(number,
                "Routine diagnostic output only; no new project decision, assignment, or state change."),
                "diagnostic_log": log}, ensure_ascii=False)})

    def capture(number: int, *, query_only: bool = False) -> None:
        user = ("What are my current Project Cedar and Project Birch todos?" if query_only else
            "I manage Project Cedar and Project Birch. Review these source records and retain confirmed "
            "follow-up actions and state changes. Do not retain routine diagnostic logs.")
        assistant = ("Your active follow-ups are the Cedar acceptance report and Birch handover meeting. "
                     "The Cedar deployment checklist is completed." if query_only else
                     "I reviewed the supplied source records.")
        core.capture("hermes", "synthetic-lifecycle", f"turn-{number}", "user", user,
                     event_id=f"synthetic-u-{number}")
        core.capture("hermes", "synthetic-lifecycle", f"turn-{number}", "assistant", assistant,
                     event_id=f"synthetic-a-{number}", tool_evidence=None if query_only else records)

    def snapshot() -> dict[str, bytes]:
        return {str(path.relative_to(core.vault.root)): path.read_bytes()
                for area in ("knowledge", "history") for path in core.vault.list_markdown(area)}

    rows = []
    capture(1)
    for phase in ("first", "same_turn", "new_turn", "query_only"):
        if phase == "new_turn":
            capture(2)
        elif phase == "query_only":
            capture(3, query_only=True)
        calls_before, before = backend.calls, snapshot()
        result = core.process(source="hermes", session_id="synthetic-lifecycle", model=backend)
        row = {"phase": phase, "calls": backend.calls - calls_before, "result": result}
        rows.append(row)
        (output / "results.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        memories = [record.memory for record in core._read_memories_unlocked("knowledge")]
        (output / f"{phase}-memories.json").write_text(
            json.dumps([memory.to_dict() for memory in memories], ensure_ascii=False, indent=2), encoding="utf-8")
        todos = core.list_todos(status="active")
        (output / f"{phase}-todos.json").write_text(json.dumps(todos, ensure_ascii=False), encoding="utf-8")

        assert result.get("coverage_status") == "complete" and result.get("deferred_candidates", 0) == 0, \
            "confirmed synthetic evidence remains unresolved"
        assert len(memories) == 4, "expected two seeded memories and two distinct new todos"
        assert core.read("cedar-checklist").status == "completed", "existing checklist was not completed"
        cedar = [memory for memory in memories if memory.type == "todo"
                 and memory.scopes == ["project:Cedar"] and memory.memory_id != "cedar-checklist"]
        birch = [memory for memory in memories if memory.type == "todo" and memory.scopes == ["project:Birch"]]
        assert len(cedar) == 1 and cedar[0].status == "active", "Cedar report was lost, duplicated, or closed"
        assert len(birch) == 1 and birch[0].status == "active", "final-source Birch handover was lost or duplicated"
        assert cedar[0].due_date == report_due and birch[0].due_date == handover_due, "source deadlines were lost"
        assert len(todos["results"]) == 2, "public todo readback did not return both active follow-ups"
        if phase != "first":
            assert result["memories_written"] == 0 and snapshot() == before, "repeated facts or a query changed memory"
        if phase == "same_turn":
            assert backend.calls == calls_before, "already-processed turn called the model again"
        row["status"] = "pass"
        print(json.dumps({"phase": phase, "status": "pass", "calls": row["calls"]}), flush=True)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-config", type=Path, required=True,
                        help="read only llm routing from this config; never read its Vault content")
    args = parser.parse_args()
    os.umask(0o077)
    output = Path(tempfile.mkdtemp(prefix="memleaf-live-core-"))
    print(f"Synthetic acceptance output: {output}", flush=True)
    try:
        # Parse this file without validating or probing unrelated native paths.
        route_config = load_yaml(args.model_config.read_text(encoding="utf-8"))["llm"]
        backend = RecordedRoute(ModelRouter.from_config({"llm": route_config}), output)
        rows = run(output, backend)
        result = {"status": "pass", "calls": backend.calls, "phases": rows}
    except Exception as error:
        result = {"status": "fail", "error_type": type(error).__name__,
                  "validation_detail": getattr(error, "validation_detail", None),
                  "evidence_check": getattr(error, "evidence_check", None)}
        # Assertion messages are authored above; do not expose server exception text.
        if isinstance(error, AssertionError):
            result["assertion"] = str(error)
    (output / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items() if key != "phases"}), flush=True)
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Opt-in live Model Route acceptance using only synthetic, isolated Vaults.

Run with MEMLEAF_LIVE_MODEL_TOKEN, MEMLEAF_LIVE_MODEL and
MEMLEAF_LIVE_BASE_URL. This is not part of deterministic unittest discovery.
Neither fixtures nor model replies are patched to manufacture accepted output.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import time
from typing import Any

from memleaf import Memleaf
from memleaf.config import save_config
from memleaf.llm import ModelRouter
from memleaf.llm.openai_compatible import OpenAICompatibleBackend
from memleaf.provenance import observation_record

MAX_CALLS = 48


class RecordedAPI(OpenAICompatibleBackend):
    def __init__(self, output: Path):
        super().__init__(base_url=os.environ["MEMLEAF_LIVE_BASE_URL"],
            api_key=os.environ["MEMLEAF_LIVE_MODEL_TOKEN"],
            model=os.environ["MEMLEAF_LIVE_MODEL"],
            json_mode=True, timeout=60)
        self.output = output
        self.calls = 0
        self.last_call = 0.0

    def complete(self, prompt: str, *, system: str = "", purpose: str = "", temperature: float = 0.0) -> str:
        if self.calls >= MAX_CALLS:
            raise RuntimeError("Live acceptance call budget exhausted")
        # Keep requests paced; this opt-in harness has a fixed total call budget.
        time.sleep(max(0.0, 6.2 - (time.monotonic() - self.last_call)))
        self.last_call = time.monotonic()
        self.calls += 1
        response = super().complete(prompt, system=system, purpose=purpose, temperature=temperature)
        with (self.output / "synthetic-model-responses.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"call": self.calls, "purpose": purpose,
                "response": response}, ensure_ascii=False) + "\n")
        return response


def main() -> int:
    output = Path(sys.argv[1] if len(sys.argv) > 1 else "live-acceptance-results")
    output.mkdir(parents=True, exist_ok=True)
    required = ("MEMLEAF_LIVE_MODEL_TOKEN", "MEMLEAF_LIVE_BASE_URL", "MEMLEAF_LIVE_MODEL")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        (output / "summary.json").write_text(json.dumps({"status": "blocked", "reason": "missing_model_configuration", "missing_variables": missing}))
        print("Live acceptance BLOCKED: missing explicit model configuration: " + ", ".join(missing))
        return 2
    backend = RecordedAPI(output)
    router = ModelRouter(mode="api", api=backend)
    results: list[dict[str, Any]] = []
    cases = [
        ("query-imperative", "麻烦你列出我最近必须完成的事项。", "Orion 需要准备上线检查表。", "readonly", None),
        ("query-english", "Summarize my current tasks, please.", "Orion needs a launch checklist.", "readonly", None),
        ("query-project-name", "邮箱迁移项目的负责人是谁？", "负责人是 Bob。", "readonly", None),
        ("mixed-question", "Orion 的数据库已经从 MySQL 切换到 PostgreSQL，现在有什么风险？", "可以检查驱动兼容性。", "postgres", None),
        ("mixed-example", "Orion 已经正式采用 PostgreSQL。请举一个连接配置的例子。", "示例：数据库密码 demo，负责人 Alice。", "postgres", None),
        ("actual-quotation", "这是已经批准的真实项目规范，请记住：\n> Orion 生产环境只允许 JDK 17。", "收到。", "jdk", None),
        ("whole-word", "The whole Orion project now uses PostgreSQL as its production database.", "Understood.", "postgres", None),
        ("illustrative-only", "下面是假设示例，不是实际事项：Orion 需要修复两个问题。不要记录为真实待办。", "示例中可以安排两项修复。", "readonly", None),
        ("negation", "Orion 的两个模块无需修正，不需要创建修复任务。", "明白。", "no_active_todo", None),
        ("completed-followup", "Orion 上线检查表已经完成了，现在还有什么待办？", "检查表已完成。", "completed", None),
    ]
    for tool in ("calendar.events", "documents.read", "issues.get", "browser.read", "terminal.result", "mail.read"):
        cases.append(("tool-" + tool, "请查看来源中的项目规范。", "来源要求 Orion 使用 JDK 17。", "jdk", tool))
    with tempfile.TemporaryDirectory(prefix="memleaf-live-acceptance-") as temporary:
        for index, (name, user, assistant, expectation, tool) in enumerate(cases):
            before_calls = backend.calls
            service = Memleaf(Path(temporary) / str(index))
            config = service.vault.config()
            config["scopes"] = {"project:Orion": {}, "project:邮箱迁移": {}}
            save_config(service.vault.config_path, config)
            if expectation in {"readonly", "completed"} and name != "illustrative-only":
                service.create_memory(memory_id="existing-task", title="Orion 上线检查表",
                    body="Orion 上线检查表需要准备。", type="todo", scopes=["project:Orion"], status="active")
            snapshot = {p.name: p.read_bytes() for p in service.vault.list_markdown("knowledge")}
            service.capture("hermes", name, "t1", "user", user, event_id=name + "-u")
            evidence = ([observation_record(tool, name + "-call", "Orion 生产环境只允许 JDK 17。")]
                        if tool else None)
            service.capture("hermes", name, "t1", "assistant", assistant,
                event_id=name + "-a", tool_evidence=evidence)
            row: dict[str, Any] = {"case": name, "expected": expectation}
            try:
                actual = service.process(source="hermes", session_id=name, model=router)
                memories = [record.memory for record in service._read_memories_unlocked("knowledge")]
                after = {p.name: p.read_bytes() for p in service.vault.list_markdown("knowledge")}
                if expectation == "readonly":
                    passed = after == snapshot and not service.vault.list_markdown("history")
                elif expectation == "no_active_todo":
                    passed = not any(m.type == "todo" and m.status == "active" for m in memories)
                elif expectation == "completed":
                    target = service.read("existing-task")
                    passed = target is not None and target.status == "completed" and len(memories) == 1
                else:
                    token = "postgresql" if expectation == "postgres" else "jdk 17"
                    passed = any(token in (m.title + " " + m.body).casefold() and m.scopes == ["project:Orion"] for m in memories)
                    if name == "mixed-example":
                        passed = passed and all("demo" not in m.body and "Alice" not in m.body for m in memories)
                row.update(status="pass" if passed else "fail", result=actual,
                    memories=[{"id": m.memory_id, "body": m.body, "type": m.type, "status": m.status, "scopes": m.scopes} for m in memories])
            except Exception as error:
                # Exception text can contain server metadata; record only safe type/code.
                row.update(status="error", error_type=type(error).__name__, error_code=getattr(error, "code", None))
            row["model_calls"] = backend.calls - before_calls
            results.append(row)
            print(json.dumps({k: row[k] for k in ("case", "status", "model_calls")}), flush=True)
            (output / "summary.json").write_text(json.dumps({"model": backend.model,
                "cases": len(results), "calls": backend.calls, "passed": sum(r["status"] == "pass" for r in results),
                "failed": sum(r["status"] != "pass" for r in results), "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
            if backend.calls >= MAX_CALLS:
                break
    return 0 if len(results) == len(cases) and all(r["status"] == "pass" for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

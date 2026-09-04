"""Regression coverage for actionable items in a mailbox digest.

The production incident behind these tests was not a failed inbox process:
the model gate returned a valid subset of a multi-project email digest and
silently dropped two explicit Morgan Fund corrections.  These tests keep the
source turn realistic while making the gate omission deterministic, so every
actionable item must have an auditable write/update/no-op or deferred outcome.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from memleaf import Memleaf
from memleaf.config import save_config
from memleaf.index import event_key


class QueueBackend:
    """Deterministic model backend with an intentionally incomplete first gate."""

    provider = "fake"
    model = "email-actionable-coverage"

    def __init__(
        self,
        first_gate: str,
        coverage_gate: str,
        summaries: dict[str, str],
        *,
        fallback_evidence: str | None = None,
    ):
        self.first_gate = first_gate
        self.coverage_gate = coverage_gate
        self.summaries = dict(summaries)
        self.fallback_evidence = fallback_evidence
        self.gate_calls = 0
        self.calls: list[dict[str, str]] = []

    def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        purpose: str = "",
        temperature: float = 0.0,
    ) -> str:
        del system, temperature
        self.calls.append({"prompt": prompt, "purpose": purpose})
        if purpose == "gate":
            self.gate_calls += 1
            # The first answer deliberately omits both detailed Morgan
            # corrections.  A coverage/retry implementation may ask again;
            # the historical implementation accepts the first valid gate and
            # never sees this second response.
            return self.first_gate if self.gate_calls == 1 else self.coverage_gate
        if purpose == "summarize":
            candidate_text = prompt
            if "Candidate:\n" in prompt:
                candidate_text = prompt.split("Candidate:\n", 1)[1]
                candidate_text = candidate_text.split("\nEvidence", 1)[0]
            for marker, response in self.summaries.items():
                if marker in candidate_text:
                    return response
            # Current production recovery recognizes the aggregate Morgan
            # sentence but does not yet recover its two detailed actions.  A
            # deterministic summary keeps this regression at the persistence
            # assertion, where the silent omission is observable, instead of
            # turning it into an unrelated test-backend failure.
            if "两个需修正的问题" in candidate_text:
                evidence = self.fallback_evidence
                if evidence is None:
                    first_summary = next(iter(self.summaries.values()), "{}")
                    evidence = json.loads(first_summary)["sources"][0]["event_key"]
                return summary(
                    evidence,
                    title="摩根基金投资申报系统问题",
                    body="摩根基金投资申报系统还有两个需修正的问题。",
                    scopes=["project:摩根基金"],
                )
            raise AssertionError(f"no deterministic summary for prompt: {prompt[:240]}")
        raise AssertionError(f"unexpected model purpose: {purpose}")


def gate(candidates: list[dict[str, object]]) -> str:
    return json.dumps({"candidates": candidates}, ensure_ascii=False)


def candidate(
    candidate_id: str,
    evidence: list[str],
    *,
    memory: str,
    type: str | None,
    scopes: list[str],
    worth: bool = True,
    duplicate: bool = False,
    update_memory_id: str | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "candidate_id": candidate_id,
        "memory": memory,
        "evidence_event_ids": list(evidence),
        "duplicate": duplicate,
        "worth": worth,
        "type": type,
        "scopes": list(scopes),
        "scope_source": "model",
    }
    if update_memory_id is not None:
        value["update_memory_id"] = update_memory_id
    return value


def summary(
    evidence: str,
    *,
    title: str,
    body: str,
    scopes: list[str],
    update_memory_id: str | None = None,
    scope_source: str = "model",
) -> str:
    value: dict[str, object] = {
        "title": title,
        "body": body,
        "tags": ["mailbox", "actionable"],
        "type": "todo",
        "scopes": list(scopes),
        "scope_source": scope_source,
        "sources": [{"event_key": evidence}],
        "status": "active",
    }
    if update_memory_id is not None:
        value["update_memory_id"] = update_memory_id
    return json.dumps(value, ensure_ascii=False)


class EmailActionableCoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="memleaf-email-actionable-")
        self.addCleanup(temporary.cleanup)
        self.vault = Path(temporary.name) / "vault"
        self.service = Memleaf(self.vault)
        config = self.service.vault.config()
        config["scopes"] = {
            "project:中银国际": {"aliases": []},
            "project:金元顺安": {"aliases": []},
            "project:摩根基金": {"aliases": ["摩根"]},
            "project:安联基金": {"aliases": []},
        }
        save_config(self.service.vault.config_path, config)

    def capture_mail_digest(self, session: str) -> tuple[str, str]:
        user_id = f"{session}-user"
        assistant_id = f"{session}-assistant"
        self.service.capture(
            "hermes",
            session,
            "turn-1",
            "user",
            "请把邮箱里需要关注的项目事项整理成待办",
            event_id=user_id,
        )
        assistant = """
邮箱巡检结果：

1. 中银国际：请提供项目干系人信息表和外部人员入场材料。
2. 金元顺安：实施计划需要按客户意见重排后再回复。
3. 摩根基金：投资申报系统 XC 版还有两个需修正的问题：
   - 撤单场景校验规则需补充；
   - 历史数据迁移字段映射需修正。
4. 安联基金：日报和上线后汇总，仅供参考，无具体动作。
""".strip()
        self.service.capture(
            "hermes",
            session,
            "turn-1",
            "assistant",
            assistant,
            event_id=assistant_id,
        )
        return event_key(user_id), event_key(assistant_id)

    def processed_entry(self, session: str) -> dict[str, object]:
        value = json.loads(self.service.vault.processed_index_path.read_text(encoding="utf-8"))
        return value["sessions"][f"hermes/{session}"]["processed_turns"][0]

    def active_memories(self):
        return [record.memory for record in self.service._read_memories_unlocked("knowledge")]

    def test_omitted_morgan_corrections_get_dispositions_and_reuse_existing_item(self) -> None:
        """A valid partial gate must not silently discard explicit actions."""

        user_key, assistant_key = self.capture_mail_digest("mailbox-coverage")
        morgan_scope = ["project:摩根基金"]
        existing = self.service.create_memory(
            memory_id="morgan-existing-correction",
            title="摩根基金撤单场景校验规则",
            body="摩根基金撤单场景校验规则旧版已提交，待客户确认。",
            tags=["mailbox"],
            type="todo",
            scopes=morgan_scope,
            status="active",
        )

        first_gate = gate(
            [
                candidate(
                    "bankin-stakeholders",
                    [user_key, assistant_key],
                    memory="中银国际项目干系人信息表和外部人员入场材料需要提供。",
                    type="todo",
                    scopes=["project:中银国际"],
                ),
                candidate(
                    "jinyuan-plan",
                    [user_key, assistant_key],
                    memory="金元顺安实施计划需要按客户意见重排后回复。",
                    type="todo",
                    scopes=["project:金元顺安"],
                ),
                # The aggregate shell is not itself a reusable memory.  Its
                # detailed Morgan actions remain in the source event.
                candidate(
                    "morgan-shell",
                    [user_key, assistant_key],
                    memory="摩根基金投资申报系统还有两个需修正的问题。",
                    type="fact",
                    scopes=morgan_scope,
                    worth=False,
                ),
                candidate(
                    "allianz-background",
                    [user_key, assistant_key],
                    memory="安联基金日报和上线后汇总仅供参考，无具体动作。",
                    type="fact",
                    scopes=["project:安联基金"],
                    worth=False,
                ),
            ]
        )
        coverage_gate = gate(
            [
                candidate(
                    "morgan-withdrawal-correction",
                    [user_key, assistant_key],
                    memory="摩根基金撤单场景校验规则需要补充并修正。",
                    type="todo",
                    scopes=morgan_scope,
                    update_memory_id=existing.memory_id,
                ),
                candidate(
                    "morgan-history-mapping-correction",
                    [user_key, assistant_key],
                    memory="摩根基金历史数据迁移字段映射需要修正。",
                    type="todo",
                    scopes=morgan_scope,
                ),
            ]
        )
        backend = QueueBackend(
            first_gate,
            coverage_gate,
            {
                "中银国际项目干系人信息表": summary(
                    user_key,
                    title="中银国际干系人信息表和入场材料",
                    body="中银国际项目干系人信息表和外部人员入场材料需要提供。",
                    scopes=["project:中银国际"],
                ),
                "金元顺安实施计划": summary(
                    user_key,
                    title="金元顺安实施计划重排",
                    body="金元顺安实施计划需要按客户意见重排后回复。",
                    scopes=["project:金元顺安"],
                ),
                "撤单场景校验规则": summary(
                    user_key,
                    title="摩根基金撤单场景校验规则",
                    body="摩根基金撤单场景校验规则需要补充并修正。",
                    scopes=morgan_scope,
                    update_memory_id=existing.memory_id,
                ),
                "历史数据迁移字段映射": summary(
                    user_key,
                    title="摩根基金历史数据迁移字段映射",
                    body="摩根基金历史数据迁移字段映射需要修正。",
                    scopes=morgan_scope,
                ),
            },
            fallback_evidence=user_key,
        )

        result = self.service.process(
            source="hermes",
            session_id="mailbox-coverage",
            model=backend,
        )

        # The existing Morgan item must be updated in place, never duplicated
        # under another ID or another project scope.
        self.assertIn(existing.memory_id, result["memory_ids"])
        current_existing = self.service.read(existing.memory_id)
        self.assertIsNotNone(current_existing)
        self.assertIn("需要补充并修正", current_existing.body)
        morgan_current = [
            memory
            for memory in self.active_memories()
            if "摩根基金" in f"{memory.title}\n{memory.body}"
        ]
        self.assertEqual([memory.memory_id for memory in morgan_current].count(existing.memory_id), 1)
        self.assertTrue(
            all(memory.scopes == morgan_scope for memory in morgan_current),
            "a Morgan correction was persisted under a different project scope",
        )

        # Every detailed Morgan action must be either present as its own
        # active memory or named in the deferred ledger.  Absence from both is
        # the exact historical silent-omission defect.
        entry = self.processed_entry("mailbox-coverage")
        dispositions = entry.get("candidate_dispositions", [])
        self.assertTrue(
            any(
                item.get("disposition") == "UPDATE"
                and item.get("memory_id") == existing.memory_id
                for item in dispositions
            ),
            "the existing Morgan correction must have an auditable UPDATE outcome",
        )
        deferred = {
            item["candidate_id"]: item
            for item in entry.get("deferred_candidates", [])
            if isinstance(item, dict) and isinstance(item.get("candidate_id"), str)
        }
        history_mapping = [
            memory
            for memory in self.active_memories()
            if "历史数据迁移字段映射" in f"{memory.title}\n{memory.body}"
        ]
        if not history_mapping:
            self.assertIn("morgan-history-mapping-correction", deferred)
            self.assertTrue(deferred["morgan-history-mapping-correction"].get("reason"))
        else:
            self.assertEqual(len(history_mapping), 1)
            self.assertEqual(history_mapping[0].scopes, morgan_scope)
            self.assertEqual(history_mapping[0].type, "todo")
            self.assertTrue(
                any(
                    item.get("disposition") == "CREATE"
                    and item.get("memory_id") == history_mapping[0].memory_id
                    for item in dispositions
                ),
                "the second Morgan correction must have an auditable CREATE outcome",
            )

        # Background mail is not a business action and must not be promoted.
        self.assertFalse(
            any(
                "安联基金日报" in f"{memory.title}\n{memory.body}"
                for memory in self.active_memories()
            )
        )

if __name__ == "__main__":
    unittest.main()

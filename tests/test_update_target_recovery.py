"""Regression tests for safe recovery from invalid update targets.

The production failure that motivated these tests was a gate candidate whose
type did not match the type of its selected ``update_memory_id``.  A bounded
retry must not make an unrelated target block an otherwise useful turn, but
it also must not turn a genuinely same-use type conflict into a duplicate
memory.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from memleaf import Memleaf
from memleaf.config import save_config
from memleaf.index import event_key


from tests.semantic_fixtures import semantic_fixture

@semantic_fixture
class QueueBackend:
    provider = "fake"
    model = "update-target-recovery"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict[str, str]] = []

    def complete(self, prompt, *, system="", purpose="", temperature=0.0):
        del temperature
        self.calls.append({"prompt": prompt, "system": system, "purpose": purpose})
        if not self.responses:
            raise AssertionError("test backend response queue exhausted")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def gate(candidates):
    return json.dumps({"candidates": candidates}, ensure_ascii=False)


def candidate(
    candidate_id,
    evidence,
    *,
    memory,
    type,
    scopes,
    update_memory_id=None,
    worth=True,
    duplicate=False,
    duplicate_memory_id=None,
    scope_source="model",
):
    value = {
        "candidate_id": candidate_id,
        "memory": memory,
        "evidence_event_ids": list(evidence),
        "duplicate": duplicate,
        "worth": worth,
        "type": type,
        "scopes": list(scopes),
        "scope_source": scope_source,
    }
    if update_memory_id is not None:
        value["update_memory_id"] = update_memory_id
    if duplicate_memory_id is not None:
        value["duplicate_memory_id"] = duplicate_memory_id
    return value


def summary(event, *, title, body, type, scopes, update_memory_id=None):
    value = {
        "title": title,
        "body": body,
        "tags": ["update-target-recovery"],
        "type": type,
        "scopes": list(scopes),
        "scope_source": "model",
        "sources": [{"event_key": event}],
    }
    if update_memory_id is not None:
        value["update_memory_id"] = update_memory_id
    return json.dumps(value, ensure_ascii=False)


def no_change():
    return json.dumps({"decision": "NO_CHANGE"})


class UpdateTargetRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(prefix="memleaf-update-target-")
        self.service = Memleaf(Path(self.tempdir.name) / "vault")
        config = self.service.vault.config()
        config["scopes"] = {
            "project:alpha": {"aliases": ["alpha"]},
            "project:beta": {"aliases": ["beta"]},
            "project:gamma": {"aliases": ["gamma"]},
        }
        save_config(self.service.vault.config_path, config)

    def tearDown(self):
        self.tempdir.cleanup()

    def capture_turn(self, session, *, user, assistant):
        user_id = f"{session}-user"
        assistant_id = f"{session}-assistant"
        self.service.capture(
            "hermes", session, "turn-1", "user", user, event_id=user_id
        )
        self.service.capture(
            "hermes", session, "turn-1", "assistant", assistant, event_id=assistant_id
        )
        return event_key(user_id), event_key(assistant_id)

    def active(self):
        return [record.memory for record in self.service._read_memories_unlocked("knowledge")]

    def inbox_exists(self, session):
        return (self.service.vault.inbox_path / "hermes" / f"{session}.md").is_file()

    def test_final_retry_splits_grounded_multi_project_aggregate_per_candidate(self):
        """One bad aggregate must not block independently grounded projects."""

        user_key, assistant_key = self.capture_turn(
            "multi-project-aggregate",
            user=(
                "alpha 项目实施计划新增提前部署测试环境要求；"
                "beta 项目需补充网络拓扑图；"
                "gamma 负责人已确认由乙负责。"
            ),
            assistant="已按项目分别整理本轮事项。",
        )
        aggregate = candidate(
            "multi-project-aggregate",
            [user_key, assistant_key],
            memory=(
                "alpha 项目实施计划新增提前部署测试环境要求；"
                "beta 项目需补充网络拓扑图；"
                "gamma 负责人已确认由乙负责。"
            ),
            type="fact",
            scopes=["project:alpha"],
        )
        backend = QueueBackend(
            [
                gate([aggregate]),
                gate([aggregate]),
                gate([aggregate]),
                summary(
                    user_key,
                    title="alpha 项目实施计划",
                    body="alpha 项目实施计划新增提前部署测试环境要求。",
                    type="project",
                    scopes=["project:alpha"],
                ),
                summary(
                    user_key,
                    title="beta 网络拓扑图",
                    body="beta 项目需补充网络拓扑图。",
                    type="todo",
                    scopes=["project:beta"],
                ),
                summary(
                    user_key,
                    title="gamma 负责人",
                    body="gamma 负责人已确认由乙负责。",
                    type="fact",
                    scopes=["project:gamma"],
                ),
            ]
        )

        result = self.service.process(
            source="hermes", session_id="multi-project-aggregate", model=backend
        )

        self.assertEqual(result["memories_written"], 3)
        memories = self.active()
        self.assertEqual(
            {memory.scopes[0] for memory in memories},
            {"project:alpha", "project:beta", "project:gamma"},
        )
        self.assertEqual(result["deferred_candidates"], 0)
        self.assertEqual(
            [call["purpose"] for call in backend.calls],
            ["gate", "gate", "gate", "summarize", "summarize", "summarize"],
        )

    def test_first_valid_insufficient_context_aggregate_is_split(self):
        """A valid unscoped aggregate is split without spending gate retries."""

        user_key, assistant_key = self.capture_turn(
            "first-response-aggregate",
            user=(
                "alpha 负责人当前为甲；"
                "beta 负责人当前为乙；"
                "gamma 负责人当前为丙。"
            ),
            assistant="已按项目分别整理本轮事项。",
        )
        aggregate = candidate(
            "first-response-aggregate",
            [user_key, assistant_key],
            memory=(
                "alpha 负责人当前为甲；"
                "beta 负责人当前为乙；"
                "gamma 负责人当前为丙。"
            ),
            type="fact",
            scopes=["unscoped"],
            scope_source="insufficient_context",
        )
        backend = QueueBackend(
            [
                gate([aggregate]),
                summary(
                    user_key,
                    title="alpha 负责人",
                    body="alpha 负责人当前为甲。",
                    type="fact",
                    scopes=["project:alpha"],
                ),
                summary(
                    user_key,
                    title="beta 负责人",
                    body="beta 负责人当前为乙。",
                    type="fact",
                    scopes=["project:beta"],
                ),
                summary(
                    user_key,
                    title="gamma 负责人",
                    body="gamma 负责人当前为丙。",
                    type="fact",
                    scopes=["project:gamma"],
                ),
            ]
        )

        result = self.service.process(
            source="hermes", session_id="first-response-aggregate", model=backend
        )

        self.assertEqual(result["memories_written"], 3)
        self.assertEqual(result["deferred_candidates"], 0)
        self.assertEqual(
            {memory.scopes[0] for memory in self.active()},
            {"project:alpha", "project:beta", "project:gamma"},
        )
        self.assertEqual(
            [call["purpose"] for call in backend.calls],
            ["gate", "summarize", "summarize", "summarize"],
        )

    def test_unrelated_fact_target_is_removed_and_project_plan_is_created(self):
        """A plan aimed at an unrelated fact survives three gate retries as CREATE."""

        old = self.service.create_memory(
            memory_id="mem-beta-fact",
            title="beta 项目负责人",
            body="beta 项目负责人仍为丙。",
            type="fact",
            scopes=["project:beta"],
        )
        user_key, assistant_key = self.capture_turn(
            "project-plan-wrong-fact",
            user="beta 项目负责人仍为丙；alpha 项目实施计划新增提前部署测试环境要求。",
            assistant="已确认 beta 项目负责人未变，alpha 实施计划有新的交付约束。",
        )
        wrong_target = candidate(
            "alpha-plan-wrong-fact",
            [user_key, assistant_key],
            memory="alpha 项目实施计划新增提前部署测试环境要求。",
            type="project",
            scopes=["project:alpha"],
            update_memory_id=old.memory_id,
        )
        backend = QueueBackend(
            [
                gate([wrong_target]),
                gate([wrong_target]),
                gate([wrong_target]),
                summary(
                    user_key,
                    title="alpha 项目实施计划",
                    body="alpha 项目实施计划新增提前部署测试环境要求。",
                    type="project",
                    scopes=["project:alpha"],
                ),
            ]
        )

        result = self.service.process(
            source="hermes", session_id="project-plan-wrong-fact", model=backend
        )

        memories = self.active()
        self.assertEqual(result["memories_written"], 1)
        self.assertEqual(
            [call["purpose"] for call in backend.calls],
            ["gate", "gate", "gate", "summarize"],
        )
        self.assertEqual(self.service.read(old.memory_id).body, old.body)
        created = [memory for memory in memories if memory.memory_id != old.memory_id]
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].type, "project")
        self.assertIn("实施计划", created[0].body)
        self.assertEqual(self.service.vault.list_markdown("history"), [])

    def test_unrelated_project_target_is_removed_and_fact_is_created(self):
        """A fact aimed at an unrelated project target also becomes an independent CREATE."""

        old = self.service.create_memory(
            memory_id="mem-beta-plan",
            title="beta 项目实施计划",
            body="beta 项目实施计划按原安排执行。",
            type="project",
            scopes=["project:beta"],
        )
        user_key, assistant_key = self.capture_turn(
            "fact-wrong-project",
            user="beta 项目实施计划仍按原安排；alpha 项目负责人已确认由乙负责。",
            assistant="已记录 beta 计划未变，以及 alpha 项目负责人变更。",
        )
        wrong_target = candidate(
            "alpha-owner-wrong-project",
            [user_key, assistant_key],
            memory="alpha 项目负责人已确认由乙负责。",
            type="fact",
            scopes=["project:alpha"],
            update_memory_id=old.memory_id,
        )
        backend = QueueBackend(
            [
                gate([wrong_target]),
                gate([wrong_target]),
                gate([wrong_target]),
                summary(
                    user_key,
                    title="alpha 项目负责人",
                    body="alpha 项目负责人已确认由乙负责。",
                    type="fact",
                    scopes=["project:alpha"],
                ),
            ]
        )

        result = self.service.process(
            source="hermes", session_id="fact-wrong-project", model=backend
        )

        memories = self.active()
        self.assertEqual(result["memories_written"], 1)
        self.assertEqual(self.service.read(old.memory_id).body, old.body)
        created = [memory for memory in memories if memory.memory_id != old.memory_id]
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].type, "fact")
        self.assertIn("负责人", created[0].body)
        self.assertEqual(self.service.vault.list_markdown("history"), [])

    def test_same_project_plan_detaches_orion_sync_fact_target_and_creates_project(self):
        """A plan must not overwrite a same-scope Orion synchronization fact."""

        config = self.service.vault.config()
        config["scopes"]["project:金元顺安"] = {"aliases": ["金元顺安"]}
        save_config(self.service.vault.config_path, config)
        old = self.service.create_memory(
            memory_id="mem-jinyuan-orion-sync",
            title="任务同步到 Orion",
            body="金元顺安任务已同步到 Orion。",
            type="fact",
            scopes=["project:金元顺安"],
        )
        user_key, assistant_key = self.capture_turn(
            "jinyuan-plan-wrong-orion-fact",
            user="金元顺安实施计划新增信创测试环境部署要求。",
            assistant="已确认金元顺安实施计划需提前部署测试环境。",
        )
        wrong_target = candidate(
            "jinyuan-plan-wrong-orion-fact",
            [user_key, assistant_key],
            memory="金元顺安实施计划新增信创测试环境部署要求。",
            type="project",
            scopes=["project:金元顺安"],
            update_memory_id=old.memory_id,
        )
        backend = QueueBackend(
            [
                gate([wrong_target]),
                gate([wrong_target]),
                gate([wrong_target]),
                summary(
                    user_key,
                    title="金元顺安信创实施计划",
                    body="金元顺安实施计划新增信创测试环境部署要求。",
                    type="project",
                    scopes=["project:金元顺安"],
                ),
            ]
        )

        result = self.service.process(
            source="hermes",
            session_id="jinyuan-plan-wrong-orion-fact",
            model=backend,
            scope="project:金元顺安",
        )

        self.assertEqual(result["memories_written"], 1)
        self.assertEqual(
            [call["purpose"] for call in backend.calls],
            ["gate", "gate", "gate", "summarize"],
        )
        current = self.service.read(old.memory_id)
        self.assertEqual(current.type, "fact")
        self.assertEqual(current.body, old.body)
        self.assertEqual(self.service.vault.list_markdown("history"), [])
        created = [memory for memory in self.active() if memory.memory_id != old.memory_id]
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].type, "project")
        self.assertEqual(created[0].scopes, ["project:金元顺安"])
        self.assertIn("信创测试环境", created[0].body)

    def test_authoritative_scope_type_mismatch_does_not_detach_or_create(self):
        """User/session scope keeps an explicit type-conflict target retryable."""

        for scope_source in ("session_context", "user"):
            with self.subTest(scope_source=scope_source):
                service = Memleaf(Path(self.tempdir.name) / f"authoritative-{scope_source}")
                config = service.vault.config()
                config["scopes"] = {
                    "project:alpha": {"aliases": ["alpha"]},
                }
                save_config(service.vault.config_path, config)
                old = service.create_memory(
                    memory_id=f"mem-authoritative-{scope_source}",
                    title="alpha 项目负责人",
                    body="alpha 项目负责人仍为丙。",
                    type="fact",
                    scopes=["project:alpha"],
                )
                user_id = f"authoritative-{scope_source}-user"
                assistant_id = f"authoritative-{scope_source}-assistant"
                service.capture(
                    "hermes",
                    f"authoritative-{scope_source}",
                    "turn-1",
                    "user",
                    "alpha 项目负责人仍为丙；alpha 项目接口性能需重新确认。",
                    event_id=user_id,
                )
                service.capture(
                    "hermes",
                    f"authoritative-{scope_source}",
                    "turn-1",
                    "assistant",
                    "已确认负责人未变，接口事项待继续确认。",
                    event_id=assistant_id,
                )
                user_key = event_key(user_id)
                assistant_key = event_key(assistant_id)
                wrong_type = candidate(
                    f"authoritative-{scope_source}-candidate",
                    [user_key, assistant_key],
                    memory="alpha 项目接口性能需重新确认。",
                    type="project",
                    scopes=["project:alpha"],
                    update_memory_id=old.memory_id,
                    scope_source=scope_source,
                )
                backend = QueueBackend([gate([wrong_type])] * 3)

                result = service.process(
                    source="hermes",
                    session_id=f"authoritative-{scope_source}",
                    model=backend,
                )

                self.assertEqual(result["processed_turns"], 1)
                self.assertEqual(result["deferred_candidates"], 1)
                self.assertEqual(result["memories_written"], 0)
                self.assertEqual(result["memory_ids"], [])
                self.assertEqual(
                    [call["purpose"] for call in backend.calls], ["gate"] * 3
                )
                self.assertEqual(service.read(old.memory_id).body, old.body)
                self.assertEqual(
                    len(service._read_memories_unlocked("knowledge")), 1
                )
                self.assertEqual(service.vault.list_markdown("history"), [])
                processed = json.loads(
                    service.vault.processed_index_path.read_text(encoding="utf-8")
                )
                entry = processed["sessions"][
                    f"hermes/authoritative-{scope_source}"
                ]["processed_turns"][0]
                self.assertEqual(
                    entry["deferred_candidates"][0]["reason"],
                    "update_target_type_mismatch",
                )
                self.assertTrue(
                    (
                        service.vault.inbox_path
                        / "hermes"
                        / f"authoritative-{scope_source}.md"
                    ).is_file()
                )

    def test_detached_wrong_target_reuses_unique_same_use_project_target(self):
        """After detaching a wrong fact, a unique plan target is still updated."""

        wrong_target = self.service.create_memory(
            memory_id="mem-alpha-orion-fact",
            title="alpha 任务同步到 Orion",
            body="alpha 任务已同步到 Orion。",
            type="fact",
            scopes=["project:alpha"],
        )
        correct_target = self.service.create_memory(
            memory_id="mem-alpha-plan-correct",
            title="alpha 项目实施计划",
            body="alpha 项目实施计划按原安排执行。",
            type="project",
            scopes=["project:alpha"],
        )
        user_key, assistant_key = self.capture_turn(
            "detach-to-unique-plan",
            user=(
                "alpha 任务同步到 Orion 已完成；alpha 项目实施计划新增"
                "信创测试环境部署要求。"
            ),
            assistant="已确认同步事项完成，实施计划需要按新增要求更新。",
        )
        wrong = candidate(
            "alpha-plan-wrong-orion-target",
            [user_key, assistant_key],
            memory="alpha 项目实施计划新增信创测试环境部署要求。",
            type="project",
            scopes=["project:alpha"],
            update_memory_id=wrong_target.memory_id,
        )
        backend = QueueBackend(
            [
                gate([wrong]),
                gate([wrong]),
                gate([wrong]),
                summary(
                    user_key,
                    title="alpha 项目实施计划",
                    body="alpha 项目实施计划新增信创测试环境部署要求。",
                    type="project",
                    scopes=["project:alpha"],
                    update_memory_id=correct_target.memory_id,
                ),
            ]
        )

        result = self.service.process(
            source="hermes", session_id="detach-to-unique-plan", model=backend
        )

        self.assertEqual(result["memory_ids"], [correct_target.memory_id])
        self.assertEqual(result["memories_written"], 1)
        self.assertEqual(self.service.read(wrong_target.memory_id).body, wrong_target.body)
        self.assertIn("信创测试环境", self.service.read(correct_target.memory_id).body)
        self.assertEqual(len(self.active()), 2)
        history = self.service.vault.list_markdown("history")
        self.assertEqual(len(history), 1)
        self.assertEqual(
            self.service._read_memories_unlocked("history")[0].memory.extra.get(
                "active_memory_id"
            ),
            correct_target.memory_id,
        )

    def test_detached_wrong_target_with_multiple_valid_plans_is_deferred(self):
        """A detached candidate must not guess when two same-use plans remain."""

        wrong_target = self.service.create_memory(
            memory_id="mem-alpha-orion-ambiguous",
            title="alpha 任务同步到 Orion",
            body="alpha 任务已同步到 Orion。",
            type="fact",
            scopes=["project:alpha"],
        )
        first_plan = self.service.create_memory(
            memory_id="mem-alpha-plan-test",
            title="alpha 项目实施计划：测试环境",
            body="alpha 测试环境实施计划按原安排执行。",
            type="project",
            scopes=["project:alpha"],
        )
        second_plan = self.service.create_memory(
            memory_id="mem-alpha-plan-release",
            title="alpha 项目实施计划：上线安排",
            body="alpha 上线实施计划按原安排执行。",
            type="project",
            scopes=["project:alpha"],
        )
        user_key, assistant_key = self.capture_turn(
            "detach-ambiguous-plans",
            user="alpha 任务同步到 Orion 已完成；alpha 项目实施计划需要更新。",
            assistant="已确认有多个实施计划可能需要调整。",
        )
        wrong = candidate(
            "alpha-plan-ambiguous-after-detach",
            [user_key, assistant_key],
            memory="alpha 项目实施计划需要更新。",
            type="project",
            scopes=["project:alpha"],
            update_memory_id=wrong_target.memory_id,
        )
        backend = QueueBackend([gate([wrong])] * 3)

        result = self.service.process(
            source="hermes", session_id="detach-ambiguous-plans", model=backend
        )

        self.assertEqual(result["processed_turns"], 1)
        self.assertEqual(result["memories_written"], 0)
        self.assertEqual(result["memory_ids"], [])
        self.assertEqual(result["deferred_candidates"], 1)
        self.assertEqual(
            [call["purpose"] for call in backend.calls], ["gate"] * 3
        )
        self.assertEqual(
            self.service.read(wrong_target.memory_id).body, wrong_target.body
        )
        self.assertEqual(self.service.read(first_plan.memory_id).body, first_plan.body)
        self.assertEqual(self.service.read(second_plan.memory_id).body, second_plan.body)
        self.assertEqual(self.service.vault.list_markdown("history"), [])
        processed = json.loads(
            self.service.vault.processed_index_path.read_text(encoding="utf-8")
        )
        entry = processed["sessions"]["hermes/detach-ambiguous-plans"]["processed_turns"][0]
        self.assertEqual(entry["deferred_candidates"][0]["reason"], "ambiguous_update_target")
        self.assertTrue(self.inbox_exists("detach-ambiguous-plans"))

    def test_adjacent_sent_mail_is_skipped_in_favor_of_formal_project_plan(self):
        """A sent-plan mail record must not steal an update from the formal plan."""

        adjacent = self.service.create_memory(
            memory_id="mem-alpha-sent-plan-mail",
            title="alpha 已发送实施方案邮件",
            body="alpha 实施方案邮件已发送给客户。",
            type="project",
            scopes=["project:alpha"],
        )
        formal = self.service.create_memory(
            memory_id="mem-alpha-formal-plan",
            title="alpha 实施计划",
            body="alpha 实施计划按原安排执行。",
            type="project",
            scopes=["project:alpha"],
        )
        user_key, assistant_key = self.capture_turn(
            "adjacent-plan-selection",
            user="alpha 已发送实施方案邮件；alpha 实施计划新增信创测试环境要求。",
            assistant="邮件已归档，正式实施计划需要更新。",
        )
        plan_candidate = candidate(
            "alpha-formal-plan-update",
            [user_key, assistant_key],
            memory="alpha 实施计划新增信创测试环境要求。",
            type="project",
            scopes=["project:alpha"],
        )
        backend = QueueBackend(
            [
                gate([plan_candidate]),
                summary(
                    user_key,
                    title="alpha 实施计划",
                    body="alpha 实施计划新增信创测试环境要求。",
                    type="project",
                    scopes=["project:alpha"],
                    update_memory_id=formal.memory_id,
                ),
            ]
        )

        result = self.service.process(
            source="hermes", session_id="adjacent-plan-selection", model=backend
        )

        self.assertEqual(result["memory_ids"], [formal.memory_id])
        self.assertEqual(result["memories_written"], 1)
        self.assertEqual(self.service.read(adjacent.memory_id).body, adjacent.body)
        self.assertIn("信创测试环境", self.service.read(formal.memory_id).body)
        self.assertEqual(len(self.active()), 2)
        self.assertEqual(len(self.service.vault.list_markdown("history")), 1)
        self.assertEqual(
            self.service._read_memories_unlocked("history")[0].memory.extra.get(
                "active_memory_id"
            ),
            formal.memory_id,
        )

    def test_mismatch_and_legal_duplicate_share_a_round_without_blocking_duplicate(self):
        """A mismatch must not prevent a legal duplicate no-op in the same turn."""

        wrong_target = self.service.create_memory(
            memory_id="mem-beta-mismatch-target",
            title="beta 项目负责人",
            body="beta 项目负责人仍为丙。",
            type="fact",
            scopes=["project:beta"],
        )
        duplicate_target = self.service.create_memory(
            memory_id="mem-alpha-duplicate-target",
            title="alpha 项目状态",
            body="alpha 项目状态已记录。",
            type="fact",
            scopes=["project:alpha"],
        )
        user_key, assistant_key = self.capture_turn(
            "mismatch-with-duplicate",
            user="beta 项目负责人仍为丙；alpha 项目状态已记录，无新增变化。",
            assistant="beta 事项产生了错误关联，alpha 状态确认是重复记录。",
        )
        mismatch = candidate(
            "alpha-plan-wrong-beta-target",
            [user_key, assistant_key],
            memory="alpha 项目实施计划新增测试环境要求。",
            type="project",
            scopes=["project:alpha"],
            update_memory_id=wrong_target.memory_id,
        )
        duplicate = candidate(
            "alpha-duplicate-status",
            [user_key, assistant_key],
            memory="alpha 项目状态已记录，无新增变化。",
            type="fact",
            scopes=["project:alpha"],
            worth=False,
            duplicate=True,
            duplicate_memory_id=duplicate_target.memory_id,
        )
        backend = QueueBackend(
            [
                gate([mismatch, duplicate]),
                gate([mismatch, duplicate]),
                gate([mismatch, duplicate]),
                summary(
                    user_key,
                    title="alpha 项目实施计划",
                    body="alpha 项目实施计划新增测试环境要求。",
                    type="project",
                    scopes=["project:alpha"],
                ),
            ]
        )

        result = self.service.process(
            source="hermes", session_id="mismatch-with-duplicate", model=backend
        )

        self.assertEqual(result["memories_written"], 1)
        self.assertEqual(len(result["memory_ids"]), 1)
        self.assertEqual(self.service.read(wrong_target.memory_id).body, wrong_target.body)
        self.assertEqual(
            self.service.read(duplicate_target.memory_id).body,
            duplicate_target.body,
        )
        created = [
            memory
            for memory in self.active()
            if memory.memory_id not in {wrong_target.memory_id, duplicate_target.memory_id}
        ]
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].type, "project")
        self.assertIn("测试环境", created[0].body)
        self.assertEqual(self.service.vault.list_markdown("history"), [])

    def test_model_scope_membership_precedes_elliptical_wrong_beta_target(self):
        """An alpha-scoped candidate must not update beta through "this project" wording."""

        beta_target = self.service.create_memory(
            memory_id="mem-beta-plan-elliptical",
            title="beta 项目实施计划",
            body="beta 项目实施计划按原安排执行。",
            type="project",
            scopes=["project:beta"],
        )
        user_key, assistant_key = self.capture_turn(
            "elliptical-alpha-wrong-beta",
            user="beta 项目当前情况已确认；该项目实施计划新增测试环境要求。",
            assistant="已确认该项目实施计划需要更新。",
        )
        wrong_scope_target = candidate(
            "alpha-scope-wrong-beta-target",
            [user_key, assistant_key],
            memory="该项目实施计划新增测试环境要求。",
            type="project",
            scopes=["project:alpha"],
            update_memory_id=beta_target.memory_id,
        )
        backend = QueueBackend([gate([wrong_scope_target])] * 3)

        result = self.service.process(
            source="hermes",
            session_id="elliptical-alpha-wrong-beta",
            model=backend,
        )

        self.assertEqual(result["processed_turns"], 1)
        self.assertEqual(result["memories_written"], 0)
        self.assertEqual(result["memory_ids"], [])
        self.assertEqual(result["deferred_candidates"], 1)
        self.assertEqual(
            [call["purpose"] for call in backend.calls], ["gate"] * 3
        )
        self.assertEqual(self.service.read(beta_target.memory_id).body, beta_target.body)
        self.assertEqual(self.service.read(beta_target.memory_id).type, "project")
        self.assertEqual(self.service.vault.list_markdown("history"), [])
        self.assertEqual(len(self.active()), 1)
        self.assertTrue(self.inbox_exists("elliptical-alpha-wrong-beta"))

    def test_same_use_type_conflict_never_overwrites_or_duplicates(self):
        """A same-topic type conflict remains zero-write instead of creating a sibling."""

        old = self.service.create_memory(
            memory_id="mem-alpha-owner",
            title="alpha 项目负责人",
            body="alpha 项目负责人仍为丙。",
            type="fact",
            scopes=["project:alpha"],
        )
        user_key, assistant_key = self.capture_turn(
            "same-use-type-conflict",
            user="alpha 项目负责人更新为乙。",
            assistant="已确认 alpha 项目负责人更新为乙。",
        )
        wrong_type = candidate(
            "alpha-owner-project-label",
            [user_key, assistant_key],
            memory="alpha 项目负责人更新为乙。",
            type="project",
            scopes=["project:alpha"],
            update_memory_id=old.memory_id,
        )
        backend = QueueBackend([gate([wrong_type])] * 3)

        result = self.service.process(
            source="hermes", session_id="same-use-type-conflict", model=backend
        )

        self.assertEqual(result["processed_turns"], 1)
        self.assertEqual(result["deferred_candidates"], 1)
        self.assertEqual(result["memories_written"], 0)
        self.assertEqual(result["memory_ids"], [])
        self.assertEqual(len(self.active()), 1)
        self.assertEqual(self.service.read(old.memory_id).body, old.body)
        self.assertEqual(self.service.vault.list_markdown("history"), [])
        self.assertTrue(self.inbox_exists("same-use-type-conflict"))
        processed = json.loads(self.service.vault.processed_index_path.read_text(encoding="utf-8"))
        entry = processed["sessions"]["hermes/same-use-type-conflict"]["processed_turns"][0]
        self.assertEqual(entry["deferred_candidates"][0]["reason"], "update_target_type_mismatch")

    def test_bad_cross_project_candidate_does_not_block_valid_candidate(self):
        """One invalid update in a digest is isolated while another candidate commits."""

        old = self.service.create_memory(
            memory_id="mem-beta-plan-digest",
            title="beta 项目实施计划",
            body="beta 项目实施计划按原安排执行。",
            type="project",
            scopes=["project:beta"],
        )
        user_key, assistant_key = self.capture_turn(
            "cross-project-digest",
            user="beta 项目实施计划仍按原安排；alpha 项目临时建议待确认；gamma 项目负责人已更新为乙。",
            assistant="beta 计划未变，alpha 事项继续等待确认，gamma 负责人变更已确认。",
        )
        bad = candidate(
            "alpha-wrong-target-in-digest",
            [user_key, assistant_key],
            memory="alpha 项目临时建议待确认。",
            type="fact",
            scopes=["project:alpha"],
            update_memory_id=old.memory_id,
        )
        good = candidate(
            "gamma-owner-in-digest",
            [user_key, assistant_key],
            memory="gamma 项目负责人已更新为乙。",
            type="fact",
            scopes=["project:gamma"],
            update_memory_id=None,
        )
        # The bad candidate is repeated by the model on every gate retry;
        # the final retry must remove only that target and retain ``good``.
        backend = QueueBackend(
            [
                gate([bad, good]),
                gate([bad, good]),
                gate([bad, good]),
                no_change(),
                summary(
                    user_key,
                    title="gamma 项目负责人",
                    body="gamma 项目负责人已更新为乙。",
                    type="fact",
                    scopes=["project:gamma"],
                ),
            ]
        )

        result = self.service.process(
            source="hermes", session_id="cross-project-digest", model=backend
        )

        self.assertEqual(result["memories_written"], 1)
        self.assertEqual(self.service.read(old.memory_id).body, old.body)
        created = [memory for memory in self.active() if memory.memory_id != old.memory_id]
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].type, "fact")
        self.assertIn("gamma", created[0].body)
        self.assertEqual(self.service.vault.list_markdown("history"), [])

    def test_temporary_and_covered_candidates_do_not_pollute_on_fallback(self):
        """A fallback CREATE still obeys NO_CHANGE for transient/covered items."""

        old_temporary = self.service.create_memory(
            memory_id="mem-beta-temporary",
            title="beta 临时事项",
            body="beta 临时事项已记录。",
            type="fact",
            scopes=["project:beta"],
        )
        old_covered = self.service.create_memory(
            memory_id="mem-beta-covered",
            title="beta 已有记录",
            body="beta 已有记录，无新增变化。",
            type="fact",
            scopes=["project:beta"],
        )
        user_key, assistant_key = self.capture_turn(
            "no-pollution-fallback",
            user="beta 临时事项已记录，beta 已有记录无新增变化；alpha 项目等待客户反馈；alpha 项目已有记录，无新增变化。",
            assistant="beta 事项只是原有记录，alpha 两项也只是临时或已覆盖事项，不应进入永久记忆。",
        )
        temporary = candidate(
            "alpha-temporary",
            [user_key, assistant_key],
            memory="alpha 项目等待客户反馈。",
            type="project",
            scopes=["project:alpha"],
            update_memory_id=old_temporary.memory_id,
        )
        covered = candidate(
            "alpha-covered",
            [user_key, assistant_key],
            memory="alpha 项目已有记录，无新增变化。",
            type="project",
            scopes=["project:alpha"],
            update_memory_id=old_covered.memory_id,
        )
        backend = QueueBackend(
            [
                gate([temporary, covered]),
                gate([temporary, covered]),
                gate([temporary, covered]),
                no_change(),
                no_change(),
            ]
        )

        result = self.service.process(
            source="hermes", session_id="no-pollution-fallback", model=backend
        )

        self.assertEqual(result["memories_written"], 0)
        self.assertEqual(len(self.active()), 2)
        self.assertEqual(self.service.read(old_temporary.memory_id).body, old_temporary.body)
        self.assertEqual(self.service.read(old_covered.memory_id).body, old_covered.body)
        self.assertEqual(self.service.vault.list_markdown("history"), [])

    def test_ambiguous_same_use_target_keeps_inbox_and_writes_nothing(self):
        """No deterministic target is still a retryable, zero-write condition."""

        first = self.service.create_memory(
            memory_id="mem-alpha-plan-one",
            title="alpha 项目实施计划：测试环境",
            body="alpha 测试环境实施计划按原安排执行。",
            type="project",
            scopes=["project:alpha"],
        )
        second = self.service.create_memory(
            memory_id="mem-alpha-plan-two",
            title="alpha 项目实施计划：上线安排",
            body="alpha 上线实施计划按原安排执行。",
            type="project",
            scopes=["project:alpha"],
        )
        user_key, assistant_key = self.capture_turn(
            "ambiguous-recovery",
            user="alpha 项目实施计划需要更新。",
            assistant="已确认 alpha 项目存在多个可能的实施计划目标。",
        )
        ambiguous = {
            "candidate_id": "ambiguous-alpha-plan",
            "memory": "alpha 项目实施计划需要更新。",
            "evidence_event_ids": [user_key, assistant_key],
            "duplicate": False,
            "worth": True,
            "type": "project",
            "scopes": ["project:alpha"],
            "scope_source": "model",
        }
        backend = QueueBackend([gate([ambiguous])])

        result = self.service.process(
            source="hermes", session_id="ambiguous-recovery", model=backend
        )

        self.assertEqual(result["memories_written"], 0)
        self.assertEqual(result["deferred_candidates"], 1)
        self.assertEqual({memory.memory_id for memory in self.active()}, {first.memory_id, second.memory_id})
        self.assertEqual(self.service.vault.list_markdown("history"), [])
        self.assertTrue(self.inbox_exists("ambiguous-recovery"))


if __name__ == "__main__":
    unittest.main()

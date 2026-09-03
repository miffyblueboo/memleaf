from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, text: str) -> None:
    (ROOT / path).write_text(text, encoding="utf-8")


def replace_once(path: str, old: str, new: str) -> None:
    text = read(path)
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one occurrence, found {count}: {old[:80]!r}")
    write(path, text.replace(old, new, 1))


def replace_all(path: str, old: str, new: str, *, minimum: int = 1) -> None:
    text = read(path)
    count = text.count(old)
    if count < minimum:
        raise RuntimeError(f"{path}: expected at least {minimum} occurrences, found {count}: {old[:80]!r}")
    write(path, text.replace(old, new))


def regex_replace_once(path: str, pattern: str, replacement: str) -> None:
    text = read(path)
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE | re.DOTALL)
    if count != 1:
        raise RuntimeError(f"{path}: regex expected one occurrence, found {count}: {pattern[:100]!r}")
    write(path, updated)


# ---------------------------------------------------------------------------
# Memory model: first-class todo due_date with backward-compatible Markdown.
# ---------------------------------------------------------------------------
replace_once(
    "src/memleaf/models.py",
    "from datetime import datetime, timezone",
    "from datetime import date, datetime, timezone",
)
replace_once(
    "src/memleaf/models.py",
    "    status: Optional[str] = None\n    completed_at: Optional[str] = None\n    extra: dict[str, Any] = field(default_factory=dict)",
    "    status: Optional[str] = None\n    completed_at: Optional[str] = None\n    due_date: Optional[str] = None\n    extra: dict[str, Any] = field(default_factory=dict)",
)
replace_once(
    "src/memleaf/models.py",
    "        if self.completed_at is not None and not isinstance(self.completed_at, str):\n            raise ValueError(\"memory completed_at must be a string\")",
    "        if self.completed_at is not None and not isinstance(self.completed_at, str):\n            raise ValueError(\"memory completed_at must be a string\")\n        if self.due_date is not None:\n            if not isinstance(self.due_date, str) or len(self.due_date) != 10:\n                raise ValueError(\"memory due_date must be YYYY-MM-DD\")\n            try:\n                parsed_due_date = date.fromisoformat(self.due_date)\n            except ValueError as error:\n                raise ValueError(\"memory due_date must be YYYY-MM-DD\") from error\n            if parsed_due_date.isoformat() != self.due_date or self.type != \"todo\":\n                raise ValueError(\"memory due_date requires a todo and YYYY-MM-DD\")",
)
replace_once(
    "src/memleaf/models.py",
    "            completed_at=metadata.pop(\"completed_at\", None),\n            extra=metadata,",
    "            completed_at=metadata.pop(\"completed_at\", None),\n            due_date=metadata.pop(\"due_date\", None),\n            extra=metadata,",
)
replace_once(
    "src/memleaf/models.py",
    "            \"completed_at\",\n        }",
    "            \"completed_at\",\n            \"due_date\",\n        }",
)
replace_once(
    "src/memleaf/models.py",
    "            completed_at=value.get(\"completed_at\"),\n            extra={key: item for key, item in value.items() if key not in known},",
    "            completed_at=value.get(\"completed_at\"),\n            due_date=value.get(\"due_date\"),\n            extra={key: item for key, item in value.items() if key not in known},",
)
replace_once(
    "src/memleaf/models.py",
    "        if self.completed_at is not None:\n            metadata[\"completed_at\"] = self.completed_at\n        for key, value in self.extra.items():",
    "        if self.completed_at is not None:\n            metadata[\"completed_at\"] = self.completed_at\n        if self.due_date is not None:\n            metadata[\"due_date\"] = self.due_date\n        for key, value in self.extra.items():",
)

# ---------------------------------------------------------------------------
# Validation: todo type detection, due_date schema and grounding.
# ---------------------------------------------------------------------------
replace_once(
    "src/memleaf/validation.py",
    "        \"todo_fields\",\n        \"relative_time\",",
    "        \"todo_fields\",\n        \"invalid_due_date\",\n        \"due_date_not_grounded\",\n        \"relative_time\",",
)
replace_once(
    "src/memleaf/validation.py",
    "        \"completed_at\",\n    )\n)",
    "        \"completed_at\",\n        \"due_date\",\n    )\n)",
)
insert_marker = "\ndef is_project_plan_text(value: Any) -> bool:\n"
validation = read("src/memleaf/validation.py")
if insert_marker not in validation:
    raise RuntimeError("validation.py: project-plan marker not found")
action_helper = r'''
_ACTION_VERBS = (
    "修复", "整改", "排查", "准备", "反馈", "回复", "提交", "确认", "部署", "迁移",
    "调整", "跟进", "处理", "补充", "发送", "提供", "review", "reply", "submit",
    "confirm", "deploy", "migrate", "fix", "investigate", "prepare", "follow up",
)
_ACTION_PREFIXES = (
    "需要", "需", "待", "请", "必须", "务必", "尽快", "尚需", "还需", "未完成",
    "need to", "needs to", "must", "todo", "to-do", "pending",
)
_ACTION_PLAN_ADJUST = re.compile(
    r"(?:需要|需|待|请|尽快|按\s*\d+\s*[点项条]|根据\s*\d+\s*[点项条]).{0,20}(?:调整|修改|更新|完善).{0,20}(?:计划|方案)",
    re.IGNORECASE,
)


def is_actionable_todo_text(value: Any) -> bool:
    """Recognize high-signal unfinished actions without classifying stable rules."""

    if not isinstance(value, str) or not value.strip():
        return False
    folded = value.casefold()
    if _ACTION_PLAN_ADJUST.search(value):
        return True
    has_action = any(marker in folded for marker in _ACTION_VERBS)
    has_prefix = any(marker in folded for marker in _ACTION_PREFIXES)
    has_deadline = bool(_CALENDAR_TEXT.search(value)) and any(
        marker in folded for marker in ("前完成", "之前完成", "截止", "截至", " by ", "before ")
    )
    return has_action and (has_prefix or has_deadline)


def _validated_due_date(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ModelOutputError("invalid todo due_date", validation_detail="invalid_due_date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ModelOutputError("invalid todo due_date", validation_detail="invalid_due_date") from error
    if parsed.isoformat() != value:
        raise ModelOutputError("invalid todo due_date", validation_detail="invalid_due_date")
    return value

'''
write("src/memleaf/validation.py", validation.replace(insert_marker, "\n" + action_helper + "def is_project_plan_text(value: Any) -> bool:\n", 1))

# Expand mixed-future-use from only dated todos to any high-confidence independent unfinished action.
replace_once(
    "src/memleaf/validation.py",
    "        if _CALENDAR_TEXT.search(clause)\n        and any(marker in clause.casefold() for marker in _DATED_TODO_MARKERS)",
    "        if (\n            (_CALENDAR_TEXT.search(clause) and any(marker in clause.casefold() for marker in _DATED_TODO_MARKERS))\n            or is_actionable_todo_text(clause)\n        )",
)

# Gate: normalize clear unfinished actions to todo before plan canonicalization.
replace_once(
    "src/memleaf/validation.py",
    "        # A clearly named implementation plan/plan adjustment is a project\n        # memory, even when the model labels it as a generic fact.  Keep todo",
    "        if (\n            item[\"worth\"]\n            and candidate_type in {\"fact\", \"event\", \"other\", \"project\"}\n            and is_actionable_todo_text(item[\"memory\"])\n        ):\n            item[\"type\"] = \"todo\"\n            candidate_type = \"todo\"\n        # A clearly named implementation plan/plan adjustment is a project\n        # memory, even when the model labels it as a generic fact.  Keep todo",
)

# Summarize schema accepts due_date and validates it against current evidence-derived dates.
replace_once(
    "src/memleaf/validation.py",
    "    expected_scope_source: str | None = None,\n    allow_no_change: bool = False,",
    "    expected_scope_source: str | None = None,\n    allowed_due_dates: Iterable[str] | None = None,\n    allow_no_change: bool = False,",
)
replace_once(
    "src/memleaf/validation.py",
    "        \"completed_at\",\n        \"shadow_native_ids\",",
    "        \"completed_at\",\n        \"due_date\",\n        \"shadow_native_ids\",",
)
replace_once(
    "src/memleaf/validation.py",
    "    if item.get(\"status\") == \"completed\" and \"completed_at\" not in item:\n        raise ModelOutputError(\"completed todo requires completed_at\", validation_detail=\"todo_fields\")\n    return item",
    "    if item.get(\"status\") == \"completed\" and \"completed_at\" not in item:\n        raise ModelOutputError(\"completed todo requires completed_at\", validation_detail=\"todo_fields\")\n    if \"due_date\" in item and item[\"due_date\"] is not None:\n        if candidate_type != \"todo\":\n            raise ModelOutputError(\"due_date requires todo\", validation_detail=\"todo_fields\")\n        item[\"due_date\"] = _validated_due_date(item[\"due_date\"])\n        if allowed_due_dates is not None:\n            grounded = {value for value in allowed_due_dates if isinstance(value, str)}\n            if item[\"due_date\"] not in grounded:\n                raise ModelOutputError(\"todo due_date is not grounded by current evidence\", validation_detail=\"due_date_not_grounded\")\n    return item",
)
# parse_summarize signature and forwarding.
replace_once(
    "src/memleaf/validation.py",
    "    expected_scope_source: str | None = None,\n    allow_no_change: bool = False,\n) -> dict[str, Any]:\n    try:\n        parsed = parse_strict_json(raw)",
    "    expected_scope_source: str | None = None,\n    allowed_due_dates: Iterable[str] | None = None,\n    allow_no_change: bool = False,\n) -> dict[str, Any]:\n    try:\n        parsed = parse_strict_json(raw)",
)
replace_once(
    "src/memleaf/validation.py",
    "            expected_scope_source=expected_scope_source,\n            allow_no_change=allow_no_change,",
    "            expected_scope_source=expected_scope_source,\n            allowed_due_dates=allowed_due_dates,\n            allow_no_change=allow_no_change,",
)
# Compact schema due_date.
replace_once(
    "src/memleaf/validation.py",
    "        if item.get(\"status\") == \"completed\" and \"completed_at\" not in item:\n            raise ModelOutputError(\"compact completed todo requires completed_at\")\n        normalized.append(item)",
    "        if item.get(\"status\") == \"completed\" and \"completed_at\" not in item:\n            raise ModelOutputError(\"compact completed todo requires completed_at\")\n        if \"due_date\" in item and item[\"due_date\"] is not None:\n            if item[\"type\"] != \"todo\":\n                raise ModelOutputError(\"compact due_date requires todo\")\n            item[\"due_date\"] = _validated_due_date(item[\"due_date\"])\n        normalized.append(item)",
)
replace_once(
    "src/memleaf/validation.py",
    "    \"is_attachment_followup_only_text\",\n    \"is_project_plan_text\",",
    "    \"is_attachment_followup_only_text\",\n    \"is_actionable_todo_text\",\n    \"is_project_plan_text\",",
)

# ---------------------------------------------------------------------------
# Processing: due-date normalization/grounding and planned-memory propagation.
# ---------------------------------------------------------------------------
replace_once(
    "src/memleaf/processing.py",
    "    is_attachment_followup_only_text,\n    is_mixed_future_use_text,",
    "    is_attachment_followup_only_text,\n    is_actionable_todo_text,\n    is_mixed_future_use_text,",
)
replace_once(
    "src/memleaf/processing.py",
    "        \"completed_at\",\n        \"shadow_native_ids\",",
    "        \"completed_at\",\n        \"due_date\",\n        \"shadow_native_ids\",",
)
# Normalize relative due_date together with title/body.
replace_once(
    "src/memleaf/processing.py",
    "    for field in (\"title\", \"body\"):",
    "    for field in (\"title\", \"body\", \"due_date\"):",
)
# Add deterministic extraction of all dates actually present in current evidence.
marker = "\ndef _native_result(value: Any) -> list[dict[str, Any]]:\n"
processing = read("src/memleaf/processing.py")
if marker not in processing:
    raise RuntimeError("processing.py: native result marker not found")
due_helpers = r'''

def _grounded_due_dates(turn: InboxTurn) -> set[str]:
    """Return absolute dates actually supported by current visible evidence."""

    result: set[str] = set()
    for event in turn.events:
        timestamp = _parse_time(event.timestamp)
        if timestamp is None or not isinstance(event.content, str):
            continue
        normalized = normalize_relative_calendar_text(event.content, timestamp) or event.content
        for value in re.findall(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)", normalized):
            try:
                parsed = datetime.strptime(value, "%Y-%m-%d").date()
            except ValueError:
                continue
            result.add(parsed.isoformat())
        for year, month, day in re.findall(r"(?:(\d{4})\s*年\s*)?(\d{1,2})\s*月\s*(\d{1,2})\s*日?", event.content):
            try:
                parsed = datetime(
                    int(year) if year else timestamp.year,
                    int(month),
                    int(day),
                    tzinfo=timezone.utc,
                ).date()
            except ValueError:
                continue
            result.add(parsed.isoformat())
    return result

'''
write("src/memleaf/processing.py", processing.replace(marker, due_helpers + "\ndef _native_result(value: Any) -> list[dict[str, Any]]:\n", 1))
# Every summarize parser gets grounded due dates.
replace_all(
    "src/memleaf/processing.py",
    "                    expected_scope_source=candidate.get(\"scope_source\"),\n                    allow_no_change=True,",
    "                    expected_scope_source=candidate.get(\"scope_source\"),\n                    allowed_due_dates=_grounded_due_dates(turn),\n                    allow_no_change=True,",
    minimum=1,
)
# Explicit remember parser has no expected_scope_source block; add grounding near allow_no_change=False.
replace_once(
    "src/memleaf/processing.py",
    "                    scope_registry=validation_scope_registry,\n                    allow_no_change=False,",
    "                    scope_registry=validation_scope_registry,\n                    allowed_due_dates=_grounded_due_dates(turn),\n                    allow_no_change=False,",
)
# planned memory includes todo metadata.
replace_once(
    "src/memleaf/processing.py",
    "            \"keywords\": list(summary.get(\"keywords\", [])) if isinstance(summary.get(\"keywords\", []), list) else [],\n        }",
    "            \"keywords\": list(summary.get(\"keywords\", [])) if isinstance(summary.get(\"keywords\", []), list) else [],\n            \"status\": summary.get(\"status\"),\n            \"completed_at\": summary.get(\"completed_at\"),\n            \"due_date\": summary.get(\"due_date\"),\n        }",
)

# ---------------------------------------------------------------------------
# Writer: preserve/update/clear due_date without changing memory_id.
# ---------------------------------------------------------------------------
replace_once(
    "src/memleaf/memory_writer.py",
    "            and left.completed_at == right.completed_at\n            and left_extra == right_extra",
    "            and left.completed_at == right.completed_at\n            and left.due_date == right.due_date\n            and left_extra == right_extra",
)
replace_once(
    "src/memleaf/memory_writer.py",
    "        status = summary.get(\"status\")\n        if summary[\"type\"] == \"todo\" and status is None:\n            status = \"active\"",
    "        status = summary.get(\"status\") if \"status\" in summary else (existing.status if existing is not None else None)\n        if summary[\"type\"] == \"todo\" and status is None:\n            status = \"active\"\n        due_date = summary.get(\"due_date\") if \"due_date\" in summary else (existing.due_date if existing is not None else None)\n        completed_at = (\n            summary.get(\"completed_at\")\n            if \"completed_at\" in summary\n            else existing.completed_at if existing is not None and status == \"completed\" else None\n        )",
)
replace_once(
    "src/memleaf/memory_writer.py",
    "            status=status,\n            completed_at=summary.get(\"completed_at\"),\n            extra=extra,",
    "            status=status,\n            completed_at=completed_at,\n            due_date=due_date,\n            extra=extra,",
)
replace_once(
    "src/memleaf/memory_writer.py",
    "            status=existing.status,\n            completed_at=existing.completed_at,\n            extra=dict(existing.extra),",
    "            status=existing.status,\n            completed_at=existing.completed_at,\n            due_date=existing.due_date,\n            extra=dict(existing.extra),",
)
replace_once(
    "src/memleaf/memory_writer.py",
    "            status=old.status,\n            completed_at=old.completed_at,\n            extra=extra,",
    "            status=old.status,\n            completed_at=old.completed_at,\n            due_date=old.due_date,\n            extra=extra,",
)

# ---------------------------------------------------------------------------
# Compaction: carry due_date and never merge multiple independent todos.
# ---------------------------------------------------------------------------
replace_all(
    "src/memleaf/compaction.py",
    "            \"completed_at\": value.completed_at,",
    "            \"completed_at\": value.completed_at,\n            \"due_date\": value.due_date,",
    minimum=1,
)
replace_all(
    "src/memleaf/compaction.py",
    "        \"completed_at\": value.get(\"completed_at\"),",
    "        \"completed_at\": value.get(\"completed_at\"),\n        \"due_date\": value.get(\"due_date\"),",
    minimum=1,
)
replace_once(
    "src/memleaf/compaction.py",
    "                    \"completed_at\",\n                )",
    "                    \"completed_at\",\n                    \"due_date\",\n                )",
)
replace_once(
    "src/memleaf/compaction.py",
    "            status=status,\n            completed_at=summary.get(\"completed_at\"),\n            extra=extra,",
    "            status=status,\n            completed_at=summary.get(\"completed_at\"),\n            due_date=summary.get(\"due_date\"),\n            extra=extra,",
)
replace_once(
    "src/memleaf/compaction.py",
    "            and left.completed_at == right.completed_at\n            and left_extra == right_extra",
    "            and left.completed_at == right.completed_at\n            and left.due_date == right.due_date\n            and left_extra == right_extra",
)
replace_all(
    "src/memleaf/compaction.py",
    "            completed_at=source.memory.completed_at,\n            extra=extra,",
    "            completed_at=source.memory.completed_at,\n            due_date=source.memory.due_date,\n            extra=extra,",
    minimum=1,
)
# Prevent independent todo collapse in compaction plan.
replace_once(
    "src/memleaf/compaction.py",
    "        for summary in output[\"memories\"]:\n            source_ids = tuple(summary[\"source_memory_ids\"])",
    "        for summary in output[\"memories\"]:\n            source_ids = tuple(summary[\"source_memory_ids\"])\n            if summary.get(\"type\") == \"todo\" and len(source_ids) != 1:\n                raise CompactionError(\"independent todos cannot be merged by compaction\")",
)

# ---------------------------------------------------------------------------
# Service: global active todo directory and structured read metadata.
# ---------------------------------------------------------------------------
replace_once(
    "src/memleaf/service.py",
    "import base64\nfrom dataclasses import dataclass",
    "import base64\nfrom dataclasses import dataclass\nfrom datetime import date",
)
# read_page structured metadata.
replace_once(
    "src/memleaf/service.py",
    "            version: str,\n            count_hit: bool,",
    "            version: str,\n            count_hit: bool,\n            memory_type: str = \"other\",\n            status: str | None = None,\n            due_date: str | None = None,",
)
replace_once(
    "src/memleaf/service.py",
    "                \"version\": version,\n            }",
    "                \"version\": version,\n                \"type\": memory_type,\n                \"status\": status,\n                \"due_date\": due_date,\n            }",
)
replace_once(
    "src/memleaf/service.py",
    "                    version=_memory_version(memory),\n                    count_hit=record.area == \"knowledge\",\n                    record=record,",
    "                    version=_memory_version(memory),\n                    count_hit=record.area == \"knowledge\",\n                    memory_type=memory.type,\n                    status=(memory.status or \"active\") if memory.type == \"todo\" else memory.status,\n                    due_date=memory.due_date,\n                    record=record,",
)
# Add global todo directory before _scope_query_values.
service_marker = "    @staticmethod\n    def _scope_query_values(scope: str | Iterable[str] | None) -> str | list[str] | None:\n"
service = read("src/memleaf/service.py")
if service_marker not in service:
    raise RuntimeError("service.py: scope query marker not found")
list_todos_code = r'''    @staticmethod
    def _todo_date(value: str | None, field: str) -> date | None:
        if value is None:
            return None
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError(f"{field} must be YYYY-MM-DD")
        try:
            parsed = date.fromisoformat(value)
        except ValueError as error:
            raise ValueError(f"{field} must be YYYY-MM-DD") from error
        if parsed.isoformat() != value:
            raise ValueError(f"{field} must be YYYY-MM-DD")
        return parsed

    @staticmethod
    def _todo_is_asap(memory: Memory) -> bool:
        text = "\n".join([memory.title, memory.body, *memory.tags, *memory.keywords]).casefold()
        return any(marker in text for marker in ("尽快", "紧急", "优先处理", "asap", "urgent"))

    @staticmethod
    def _bounded_todo_page(
        candidates: list[dict[str, Any]],
        *,
        start: int,
        limit: int,
        fingerprint: str,
    ) -> dict[str, Any]:
        selected: list[dict[str, Any]] = []
        index = start
        while index < len(candidates) and len(selected) < limit:
            next_index = index + 1
            has_more = next_index < len(candidates)
            next_cursor = (
                _encode_page_cursor("active_todos", fingerprint, next_index)
                if has_more
                else None
            )
            proposed = {
                "status": "found",
                "results": selected + [candidates[index]],
                "has_more": has_more,
                "next_cursor": next_cursor,
            }
            if payload_chars(proposed) > MAX_SEARCH_CANDIDATE_CHARS:
                if not selected:
                    raise RetrievalError("search_result_too_large", "todo result exceeds the response budget")
                break
            selected.append(candidates[index])
            index = next_index
        has_more = index < len(candidates)
        return {
            "status": "found",
            "results": selected,
            "has_more": has_more,
            "next_cursor": _encode_page_cursor("active_todos", fingerprint, index) if has_more else None,
        }

    def list_todos(
        self,
        *,
        status: str = "active",
        scope: str | Iterable[str] | None = None,
        due_from: str | None = None,
        due_to: str | None = None,
        include_overdue: bool = True,
        include_unscheduled: bool = True,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Enumerate current todo memories globally, independent of source session or agent."""

        if status not in {"active", "completed", "cancelled", "all"}:
            raise ValueError("invalid todo status")
        if type(include_overdue) is not bool or type(include_unscheduled) is not bool:
            raise ValueError("todo inclusion flags must be booleans")
        lower = self._todo_date(due_from, "due_from")
        upper = self._todo_date(due_to, "due_to")
        if lower is not None and upper is not None and lower > upper:
            raise ValueError("due_from must not be after due_to")
        page_limit = self._page_limit(limit, MAX_SEARCH_CANDIDATE_ITEMS, MAX_SEARCH_CANDIDATE_ITEMS)
        scope_value = self._scope_query_values(scope)
        with self.vault.lock():
            self._recover_compaction_unlocked()
            records = [record for record in self._read_memories_unlocked("knowledge") if record.memory.type == "todo"]
            if scope_value is not None:
                try:
                    requested = normalize_scopes(scope_value, field="todo scope")
                except ScopeError as error:
                    raise RetrievalError("invalid_scope", "todo scope is invalid") from error
                if not requested:
                    raise RetrievalError("invalid_scope", "todo scope is invalid")
                ranked = filter_by_scope([record.memory for record in records], requested, self.vault.config())
                allowed_ids = {memory.memory_id for memory, _rank in ranked}
                records = [record for record in records if record.memory.memory_id in allowed_ids]

            today = date.today()
            filtered: list[tuple[tuple[Any, ...], _Record]] = []
            for record in records:
                memory = record.memory
                current_status = memory.status or "active"
                if status != "all" and current_status != status:
                    continue
                parsed_due = self._todo_date(memory.due_date, "due_date") if memory.due_date is not None else None
                if parsed_due is None:
                    if not include_unscheduled:
                        continue
                    bucket = 3 if self._todo_is_asap(memory) else 4
                    sort_key = (bucket, date.max, memory.title.casefold(), memory.memory_id)
                else:
                    in_range = (lower is None or parsed_due >= lower) and (upper is None or parsed_due <= upper)
                    overdue = include_overdue and parsed_due < today and (upper is None or parsed_due <= upper)
                    if (lower is not None or upper is not None) and not (in_range or overdue):
                        continue
                    bucket = 0 if parsed_due < today else 1 if parsed_due == today else 2
                    sort_key = (bucket, parsed_due, memory.title.casefold(), memory.memory_id)
                filtered.append((sort_key, record))

            filtered.sort(key=lambda item: item[0])
            ordered = [record for _key, record in filtered]
            candidates = [
                {
                    "memory_id": record.memory.memory_id,
                    "title": directory_entry(record.memory).title,
                    "due_date": record.memory.due_date,
                }
                for record in ordered
            ]
            fingerprint = _page_fingerprint(
                {
                    "filters": {
                        "status": status,
                        "scope": scope_value,
                        "due_from": due_from,
                        "due_to": due_to,
                        "include_overdue": include_overdue,
                        "include_unscheduled": include_unscheduled,
                    },
                    "records": [
                        {
                            "memory_id": record.memory.memory_id,
                            "version": _memory_version(record.memory),
                        }
                        for record in ordered
                    ],
                }
            )
            start = _decode_page_cursor(cursor, kind="active_todos", fingerprint=fingerprint, maximum=len(candidates))
            if not candidates:
                return {"status": "no_match", "results": [], "has_more": False, "next_cursor": None}
            return self._bounded_todo_page(candidates, start=start, limit=page_limit, fingerprint=fingerprint)

'''
write("src/memleaf/service.py", service.replace(service_marker, list_todos_code + service_marker, 1))
# service needs re for date validator.
replace_once("src/memleaf/service.py", "import os\nimport base64", "import os\nimport re\nimport base64")

# ---------------------------------------------------------------------------
# Retrieval gate: audit-only read counters; list_todos paging chain.
# ---------------------------------------------------------------------------
replace_once(
    "src/memleaf/retrieval_gate.py",
    "MAX_READ_ITEMS = 3\nMAX_READ_CHARS = 6000\nMAX_READ_PAGE_CHARS = 2000",
    "# Deprecated compatibility symbols. They are audit-only and no longer enforce a per-turn cap.\nMAX_READ_ITEMS = None\nMAX_READ_CHARS = None\nMAX_READ_PAGE_CHARS = 2000",
)
replace_once(
    "src/memleaf/retrieval_gate.py",
    "    \"retrieval_read_budget_exceeded\": \"retrieval read budget exceeded\",",
    "    \"retrieval_read_budget_exceeded\": \"retrieval read budget exceeded\",\n    \"retrieval_todo_pagination_mismatch\": \"todo pagination does not match the current retrieval chain\",",
)
replace_once(
    "src/memleaf/retrieval_gate.py",
    "        \"read_chars\": int(entry.get(\"read_chars\", 0) or 0),\n        \"expires_at\": entry.get(\"expires_at\"),",
    "        \"read_chars\": int(entry.get(\"read_chars\", 0) or 0),\n        \"todo_list_pending\": entry.get(\"todo_list_pending\") is True,\n        \"todo_list_pages\": int(entry.get(\"todo_list_pages\", 0) or 0),\n        \"expires_at\": entry.get(\"expires_at\"),",
)
replace_once(
    "src/memleaf/retrieval_gate.py",
    "            \"turn_aliases\": [],\n        }",
    "            \"turn_aliases\": [],\n            \"todo_list_pending\": False,\n            \"todo_list_pages\": 0,\n            \"todo_list_filter_hash\": \"\",\n            \"todo_list_expected_cursor_hash\": \"\",\n        }",
)
# Helpers and todo observation before request_gate_retry.
gate_marker = "\ndef request_gate_retry(vault: Vault | Path | str, retrieval_id: str) -> int:\n"
gate_text = read("src/memleaf/retrieval_gate.py")
if gate_marker not in gate_text:
    raise RuntimeError("retrieval_gate.py: request retry marker not found")
gate_helpers = r'''

def todo_filter_key(arguments: Mapping[str, Any]) -> str:
    """Return a stable, non-secret description of list_todos filters for chain validation."""

    payload = {
        "status": arguments.get("status", "active"),
        "scope": arguments.get("scope"),
        "due_from": arguments.get("due_from"),
        "due_to": arguments.get("due_to"),
        "include_overdue": arguments.get("include_overdue", True),
        "include_unscheduled": arguments.get("include_unscheduled", True),
    }
    import json

    return json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _optional_hash(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > 4096 or "\x00" in value:
        raise RetrievalGateError("retrieval_todo_pagination_mismatch")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def observe_todo_list(
    vault: Vault | Path | str,
    retrieval_id: str,
    status: str,
    call_id: str,
    *,
    filter_key: str,
    cursor: str | None,
    has_more: bool,
    next_cursor: str | None,
    current_source: str | None = None,
) -> None:
    """Record one real list_todos page and enforce a single current-turn cursor chain."""

    retrieval_id = _retrieval_id(retrieval_id)
    if status not in _SEARCH_STATUSES or type(has_more) is not bool or not isinstance(filter_key, str):
        raise RetrievalGateError("retrieval_status_invalid")
    _identity(call_id, "call_id")
    if current_source is not None:
        current_source = _identity(current_source, "source")
    call_hash = hashlib.sha256(call_id.encode("utf-8")).hexdigest()
    filter_hash = hashlib.sha256(filter_key.encode("utf-8")).hexdigest()
    cursor_hash = _optional_hash(cursor)
    next_hash = _optional_hash(next_cursor)
    if status == "no_match" and has_more:
        raise RetrievalGateError("retrieval_status_invalid")
    if status == "found" and has_more and not next_hash:
        raise RetrievalGateError("retrieval_status_invalid")
    root = _coerce_vault(vault)
    path = _ledger_path(root)
    with _with_lock(root):
        ledger = _read_ledger(path)
        entry = _entry_for(ledger, retrieval_id)
        if current_source is not None and not _is_current_entry(ledger["entries"], retrieval_id, entry, current_source):
            raise RetrievalGateError("retrieval_turn_mismatch")
        seen = entry.get("seen_call_hashes")
        if not isinstance(seen, list):
            seen = []
        if call_hash in seen:
            return
        pending = entry.get("todo_list_pending") is True
        previous_filter = entry.get("todo_list_filter_hash") if isinstance(entry.get("todo_list_filter_hash"), str) else ""
        expected_cursor = entry.get("todo_list_expected_cursor_hash") if isinstance(entry.get("todo_list_expected_cursor_hash"), str) else ""
        if status != "error":
            if cursor_hash:
                if not pending or previous_filter != filter_hash or expected_cursor != cursor_hash:
                    raise RetrievalGateError("retrieval_todo_pagination_mismatch")
            else:
                if pending and previous_filter and previous_filter != filter_hash:
                    raise RetrievalGateError("retrieval_todo_pagination_mismatch")
                entry["todo_list_pages"] = 0
            entry["todo_list_filter_hash"] = filter_hash
            entry["todo_list_pages"] = int(entry.get("todo_list_pages", 0) or 0) + 1
            entry["todo_list_pending"] = bool(has_more)
            entry["todo_list_expected_cursor_hash"] = next_hash if has_more else ""
        entry["status"] = {"found": "FOUND", "no_match": "NO_MATCH", "error": "ERROR"}[status]
        seen = [item for item in seen if isinstance(item, str)][-255:]
        seen.append(call_hash)
        entry["seen_call_hashes"] = seen
        entry["search_attempts"] = int(entry.get("search_attempts", 0) or 0) + 1
        entry["continuation_pending"] = False
        ledger["entries"][retrieval_id] = entry
        _write_ledger(path, ledger)

'''
write("src/memleaf/retrieval_gate.py", gate_text.replace(gate_marker, gate_helpers + gate_marker, 1))
# guarded_read no longer blocks on aggregate counters.
regex_replace_once(
    "src/memleaf/retrieval_gate.py",
    r"def guarded_read\(.*?\n\s+retrieval_id = _retrieval_id\(retrieval_id\)(.*?)\n\s+return dict\(result\)\n",
    '''def guarded_read(
    vault: Vault | Path | str,
    retrieval_id: str,
    memory_id: str,
    reader: Callable[[int], Mapping[str, Any] | None],
    *,
    current_source: str | None = None,
) -> Mapping[str, Any] | None:
    """Read one bounded page while keeping per-turn read counters for audit only."""

    retrieval_id = _retrieval_id(retrieval_id)
    memory_id = _identity(memory_id, "memory_id")
    if not callable(reader):
        raise RetrievalGateError("retrieval_reader_invalid")
    root = _coerce_vault(vault)
    path = _ledger_path(root)
    with _with_lock(root):
        ledger = _read_ledger(path)
        entry = _entry_for(ledger, retrieval_id)
        if current_source is not None:
            source = _identity(current_source, "source")
            if not _is_current_entry(ledger["entries"], retrieval_id, entry, source):
                raise RetrievalGateError("retrieval_turn_mismatch")
        if entry.get("status") != "FOUND":
            raise RetrievalGateError("retrieval_search_required")
        read_ids = entry.get("read_ids")
        if not isinstance(read_ids, list):
            read_ids = []
        read_ids = [item for item in read_ids if isinstance(item, str)]
        read_chars = int(entry.get("read_chars", 0) or 0)
        result = reader(MAX_READ_PAGE_CHARS)
        if result is None:
            return None
        if not isinstance(result, Mapping):
            raise RetrievalGateError("retrieval_reader_invalid")
        body = result.get("body")
        if not isinstance(body, str) or len(body) > MAX_READ_PAGE_CHARS:
            raise RetrievalGateError("retrieval_reader_invalid")
        if body:
            if memory_id not in read_ids:
                read_ids.append(memory_id)
            entry["read_ids"] = read_ids
            entry["read_chars"] = read_chars + len(body)
        ledger["entries"][retrieval_id] = entry
        _write_ledger(path, ledger)
        return dict(result)
''',
)
replace_once(
    "src/memleaf/retrieval_gate.py",
    "    \"observe_search\",\n    \"request_gate_retry\",",
    "    \"observe_search\",\n    \"observe_todo_list\",\n    \"todo_filter_key\",\n    \"request_gate_retry\",",
)

# ---------------------------------------------------------------------------
# MCP: public list_todos tool, current-turn binding and retrieval observation.
# ---------------------------------------------------------------------------
replace_once(
    "src/memleaf/mcp_server.py",
    "    guarded_read,\n    observe_search,",
    "    guarded_read,\n    observe_search,\n    observe_todo_list,\n    todo_filter_key,",
)
replace_once(
    "src/memleaf/mcp_server.py",
    "    \"it to reset a budget. Managed turns permit at most 3 distinct memory IDs and 6000 returned body \"\n    \"characters. MCP read requires retrieval_id and a current FOUND search; NO_MATCH, ERROR, \"\n    \"and DEGRADED turns cannot read. A budget error requires stopping further reads in this turn, \"\n    \"not a different tool. \"",
    "    \"it. Managed reads have no aggregate ID/character quota; read every relevant memory needed for \"\n    \"the user's question while keeping each read page at 2000 characters. MCP read requires retrieval_id \"\n    \"and a current FOUND search or list_todos result; NO_MATCH, ERROR, and DEGRADED turns cannot read. \"\n    \"For global current-todo questions use list_todos rather than relevance search, omit scope to cover \"\n    \"all scopes, follow next_cursor until has_more=false, then read every matching todo body. \"",
)
# Insert tool after search definition by locating read tool marker.
mcp_marker = "    {\n        \"name\": \"read\",\n"
mcp = read("src/memleaf/mcp_server.py")
if mcp_marker not in mcp:
    raise RuntimeError("mcp_server.py: read tool marker not found")
todo_tool = '''    {
        "name": "list_todos",
        "description": (
            "Enumerate current memleaf todo memories by status/date across all scopes by default. "
            "This is not relevance search. Continue with next_cursor until has_more=false, then read "
            "the matching todo bodies with the same retrieval_id."
        ),
        "inputSchema": _object_schema(
            {
                "status": {"type": "string", "enum": ["active", "completed", "cancelled", "all"]},
                "scope": _text_or_texts_schema(),
                "due_from": {"type": "string"},
                "due_to": {"type": "string"},
                "include_overdue": {"type": "boolean"},
                "include_unscheduled": {"type": "boolean"},
                "cursor": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1},
                "retrieval_id": {"type": "string"},
            },
            required=["retrieval_id"],
        ),
    },
'''
write("src/memleaf/mcp_server.py", mcp.replace(mcp_marker, todo_tool + mcp_marker, 1))
replace_once(
    "src/memleaf/mcp_server.py",
    "    if name in {\"read\", \"search\"} and (",
    "    if name in {\"read\", \"search\", \"list_todos\"} and (",
)
# Add observer helper before _invoke_tool.
marker = "\ndef _invoke_tool(\n"
mcp = read("src/memleaf/mcp_server.py")
if marker not in mcp:
    raise RuntimeError("mcp_server.py: invoke marker not found")
observer = r'''

def _observe_mcp_todos(
    service: Memleaf,
    state: Mapping[str, Any] | None,
    value: Mapping[str, Any] | None,
    request_id: Any,
    arguments: Mapping[str, Any],
) -> None:
    if state is None:
        return
    status = value.get("status") if isinstance(value, Mapping) else "error"
    if status not in {"found", "no_match"}:
        status = "error"
    has_more = value.get("has_more") if isinstance(value, Mapping) else False
    next_cursor = value.get("next_cursor") if isinstance(value, Mapping) else None
    observe_todo_list(
        service.vault,
        state["retrieval_id"],
        status,
        _mcp_search_call_id(request_id),
        filter_key=todo_filter_key(arguments),
        cursor=arguments.get("cursor") if isinstance(arguments.get("cursor"), str) else None,
        has_more=has_more if isinstance(has_more, bool) else False,
        next_cursor=next_cursor if isinstance(next_cursor, str) else None,
        current_source="hermes",
    )

'''
write("src/memleaf/mcp_server.py", mcp.replace(marker, observer + marker, 1))
# Dispatch branch before read.
replace_once(
    "src/memleaf/mcp_server.py",
    "        elif name == \"read\":\n            # Keep the protocol boundary hard-capped",
    '''        elif name == "list_todos":
            retrieval_id = args.pop("retrieval_id", None)
            managed_state = _managed_search_state(service, retrieval_id)
            observed_args = dict(args)
            try:
                value = service.list_todos(**args)
            except Exception as error:
                try:
                    _observe_mcp_todos(service, managed_state, None, request_id, observed_args)
                except Exception as observe_error:
                    return _tool_error(observe_error)
                return _tool_error(error)
            try:
                _observe_mcp_todos(service, managed_state, value, request_id, observed_args)
            except Exception as error:
                return _tool_error(error)
        elif name == "read":
            # Keep the protocol boundary hard-capped''',
)

# ---------------------------------------------------------------------------
# Host runtime: list_todos counts as retrieval and incomplete paging blocks completion.
# ---------------------------------------------------------------------------
replace_once(
    "src/memleaf/host_runtime.py",
    "    observe_search,\n    request_gate_retry,",
    "    observe_search,\n    observe_todo_list,\n    request_gate_retry,",
)
replace_once(
    "src/memleaf/host_runtime.py",
    "_GATE_ERROR_REASON = (\n    \"The memleaf search did not complete. Retry the memleaf search once before \"\n    \"answering; do not treat this error as no match.\"\n)",
    "_GATE_ERROR_REASON = (\n    \"The memleaf retrieval did not complete. Retry the memleaf memory tool once before \"\n    \"answering; do not treat this error as no match.\"\n)\n_TODO_PAGINATION_REASON = (\n    \"The global todo directory is incomplete. Continue memleaf list_todos with the returned \"\n    \"next_cursor until has_more=false before answering.\"\n)",
)
# Add method before complete_turn.
marker = "    def complete_turn(\n"
host = read("src/memleaf/host_runtime.py")
if marker not in host:
    raise RuntimeError("host_runtime.py: complete turn marker not found")
method = r'''    def observe_todo_list(
        self,
        *,
        session_id: str,
        turn_id: str,
        status: str,
        call_id: str,
        supplied_retrieval_id: Any,
        filter_key: str,
        cursor: str | None,
        has_more: bool,
        next_cursor: str | None,
    ) -> bool:
        retrieval_id = self._retrieval_id(session_id, turn_id)
        if retrieval_id is None or supplied_retrieval_id != retrieval_id or not isinstance(call_id, str) or not call_id:
            return False
        try:
            observe_todo_list(
                self.vault,
                retrieval_id,
                status,
                call_id,
                filter_key=filter_key,
                cursor=cursor,
                has_more=has_more,
                next_cursor=next_cursor,
                current_source=self.host,
            )
        except RetrievalGateError:
            return False
        return True

'''
write("src/memleaf/host_runtime.py", host.replace(marker, method + marker, 1))
replace_once(
    "src/memleaf/host_runtime.py",
    "            if gate_state is not None and gate_state.get(\"status\") in {\"NOT_SEARCHED\", \"ERROR\"}:\n                retries = int(gate_state.get(\"gate_retries\", 0) or 0)",
    "            todo_pending = gate_state is not None and gate_state.get(\"todo_list_pending\") is True\n            if gate_state is not None and (gate_state.get(\"status\") in {\"NOT_SEARCHED\", \"ERROR\"} or todo_pending):\n                retries = int(gate_state.get(\"gate_retries\", 0) or 0)",
)
replace_once(
    "src/memleaf/host_runtime.py",
    "                        reason = (\n                            _GATE_ERROR_REASON\n                            if gate_state.get(\"status\") == \"ERROR\"\n                            else _GATE_RETRY_REASON\n                        )",
    "                        reason = (\n                            _TODO_PAGINATION_REASON\n                            if todo_pending\n                            else _GATE_ERROR_REASON\n                            if gate_state.get(\"status\") == \"ERROR\"\n                            else _GATE_RETRY_REASON\n                        )",
)

# ---------------------------------------------------------------------------
# Codex hooks: bind/observe list_todos as a managed current-turn tool.
# ---------------------------------------------------------------------------
replace_once(
    "src/memleaf/host_events.py",
    "    observe_search,\n    request_gate_retry,",
    "    observe_search,\n    todo_filter_key,\n    request_gate_retry,",
)
replace_once(
    "src/memleaf/host_events.py",
    "    if not (_codex_memleaf_tool(tool_name, \"search\") or _codex_memleaf_tool(tool_name, \"read\")):",
    "    if not (\n        _codex_memleaf_tool(tool_name, \"search\")\n        or _codex_memleaf_tool(tool_name, \"list_todos\")\n        or _codex_memleaf_tool(tool_name, \"read\")\n    ):",
)
# Add todo result parser before post-tool handler.
marker = "\ndef _handle_codex_post_tool(\n"
host_events = read("src/memleaf/host_events.py")
if marker not in host_events:
    raise RuntimeError("host_events.py: post tool marker not found")
todo_parser = r'''

def _codex_todo_result(value: Any) -> tuple[str, bool, str | None]:
    result = _json_tool_result(value)
    if not isinstance(result, Mapping) or result.get("isError") is True or "error" in result:
        return "error", False, None
    status = result.get("status")
    items = result.get("results")
    has_more = result.get("has_more")
    next_cursor = result.get("next_cursor")
    if status not in {"found", "no_match"} or not isinstance(items, list) or not isinstance(has_more, bool):
        return "error", False, None
    if not all(
        isinstance(item, Mapping)
        and set(item) == {"memory_id", "title", "due_date"}
        and isinstance(item.get("memory_id"), str) and bool(item.get("memory_id"))
        and isinstance(item.get("title"), str) and bool(item.get("title"))
        and (item.get("due_date") is None or isinstance(item.get("due_date"), str))
        for item in items
    ):
        return "error", False, None
    if (has_more and not isinstance(next_cursor, str)) or (not has_more and next_cursor is not None):
        return "error", False, None
    if status == "found" and not items:
        return "error", False, None
    if status == "no_match" and items:
        return "error", False, None
    return status, has_more, next_cursor if isinstance(next_cursor, str) else None

'''
write("src/memleaf/host_events.py", host_events.replace(marker, todo_parser + marker, 1))
# Replace post-tool function with search+todo handling.
regex_replace_once(
    "src/memleaf/host_events.py",
    r"def _handle_codex_post_tool\(.*?\n    return \{\}\n\n@dataclass",
    '''def _handle_codex_post_tool(
    runtime: HostRuntime,
    event: Mapping[str, Any],
    session_id: str | None,
    turn_id: str | None,
) -> dict[str, Any]:
    tool_name = _field(event, "tool_name", "toolName")
    is_search = _codex_memleaf_tool(tool_name, "search")
    is_todos = _codex_memleaf_tool(tool_name, "list_todos")
    if not (is_search or is_todos) or session_id is None or turn_id is None:
        return {}
    tool_input = _field(event, "tool_input", "toolInput")
    call_id = _field(event, "tool_use_id", "toolUseId")
    if not isinstance(tool_input, Mapping) or not isinstance(call_id, str):
        return {}
    if is_search:
        runtime.observe_search(
            session_id=session_id,
            turn_id=turn_id,
            status=_codex_search_status(_field(event, "tool_response", "toolResponse")),
            call_id=call_id,
            supplied_retrieval_id=tool_input.get("retrieval_id"),
        )
        return {}
    status, has_more, next_cursor = _codex_todo_result(_field(event, "tool_response", "toolResponse"))
    runtime.observe_todo_list(
        session_id=session_id,
        turn_id=turn_id,
        status=status,
        call_id=call_id,
        supplied_retrieval_id=tool_input.get("retrieval_id"),
        filter_key=todo_filter_key(tool_input),
        cursor=tool_input.get("cursor") if isinstance(tool_input.get("cursor"), str) else None,
        has_more=has_more,
        next_cursor=next_cursor,
    )
    return {}

@dataclass''',
)

# ---------------------------------------------------------------------------
# Hermes provider: understand list_todos results and instruct exhaustive paging.
# ---------------------------------------------------------------------------
# Result validation permits either search directory or todo directory.
replace_once(
    "src/memleaf/hermes_provider/__init__.py",
    "            valid_results = all(\n                isinstance(item, Mapping)\n                and set(item) == {\"memory_id\", \"title\"}\n                and isinstance(item.get(\"memory_id\"), str)\n                and bool(item.get(\"memory_id\"))\n                and isinstance(item.get(\"title\"), str)\n                and bool(item.get(\"title\"))\n                for item in results\n            )",
    "            valid_results = all(\n                isinstance(item, Mapping)\n                and set(item) in ({\"memory_id\", \"title\"}, {\"memory_id\", \"title\", \"due_date\"})\n                and isinstance(item.get(\"memory_id\"), str)\n                and bool(item.get(\"memory_id\"))\n                and isinstance(item.get(\"title\"), str)\n                and bool(item.get(\"title\"))\n                and (\"due_date\" not in item or item.get(\"due_date\") is None or isinstance(item.get(\"due_date\"), str))\n                for item in results\n            )",
)
replace_once(
    "src/memleaf/hermes_provider/__init__.py",
    "            if call.get(\"name\") != \"mcp__memleaf__search\":\n                continue",
    "            if call.get(\"name\") not in {\"mcp__memleaf__search\", \"mcp__memleaf__list_todos\"}:\n                continue",
)
replace_once(
    "src/memleaf/hermes_provider/__init__.py",
    "            \"Read more only if needed, and do not read all entries to filter unrelated \"\n            \"items. Hermes has a soft \"",
    "            \"Read more only if needed for ordinary relevance queries. When the user asks for current \"\n            \"todos, all unfinished work, urgent work, or work due in a time range, call memleaf MCP \"\n            \"list_todos instead of relevance search; omit scope for a global query, follow every \"\n            \"next_cursor until has_more=false, and read every matching todo body with the same retrieval_id. \"\n            \"Never exclude a todo because another Hermes session or another Agent created it. Hermes has a soft \"",
)
replace_once(
    "src/memleaf/hermes_provider/__init__.py",
    "        \"this map. Search returns a directory; read only the selected memory \"\n        \"when needed. A no-match result is valid.\\n\"",
    "        \"this map. Use list_todos instead of relevance search for global current-todo questions. \"\n        \"Search/list_todos return directories; read selected memory bodies when needed. A no-match result is valid.\\n\"",
)

# Hermes install health check tool count.
replace_once("src/memleaf/adapters/hermes.py", "_MCP_EXPECTED_TOOL_COUNT = 11", "_MCP_EXPECTED_TOOL_COUNT = 12")

# ---------------------------------------------------------------------------
# Prompts: explicit todo classification and due_date contract.
# ---------------------------------------------------------------------------
replace_once(
    "src/memleaf/prompts.py",
    "An explicit implementation plan, project implementation plan, or plan\nadjustment is type project. Do not label it fact merely because it came from\nan email or because no existing project memory was retrieved. Keep an actual\ntodo as todo only when its future use is the action itself, not the durable\nplan it mentions.",
    "An explicit implementation plan, project implementation plan, or durable plan\nadjustment is type project. A concrete unfinished action to repair, investigate,\nprepare material, reply, provide feedback, submit, confirm, deploy, migrate,\nadjust, or follow up is type todo when its future use is the action itself. A\nrequest such as adjusting an implementation plan according to numbered feedback\nis a todo, not a durable project fact. When one passage contains both a durable\nrule/plan and an unfinished action, emit separate project/fact and todo candidates;\nnever discard both merely to avoid mixed_future_use.",
)
replace_once(
    "src/memleaf/prompts.py",
    "Optional fields\nare memory_id, update_memory_id, aliases, keywords, evidence_event_ids,\nshadow_native_ids, scope_operations, scope_source, status, and completed_at.\nstatus and completed_at are only for type=todo; status is active, completed,\nor cancelled, and completed requires completed_at.",
    "Optional fields\nare memory_id, update_memory_id, aliases, keywords, evidence_event_ids,\nshadow_native_ids, scope_operations, scope_source, status, completed_at, and due_date.\nstatus, completed_at, and due_date are only for type=todo; status is active, completed,\nor cancelled, completed requires completed_at, and due_date is null/omitted when no\nexplicit deadline exists or an absolute YYYY-MM-DD supported by current evidence.\nNever guess a due date. On an update, omit due_date to preserve the existing deadline;\nuse due_date=null only when current evidence explicitly removes the deadline.",
)
replace_once(
    "src/memleaf/prompts.py",
    "An explicit implementation plan or plan adjustment must use type project;\npreserve an existing update target's type when it is the same project plan.",
    "An explicit durable implementation plan or plan adjustment must use type project;\npreserve an existing update target's type when it is the same project plan. Concrete\nunfinished execution actions remain todo even when they mention an implementation plan.",
)
replace_once(
    "src/memleaf/prompts.py",
    "Each replacement must contain title, body, tags, type, scopes, scope_source,\naliases, keywords, and source_memory_ids.",
    "Each replacement must contain title, body, tags, type, scopes, scope_source,\naliases, keywords, and source_memory_ids. A todo may also contain status, completed_at,\nand due_date; never merge multiple independent todo source memories into one replacement.",
)

# ---------------------------------------------------------------------------
# Tests: replace legacy read-budget assertions and add global todo regressions.
# ---------------------------------------------------------------------------
regex_replace_once(
    "tests/test_retrieval_gate.py",
    r"    def test_read_budget_is_three_ids_and_six_thousand_body_chars\(self\) -> None:.*?(?=    def test_failed_or_empty_read)",
    '''    def test_read_audit_does_not_limit_ids_or_total_chars(self) -> None:
        retrieval_id = begin_turn(self.vault, "codex", "session", "turn-1")
        observe_search(self.vault, retrieval_id, "found", "read-audit-search")
        calls: list[int] = []

        def reader(allowed_chars: int):
            calls.append(allowed_chars)
            return {"body": "x" * allowed_chars}

        for index in range(8):
            page = guarded_read(self.vault, retrieval_id, f"mem-{index}", reader)
            self.assertEqual(MAX_READ_PAGE_CHARS, len(page["body"]))
        state = validate_turn(self.vault, retrieval_id)
        self.assertEqual(8, state["read_count"])
        self.assertEqual(8 * MAX_READ_PAGE_CHARS, state["read_chars"])
        self.assertEqual([MAX_READ_PAGE_CHARS] * 8, calls)
        self.assertIsNone(MAX_READ_ITEMS)
        self.assertIsNone(MAX_READ_CHARS)

''',
)
# V2 gate: replace two old cap-specific tests.
regex_replace_once(
    "tests/test_v2_gate_limits.py",
    r"    def test_turn_identity_and_read_budget_are_isolated_by_source_session_and_turn\(self\) -> None:.*?(?=    def test_concurrent_reads)",
    '''    def test_turn_identity_and_read_audit_are_isolated_by_source_session_and_turn(self) -> None:
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

''',
)
regex_replace_once(
    "tests/test_v2_gate_limits.py",
    r"    def test_concurrent_reads_share_budget_and_failed_reader_leaks_nothing\(self\) -> None:.*?(?=\n\nif __name__)",
    '''    def test_concurrent_reads_are_all_audited_and_failed_reader_leaks_nothing(self) -> None:
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

''',
)
regex_replace_once(
    "tests/test_v2_mcp_flow.py",
    r"    def test_managed_read_budget_is_shared_across_stdio_reconnects\(self\):.*?(?=    def test_missing_or_stale_retrieval_id)",
    '''    def test_managed_read_audit_allows_many_reads_across_stdio_reconnects(self):
        for index in range(8):
            self.service.create_memory(memory_id=f"budget-{index}", title=f"Item {index}", body="字" * 2200)
        retrieval_id = begin_turn(self.service.vault, "codex", "session", "turn")
        observe_search(self.service.vault, retrieval_id, "found", "budget-search")
        for index in range(4):
            page = self.success("read", memory_id=f"budget-{index}", retrieval_id=retrieval_id, max_chars=99999)
            self.assertEqual(len(page["body"]), 2000)
        self.process.close()
        self.process = MCPProcess(self.service.vault.root)
        self.addCleanup(self.process.close)
        for index in range(4, 8):
            page = self.success("read", memory_id=f"budget-{index}", retrieval_id=retrieval_id)
            self.assertEqual(len(page["body"]), 2000)
        state = validate_turn(self.service.vault, retrieval_id)
        self.assertEqual(state["read_count"], 8)
        self.assertGreater(state["read_chars"], 6000)
        bypass = self.call("search", query="Item", view="full", retrieval_id=retrieval_id)
        self.assertTrue(bypass["isError"])
        self.assertEqual(bypass["structuredContent"]["error"]["code"], "retrieval_full_view_forbidden")

''',
)

# New end-to-end core/MCP todo regressions.
todo_tests = r'''from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from memleaf import Memleaf
from memleaf.mcp_server import _invoke_tool
from memleaf.retrieval_gate import begin_turn, observe_search, observe_todo_list, todo_filter_key, validate_turn


class GlobalTodoRetrievalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="memleaf-global-todo-")
        self.addCleanup(temporary.cleanup)
        self.service = Memleaf(Path(temporary.name) / "vault")

    def todo(self, memory_id, title, *, due_date=None, status="active", scope="global", source="hermes", session="a"):
        return self.service.create_memory(
            memory_id=memory_id,
            title=title,
            body=f"{title} 正文",
            type="todo",
            scopes=[scope],
            status=status,
            due_date=due_date,
            sources=[{"session_id": session, "turn_id": "turn-1", "source": source}],
        )

    def test_global_list_ignores_source_session_and_scope_by_default(self):
        expected = set()
        for index, (scope, source, session) in enumerate([
            ("project:摩根基金", "hermes", "a"),
            ("project:金元顺安", "codex", "b"),
            ("project:鑫元基金", "other", "c"),
            ("project:百年保险", "hermes", "d"),
            ("global", "codex", "e"),
        ]):
            memory = self.todo(f"todo-{index}", f"任务 {index}", scope=scope, source=source, session=session)
            expected.add(memory.memory_id)
        result = self.service.list_todos()
        self.assertEqual(result["status"], "found")
        self.assertEqual({item["memory_id"] for item in result["results"]}, expected)

    def test_status_latest_active_and_history_are_separate(self):
        active = self.todo("todo-state", "状态任务", due_date="2026-09-03")
        self.service.create_memory(memory_id="history-copy", title="旧状态", body="旧 active", type="todo", status="active", due_date="2026-09-02", area="history")
        result = self.service.list_todos()
        self.assertIn(active.memory_id, {item["memory_id"] for item in result["results"]})
        self.assertNotIn("history-copy", {item["memory_id"] for item in result["results"]})
        active.status = "completed"
        active.completed_at = "2026-09-03T01:00:00Z"
        self.service.write_memory(active)
        self.assertNotIn(active.memory_id, {item["memory_id"] for item in self.service.list_todos()["results"]})

    def test_pagination_is_complete_and_stale_when_active_changes(self):
        for index in range(45):
            self.todo(f"todo-page-{index:02}", f"分页任务 {index:02}")
        seen = set()
        page = self.service.list_todos(limit=20)
        first_cursor = page["next_cursor"]
        while True:
            for item in page["results"]:
                self.assertNotIn(item["memory_id"], seen)
                seen.add(item["memory_id"])
            if not page["has_more"]:
                break
            page = self.service.list_todos(limit=20, cursor=page["next_cursor"])
        self.assertEqual(len(seen), 45)
        self.todo("todo-page-new", "新增任务")
        with self.assertRaises(Exception) as raised:
            self.service.list_todos(limit=20, cursor=first_cursor)
        self.assertIn(getattr(raised.exception, "code", ""), {"stale_cursor", "invalid_cursor"})

    def test_mcp_list_todos_allows_read_and_read_metadata(self):
        memory = self.todo("todo-mcp", "MCP 待办", due_date="2026-09-03")
        retrieval_id = begin_turn(self.service.vault, "codex", "session", "turn")
        arguments = {"status": "active", "retrieval_id": retrieval_id}
        result = _invoke_tool(self.service, "list_todos", arguments, request_id=1)
        self.assertFalse(result["isError"], result)
        # Codex normally records PostToolUse; emulate that host observation here.
        value = result["structuredContent"]
        observe_todo_list(
            self.service.vault,
            retrieval_id,
            value["status"],
            "todo-call",
            filter_key=todo_filter_key(arguments),
            cursor=None,
            has_more=value["has_more"],
            next_cursor=value["next_cursor"],
            current_source="codex",
        )
        read = _invoke_tool(self.service, "read", {"memory_id": memory.memory_id, "retrieval_id": retrieval_id})
        self.assertFalse(read["isError"], read)
        self.assertEqual(read["structuredContent"]["type"], "todo")
        self.assertEqual(read["structuredContent"]["status"], "active")
        self.assertEqual(read["structuredContent"]["due_date"], "2026-09-03")

    def test_due_date_round_trip_and_legacy_todo(self):
        memory = self.todo("todo-date", "日期任务", due_date="2026-09-03")
        loaded = self.service.read(memory.memory_id)
        self.assertEqual(loaded.due_date, "2026-09-03")
        legacy = self.service.create_memory(memory_id="todo-legacy", title="旧待办", body="旧格式", type="todo")
        self.assertIsNone(self.service.read(legacy.memory_id).due_date)
        self.assertEqual(self.service.read(legacy.memory_id).status, None)
        self.assertIn("todo-legacy", {item["memory_id"] for item in self.service.list_todos()["results"]})


if __name__ == "__main__":
    unittest.main()
'''
write("tests/test_global_todo_retrieval.py", todo_tests)

# ---------------------------------------------------------------------------
# Documentation and release metadata.
# ---------------------------------------------------------------------------
replace_all(
    "README.md",
    "受管理轮次最多读取 3 个不同记忆、累计 6000 个正文字符；",
    "同一受管理轮次可继续读取所有与问题相关的记忆；`read_count`/`read_chars` 仅用于审计，不再作为阻断配额；",
)
replace_all(
    "README.en.md",
    "managed turns can read at most 3 distinct memories and 6000 body characters;",
    "managed turns may continue reading all memories relevant to the question; `read_count`/`read_chars` are audit counters, not blocking quotas;",
    minimum=1,
)
replace_all("README.md", "验证 MCP Server 能发现 11 个工具", "验证 MCP Server 能发现 12 个工具")
replace_all("README.md", "MCP 的 11 工具验证失败", "MCP 的 12 工具验证失败")
# Add todo workflow paragraph after normal retrieval flow.
replace_once(
    "README.md",
    "5. 根据读取到的关键记忆回答，不把所有候选全部读完。\n\n当前限制：",
    "5. 根据读取到的关键记忆回答，不把所有候选全部读完。\n\n当用户询问当前待办、所有未完成工作、紧急事项或某个日期范围内必须完成的事项时，使用 `list_todos`，默认覆盖所有 Scope；持续分页直到 `has_more=false`，再用同一个 `retrieval_id` 读取所有匹配待办正文。`source/session_id/turn_id` 只用于来源追踪，不参与永久 knowledge 可见性。\n\n当前限制：",
)
replace_once(
    "README.en.md",
    "5. Answer from the verified memory bodies rather than reading every candidate by default.\n\nCurrent limits:",
    "5. Answer from the verified memory bodies rather than reading every candidate by default.\n\nFor current todos, all unfinished work, urgent items, or work due in a date range, use `list_todos`. It covers all scopes by default; follow pagination until `has_more=false`, then read every matching todo body with the same `retrieval_id`. `source/session_id/turn_id` are provenance only and never visibility filters for permanent knowledge.\n\nCurrent limits:",
)
# V2 archived plan: preserve as historical document but annotate superseded read budget.
replace_once(
    "docs/archive/V2_IMPLEMENTATION_PLAN.md",
    "| `read(memory_id, offset?, max_chars?, expected_version?, retrieval_id?)` | 保留版本化分页正文 | 每页最多 2000 正文字符；受管理轮次累计最多 3 个 ID、6000 字符 |",
    "| `read(memory_id, offset?, max_chars?, expected_version?, retrieval_id?)` | 保留版本化分页正文 | 每页最多 2000 正文字符；v0.2.20 起取消每轮 3 个 ID / 6000 字符聚合阻断，计数仅作审计 |",
)
replace_all(
    "docs/archive/V2_IMPLEMENTATION_PLAN.md",
    "并发 3 ID/6000 字符预算",
    "并发读取审计（旧 3 ID/6000 字符预算已在 v0.2.20 取消）",
)
# Version metadata.
replace_once("pyproject.toml", 'version = "0.2.19"', 'version = "0.2.20"')
replace_once("src/memleaf/__init__.py", '__version__ = "0.2.19"', '__version__ = "0.2.20"')
replace_once("src/memleaf/hermes_provider/plugin.yaml", "version: 0.2.19", "version: 0.2.20")
replace_once("README.md", "**当前版本：0.2.19。**", "**当前版本：0.2.20。**")
replace_once("README.md", "memleaf 0.2.19 通过 PyPI 分发", "memleaf 0.2.20 通过 PyPI 分发")
replace_once("README.en.md", "**Version: 0.2.19.**", "**Version: 0.2.20.**")
replace_once("README.en.md", "memleaf 0.2.19 is distributed through PyPI", "memleaf 0.2.20 is distributed through PyPI")
# Changelog entry.
replace_once(
    "CHANGELOG.md",
    "All notable changes to memleaf are documented here.\n",
    "All notable changes to memleaf are documented here.\n\n## 0.2.20 — 2026-09-03\n\n- Add `list_todos` for complete active todo retrieval across all scopes with stable pagination, date/status filtering, and current-turn retrieval gating.\n- Remove the managed-turn 3-memory / 6000-character aggregate read block while retaining the 2000-character page limit and audit counters.\n- Add first-class `todo.due_date` (`YYYY-MM-DD`) with evidence-grounded date normalization, update/history/compaction propagation, and legacy Markdown compatibility.\n- Improve automatic extraction so concrete unfinished actions become atomic todos and durable rules remain project/fact memories; mixed rule/action candidates are split or safely deferred without blocking valid siblings.\n- Keep permanent `knowledge/` globally visible to all sessions and supported Agents sharing the same Vault; provenance fields never filter active memory visibility.\n",
)

print("v0.2.20 patch applied")

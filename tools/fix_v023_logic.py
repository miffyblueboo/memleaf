from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected one occurrence, found {count}: {old[:120]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


# A plan-adjustment phrase is an unfinished action even when it contains the word "plan".
replace_once(
    "src/memleaf/validation.py",
    '''        folded = clause.casefold()
        durable = is_project_plan_text(clause) or any(marker in folded for marker in _PROJECT_RULE_MARKERS)
        todo = (
            (bool(_CALENDAR_TEXT.search(clause)) and any(marker in folded for marker in _DATED_TODO_MARKERS))
            or is_actionable_todo_text(clause)
        ) and not is_project_plan_text(clause)
        if durable == todo:
            return None
''',
    '''        folded = clause.casefold()
        plan = is_project_plan_text(clause)
        explicit_plan_adjustment = bool(_ACTION_PLAN_ADJUST.search(clause))
        todo = (
            (bool(_CALENDAR_TEXT.search(clause)) and any(marker in folded for marker in _DATED_TODO_MARKERS))
            or is_actionable_todo_text(clause)
        ) and (not plan or explicit_plan_adjustment)
        durable = (plan and not explicit_plan_adjustment) or any(
            marker in folded for marker in _PROJECT_RULE_MARKERS
        )
        if durable == todo:
            return None
''',
)

# Ensure helper-generated test turns are genuinely distinct rather than capture-id duplicates.
replace_once(
    "tests/test_v023_scope_correction.py",
    '''        from memleaf.config import save_config
        save_config(self.service.vault.config_path, config)
''',
    '''        from memleaf.config import save_config
        save_config(self.service.vault.config_path, config)
        self._turn_counter = 0
''',
)
replace_once(
    "tests/test_v023_scope_correction.py",
    '''    def _turn(self, user: str):
        self.service.capture("hermes", "s", "t", "user", user, event_id="u")
        self.service.capture("hermes", "s", "t", "assistant", "收到", event_id="a")
        text = self.service.vault.session_path("hermes", "s").read_text(encoding="utf-8")
        return next(turn for turn in parse_inbox_text(text, source="hermes", session_id="s") if turn.complete)
''',
    '''    def _turn(self, user: str):
        self._turn_counter += 1
        turn_id = f"t-{self._turn_counter}"
        self.service.capture("hermes", "s", turn_id, "user", user, event_id=f"u-{self._turn_counter}")
        self.service.capture("hermes", "s", turn_id, "assistant", "收到", event_id=f"a-{self._turn_counter}")
        text = self.service.vault.session_path("hermes", "s").read_text(encoding="utf-8")
        turns = [turn for turn in parse_inbox_text(text, source="hermes", session_id="s") if turn.complete]
        return turns[-1]
''',
)

# Keep the historical defer-only regression focused on an unsafe split: the
# extra unclassified clause means deterministic splitting must refuse it while
# the valid sibling still commits.
path = Path("tests/test_extraction_quality_regressions.py")
text = path.read_text(encoding="utf-8")
start = text.find("    def test_repeated_mixed_future_use_defers_only_bad_candidate")
end = text.find("\n    def ", start + 8)
if start < 0:
    raise SystemExit("legacy mixed-future-use regression not found")
if end < 0:
    end = len(text)
section = text[start:end]
old = '''            memory=(
                "金元顺安实施计划采用达梦和东方通，并要求部署测试环境在"
                "2026-09-10前完成。"
            ),
'''
new = '''            memory=(
                "金元顺安实施计划采用达梦和东方通；背景说明暂未定稿；"
                "并要求部署测试环境在2026-09-10前完成。"
            ),
'''
if section.count(old) != 1:
    raise SystemExit(f"legacy mixed section expected one candidate, found {section.count(old)}")
section = section.replace(old, new, 1)
path.write_text(text[:start] + section + text[end:], encoding="utf-8")

print("v0.2.23 logic refinements applied")

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, text: str) -> None:
    (ROOT / path).write_text(text, encoding="utf-8")


# Existing persisted configs with no capture section (or a partial old capture
# section with neither old nor current evidence mode) historically retained
# metadata only. Preserve that safety behavior in the one-time config
# normalization layer, not in evidence_policy.
path = "src/memleaf/config.py"
text = read(path)
old = '''    capture = normalized.get("capture")
    if capture is not None and not isinstance(capture, Mapping):
        raise ValueError("invalid memleaf capture settings")
    if isinstance(capture, Mapping):
        current = dict(capture)
        if "include_tool_output" in current:
            legacy = current.pop("include_tool_output")
            if type(legacy) is not bool:
                raise ValueError("invalid legacy memleaf capture.include_tool_output")
            migrated_mode = "bounded" if legacy else "metadata"
            explicit = current.get("tool_evidence_mode")
            if explicit is not None and explicit != migrated_mode:
                raise ValueError("conflicting legacy and current tool evidence settings")
            current["tool_evidence_mode"] = migrated_mode
        normalized["capture"] = current
'''
new = '''    capture_present = "capture" in normalized
    capture = normalized.get("capture")
    if capture is not None and not isinstance(capture, Mapping):
        raise ValueError("invalid memleaf capture settings")
    if not capture_present:
        # A persisted pre-policy config with no capture section behaved as
        # metadata-only. A genuinely new Vault never reaches this branch:
        # default_config() writes an explicit current bounded mode.
        normalized["capture"] = {"tool_evidence_mode": "metadata"}
    elif isinstance(capture, Mapping):
        current = dict(capture)
        legacy_present = "include_tool_output" in current
        explicit_present = "tool_evidence_mode" in current
        if legacy_present:
            legacy = current.pop("include_tool_output")
            if type(legacy) is not bool:
                raise ValueError("invalid legacy memleaf capture.include_tool_output")
            migrated_mode = "bounded" if legacy else "metadata"
            explicit = current.get("tool_evidence_mode")
            if explicit is not None and explicit != migrated_mode:
                raise ValueError("conflicting legacy and current tool evidence settings")
            current["tool_evidence_mode"] = migrated_mode
        elif not explicit_present:
            # Old partial capture sections also inherited metadata-only tool
            # evidence behavior before tool_evidence_mode existed.
            current["tool_evidence_mode"] = "metadata"
        normalized["capture"] = current
'''
if old not in text:
    raise SystemExit("config legacy normalization block changed")
write(path, text.replace(old, new, 1))

# Direct policy calls must use the current schema. Old-field translation is
# tested only through load/save config migration.
path = "tests/test_evidence_retention_policy.py"
text = read(path)
old = '''    def test_existing_true_and_explicit_new_setting_have_documented_precedence(self):
        config={'capture':{'include_tool_output':True}}
        record=observation_record('external.inspect','c','RAW_SENTINEL')
        self.assertEqual(retain_tool_evidence([record],config)[0]['content'],'RAW_SENTINEL')
        config['capture']['tool_evidence_mode']='off'
        self.assertEqual(retain_tool_evidence([record],config),[])
        config['capture'].update(include_tool_output=False,tool_evidence_mode='bounded')
        self.assertEqual(retain_tool_evidence([record],config)[0]['content'],'RAW_SENTINEL')
'''
new = '''    def test_current_policy_modes_do_not_understand_legacy_fields(self):
        record=observation_record('external.inspect','c','RAW_SENTINEL')
        bounded={'capture':{'tool_evidence_mode':'bounded','include_attachments':False}}
        self.assertEqual(retain_tool_evidence([record],bounded)[0]['content'],'RAW_SENTINEL')
        off={'capture':{'tool_evidence_mode':'off','include_attachments':False}}
        self.assertEqual(retain_tool_evidence([record],off),[])
        with self.assertRaises(ValueError):
            retain_tool_evidence([record],{'capture':{'include_tool_output':True}})
'''
if old not in text:
    raise SystemExit("evidence legacy direct-policy test changed")
text = text.replace(old, new, 1)
text = text.replace(
    "        for config in ({'tool_evidence_mode':'raw'}, {'tool_evidence_mode':False},\n                       {'include_tool_output':'false'}, {'include_attachments':'false'}):\n",
    "        for config in ({'tool_evidence_mode':'raw'}, {'tool_evidence_mode':False},\n                       {'include_attachments':'false'}):\n",
    1,
)
write(path, text)

# rebuild-index is now strictly derived-index maintenance. Runtime replay and
# idempotency state must remain byte-for-byte untouched, even if it contains an
# entry that is no longer represented by current inbox Markdown.
path = "tests/test_stage_b1.py"
text = read(path)
text = text.replace(
    "    def test_rebuild_preserves_session_state_and_removes_stale_events(self):\n",
    "    def test_rebuild_preserves_all_runtime_state_including_old_event_evidence(self):\n",
    1,
)
text = text.replace(
    "        self.assertNotIn(stale, rebuilt[\"event_keys\"])\n        self.assertNotIn(stale, rebuilt[\"events\"])\n",
    "        self.assertIn(stale, rebuilt[\"event_keys\"])\n        self.assertEqual(rebuilt[\"events\"][stale][\"event_id\"], \"should-not-survive\")\n",
    1,
)
write(path, text)

# First-party installer must stop depending on compatibility no-op flags and
# on the old agents-index API/path. External 0.2.x CLI scripts retain the no-op
# flags until the documented v0.3 sunset.
path = "install.sh"
text = read(path)
text = text.replace(
    '"$venv_path/bin/memleaf" init \\\n  --vault "$vault_path" \\\n  --no-codex \\\n  --no-hermes \\\n  --no-antigravity\n',
    '"$venv_path/bin/memleaf" init \\\n  --vault "$vault_path" \\\n  --no-hermes\n',
    1,
)
text = text.replace("from memleaf.adapters.base import update_agents_index", "from memleaf.adapters.base import update_agents_state")
text = text.replace("if not update_agents_index(", "if not update_agents_state(")
text = text.replace('vault_path / "_index" / "agents.json"', 'vault_path / "_state" / "agents.json"')
text = text.replace("agents index", "agents state")
if "update_agents_index" in text or '"_index" / "agents.json"' in text:
    raise SystemExit("install.sh still depends on old agents index API")
write(path, text)

# Document the preserved absent-capture behavior and that first-party setup no
# longer consumes deprecated CLI flags.
path = "docs/config-migrations.md"
text = read(path)
needle = "When reading an older configuration, `capture.include_tool_output: true` becomes `tool_evidence_mode: bounded`; `false` becomes `metadata`. Saving the normalized configuration writes only the current field. If both legacy and current fields are present but disagree, loading fails closed instead of guessing.\n"
replacement = needle + "A persisted older config with no `capture` section, or with a partial capture section that has neither evidence field, normalizes to `tool_evidence_mode: metadata`, preserving the previous safe metadata-only behavior. New Vaults still write an explicit `bounded` mode.\n"
if needle not in text:
    raise SystemExit("config migration docs normalization paragraph changed")
text = text.replace(needle, replacement, 1)
needle = "`memleaf init --no-codex` and `memleaf init --no-antigravity` are retained only as deprecated no-op argument compatibility for existing 0.2.x setup scripts. They do not select runtime behavior and are scheduled for removal in v0.3. The legacy `init --json` host result slots are likewise retained through 0.2.x so automation does not break during this maintenance release. New integrations must use `memleaf install --host ...` and the current host-state fields.\n"
replacement = needle + "memleaf's own current installers no longer pass the two deprecated no-op flags; only external 0.2.x callers retain that compatibility surface.\n"
if needle not in text:
    raise SystemExit("CLI sunset docs paragraph changed")
write(path, text.replace(needle, replacement, 1))

print("v0.2.28 phase-3 regression fixes applied")

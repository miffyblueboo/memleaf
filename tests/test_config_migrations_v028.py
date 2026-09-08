from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from memleaf.config import default_config, load_config, save_config
from memleaf.frontmatter import dump_yaml


class ConfigMigrationV028Tests(unittest.TestCase):
    def test_legacy_inject_and_tool_output_normalize_then_save_current_only(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memleaf-config-migration-") as temporary:
            path = Path(temporary) / "config.yaml"
            value = default_config(Path(temporary) / "vault")
            value["inject"] = {"mode": "tag_full", "abnormal_guard": True}
            value["capture"].pop("tool_evidence_mode")
            value["capture"]["include_tool_output"] = False
            path.write_text(dump_yaml(value), encoding="utf-8")
            loaded = load_config(path)
            self.assertNotIn("inject", loaded)
            self.assertNotIn("include_tool_output", loaded["capture"])
            self.assertEqual(loaded["capture"]["tool_evidence_mode"], "metadata")
            save_config(path, loaded)
            written = path.read_text(encoding="utf-8")
            self.assertNotIn("inject:", written)
            self.assertNotIn("include_tool_output", written)
            self.assertIn('tool_evidence_mode: "metadata"', written)

    def test_conflicting_legacy_and_current_capture_settings_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memleaf-config-migration-") as temporary:
            path = Path(temporary) / "config.yaml"
            value = default_config(Path(temporary) / "vault")
            value["capture"]["include_tool_output"] = False
            value["capture"]["tool_evidence_mode"] = "bounded"
            path.write_text(dump_yaml(value), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "conflicting legacy and current"):
                load_config(path)

    def test_partial_capture_section_defaults_to_conversation_only(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memleaf-config-migration-") as temporary:
            path = Path(temporary) / "config.yaml"
            value = default_config(Path(temporary) / "vault")
            value["capture"].pop("tool_evidence_mode")
            path.write_text(dump_yaml(value), encoding="utf-8")
            capture = load_config(path)["capture"]
            self.assertEqual(capture["tool_evidence_mode"], "off")
            self.assertFalse(capture["include_attachments"])

    def test_explicit_metadata_mode_remains_an_opt_out(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memleaf-config-migration-") as temporary:
            path = Path(temporary) / "config.yaml"
            value = default_config(Path(temporary) / "vault")
            value["capture"]["tool_evidence_mode"] = "metadata"
            path.write_text(dump_yaml(value), encoding="utf-8")
            self.assertEqual(load_config(path)["capture"]["tool_evidence_mode"], "metadata")


if __name__ == "__main__":
    unittest.main()

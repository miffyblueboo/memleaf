from __future__ import annotations

import hashlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PARTS = ROOT / ".memleaf-patch"


def assemble(prefix: str, target: str, expected_sha256: str) -> None:
    part_paths = sorted(PARTS.glob(f"{prefix}.*.part"))
    if not part_paths:
        raise RuntimeError(f"no patch parts found for {prefix}")
    payload = b"".join(path.read_bytes() for path in part_paths)
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected_sha256:
        raise RuntimeError(
            f"{prefix} payload hash mismatch: expected {expected_sha256}, got {actual}"
        )
    destination = ROOT / target
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)


assemble(
    "installer",
    "src/memleaf/installer.py",
    "b56965f2d8f5501c3391bbbad7ed2a8c012c11fbb5500c24b088951d4252fd08",
)
assemble(
    "cli",
    "src/memleaf/cli.py",
    "73c65ee422a1e4a74992057f4ef105131e4c0d4cca7eec3c6c8daf171312469b",
)
assemble(
    "test",
    "tests/test_hermes_runtime_install.py",
    "734e14dd1bd4cada7cc503a9e002bb3669263d3e181d6a47f6f162b7c288be1e",
)

pypi_test = ROOT / "tests/test_pypi_install.py"
text = pypi_test.read_text(encoding="utf-8")
start_marker = "    def test_installer_rejects_provider_version_mismatch_after_copy(self) -> None:\n"
end_marker = "    @unittest.skipIf(os.name == \"nt\", \"symlink creation is not guaranteed on Windows\")\n"
start = text.find(start_marker)
end = text.find(end_marker, start)
if start < 0 or end < 0:
    raise RuntimeError("could not locate the provider-version regression test")
replacement = '''    def test_installer_rejects_provider_version_mismatch_after_copy(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memleaf-pypi-version-mismatch-") as temporary:
            root = Path(temporary)
            home = root / "home"
            hermes_home = root / ".hermes"
            config_path = hermes_home / "config.yaml"
            vault_path = root / "vault"
            provider_path = hermes_home / "plugins" / "memleaf"
            provider_path.mkdir(parents=True)
            (provider_path / "plugin.yaml").write_text(
                "name: memleaf\\nversion: 0.2.9\\n",
                encoding="utf-8",
            )
            detection = SimpleNamespace(
                detected=True,
                confidence="high",
                executable="hermes",
                config_path=str(config_path),
            )
            initialized = SimpleNamespace(
                root=vault_path,
                agents_index_path=vault_path / "_index" / "agents.json",
            )
            model = {"status": "configured"}
            adapter = mock.Mock()
            adapter.detect.return_value = detection
            adapter.config_path = config_path
            adapter.platform = os.name
            adapter.configure_mcp_lifecycle.return_value = True
            adapter.test_mcp.return_value = True
            configured = SimpleNamespace(
                status="configured",
                reason="configured",
                to_dict=lambda: {"status": "configured", "reason": "configured"},
            )

            with mock.patch("memleaf.installer._home_from_environment", return_value=home), \
                 mock.patch("memleaf.installer._hermes_home", return_value=hermes_home), \
                 mock.patch("memleaf.installer._select_vault_path", return_value=(vault_path, "default")), \
                 mock.patch("memleaf.installer.Vault.initialize", return_value=initialized), \
                 mock.patch("memleaf.installer._prepare_model_route", return_value=model), \
                 mock.patch("memleaf.installer.HermesAdapter", return_value=adapter), \
                 mock.patch("memleaf.installer._memleaf_mcp_command", return_value=root / "memleaf-mcp"), \
                 mock.patch("memleaf.installer._configure_hermes_mcp_entry", return_value=configured), \
                 mock.patch("memleaf.installer._copy_provider", return_value=provider_path), \
                 mock.patch("memleaf.installer._write_provider_config") as write_config:
                result = install_hermes()

            self.assertEqual("failure", result["status"])
            self.assertEqual("provider_version", result["stage"])
            self.assertEqual(__version__, result["core_version"])
            self.assertEqual("0.2.9", result["provider_version"])
            self.assertFalse(result["provider_updated"])
            self.assertEqual("completed", result["rollback_status"])
            self.assertIn("version mismatch", result["reason"])
            write_config.assert_not_called()

'''
pypi_test.write_text(text[:start] + replacement + text[end:], encoding="utf-8")

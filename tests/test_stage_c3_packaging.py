from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))


class StageC3PackagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        cls.project = cls.pyproject["project"]

    def test_project_metadata_and_version_export(self) -> None:
        self.assertEqual(self.project["name"], "memleaf")
        self.assertEqual(self.project["version"], "0.2.9")
        self.assertEqual(self.project["requires-python"], ">=3.11")
        self.assertEqual(self.project["dependencies"], [])
        self.assertEqual(self.project["license"], "MIT")
        self.assertEqual(self.project["license-files"], ["LICENSE"])
        self.assertNotIn("Development Status :: 3 - Alpha", self.project["classifiers"])
        self.assertNotIn("License :: OSI Approved :: MIT License", self.project["classifiers"])
        self.assertNotIn("dynamic", self.project)

        import memleaf

        self.assertEqual(memleaf.__version__, self.project["version"])
        self.assertIn("__version__", memleaf.__all__)

    def test_console_entry_points_are_preserved(self) -> None:
        self.assertEqual(
            self.project["scripts"],
            {
                "memleaf": "memleaf.cli:main",
                "memleaf-mcp": "memleaf.mcp_server:main",
            },
        )
        package_data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["setuptools"]["package-data"]
        self.assertIn("plugin.yaml", package_data["memleaf.hermes_provider"])
        self.assertIn("README.md", package_data["memleaf.hermes_provider"])

    def test_cli_version_matches_package_version(self) -> None:
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(SOURCE_ROOT)
        completed = subprocess.run(
            [sys.executable, "-m", "memleaf.cli", "--version"],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), self.project["version"])

    def test_basic_example_runs_without_default_vault_or_network(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memleaf-c3-home-") as temporary:
            environment = os.environ.copy()
            environment["HOME"] = temporary
            environment["PYTHONPATH"] = str(SOURCE_ROOT)
            completed = subprocess.run(
                [sys.executable, str(ROOT / "examples" / "basic_usage.py")],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(completed.stdout)
            self.assertEqual(result["created"]["title"], "Offline example note")
            self.assertTrue(result["search"])
            self.assertTrue(result["context"])
            self.assertNotIn("body", result["context"][0])
            self.assertEqual(result["created"]["body"], result["read"]["body"])
            self.assertGreaterEqual(result["stats"]["knowledge"], 1)
            self.assertFalse((Path(temporary) / ".memleaf").exists())

    def test_basic_example_accepts_explicit_vault(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memleaf-c3-vault-") as temporary:
            vault = Path(temporary) / "chosen-vault"
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(SOURCE_ROOT)
            completed = subprocess.run(
                [sys.executable, str(ROOT / "examples" / "basic_usage.py"), "--vault", str(vault)],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(completed.stdout)
            self.assertEqual(Path(result["vault"]), vault)
            self.assertTrue((vault / "knowledge").is_dir())

    def test_mcp_ndjson_examples_are_json_requests(self) -> None:
        sample = ROOT / "examples" / "mcp_stdio.ndjson"
        lines = sample.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        requests = [json.loads(line) for line in lines]
        self.assertEqual(requests[0]["method"], "initialize")
        self.assertEqual(requests[1]["method"], "server/discover")
        self.assertEqual(
            requests[1]["params"]["_meta"]["io.modelcontextprotocol/protocolVersion"],
            "2026-07-28",
        )
        self.assertNotIn("/Users/", sample.read_text(encoding="utf-8"))
        self.assertNotIn("secret", sample.read_text(encoding="utf-8").casefold())

    def test_pypi_workflow_uses_the_triggering_ci_artifact_without_rebuilding(self) -> None:
        workflow_path = ROOT / ".github" / "workflows" / "release.yml"
        if not workflow_path.is_file():
            self.skipTest("repository workflows are not included in the source distribution")
        workflow = workflow_path.read_text(encoding="utf-8")
        self.assertNotIn("python -m build", workflow)
        self.assertEqual(workflow.count("github.event.workflow_run.event == 'push'"), 2)
        self.assertEqual(
            workflow.count("github.event.workflow_run.head_repository.full_name == github.repository"),
            2,
        )
        self.assertIn("ref: ${{ github.event.workflow_run.head_sha }}", workflow)
        self.assertIn("name: memleaf-distributions", workflow)
        self.assertIn("github-token: ${{ github.token }}", workflow)
        self.assertIn("repository: ${{ github.repository }}", workflow)
        self.assertIn("run-id: ${{ github.event.workflow_run.id }}", workflow)
        self.assertIn("actions: read", workflow)
        self.assertIn("contents: read", workflow)
        self.assertIn("id-token: write", workflow)

    def test_github_release_update_validates_tag_target_and_clobbers_assets(self) -> None:
        workflow_path = ROOT / ".github" / "workflows" / "ci.yml"
        if not workflow_path.is_file():
            self.skipTest("repository workflows are not included in the source distribution")
        workflow = workflow_path.read_text(encoding="utf-8")
        self.assertIn('gh release view "$TAG"', workflow)
        self.assertIn('--json targetCommitish --jq \'.targetCommitish\'', workflow)
        self.assertIn(
            'gh api "repos/${GITHUB_REPOSITORY}/commits/${TAG}" --jq \'.sha\'',
            workflow,
        )
        self.assertIn('test "$target_sha" = "$RELEASE_SHA"', workflow)
        self.assertIn("gh release upload", workflow)
        self.assertIn("--clobber", workflow)
        self.assertIn("dist/SHA256SUMS", workflow)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def node_start(node: ast.AST) -> int:
    starts = [getattr(node, "lineno", 1)]
    starts.extend(getattr(item, "lineno", 1) for item in getattr(node, "decorator_list", []))
    return min(starts) - 1


def node_text(lines: list[str], node: ast.AST) -> str:
    return "".join(lines[node_start(node): getattr(node, "end_lineno")])


def remove_nodes(source: str, nodes: list[ast.AST]) -> str:
    lines = source.splitlines(True)
    removed: set[int] = set()
    for node in nodes:
        start = node_start(node)
        end = getattr(node, "end_lineno")
        removed.update(range(start, end))
    return "".join(line for index, line in enumerate(lines) if index not in removed)


def split_admission() -> None:
    path = "src/memleaf/admission.py"
    source = read(path)
    tree = ast.parse(source)
    lines = source.splitlines(True)

    syntax_names = {
        "_POLITE", "_QUERY_START", "_QUERY_WORD", "_READ_ONLY_CONTROL", "_EXAMPLE",
        "_HEADING", "_BULLET", "_NEGATIVE_TASK", "_CLOSED_TASK", "_EXTERNAL_OWNER",
    }
    syntax_nodes: list[ast.AST] = []
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = {target.id for target in targets if isinstance(target, ast.Name)}
            if names & syntax_names:
                syntax_nodes.append(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in {"_query", "_clauses"}:
            syntax_nodes.append(node)

    structure_functions = {"_external_blocks", "_has_external_structure", "_structured_external_blocks"}
    structure_nodes = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in structure_functions
    ]
    marker_node = next(
        node for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "_EXTERNAL_MARKER" for target in node.targets)
    )
    max_external_node = next(
        node for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "MAX_EXTERNAL_UNIT_BYTES" for target in node.targets)
    )
    structure_nodes.extend([marker_node, max_external_node])

    syntax_body = "\n\n".join(node_text(lines, node).rstrip() for node in sorted(syntax_nodes, key=node_start))
    structure_body = "\n\n".join(node_text(lines, node).rstrip() for node in sorted(structure_nodes, key=node_start))
    write(
        "src/memleaf/evidence_syntax.py",
        '"""Source-neutral syntax recognizers used by evidence admission."""\n'
        "from __future__ import annotations\n\nimport re\nfrom typing import Iterable\n\n" + syntax_body + "\n",
    )
    write(
        "src/memleaf/evidence_structure.py",
        '"""Deterministic structural segmentation for evidence records."""\n'
        "from __future__ import annotations\n\nimport json\nimport re\nfrom typing import Iterable\n\n" + structure_body + "\n",
    )

    updated = remove_nodes(source, syntax_nodes + structure_nodes)
    anchor = "from .validation import ModelOutputError, parse_strict_json\n"
    imports = anchor + (
        "from .evidence_syntax import (\n"
        "    _BULLET, _CLOSED_TASK, _EXAMPLE, _EXTERNAL_OWNER, _HEADING, _NEGATIVE_TASK,\n"
        "    _POLITE, _QUERY_START, _QUERY_WORD, _READ_ONLY_CONTROL, _clauses, _query,\n"
        ")\n"
        "from .evidence_structure import (\n"
        "    MAX_EXTERNAL_UNIT_BYTES, _EXTERNAL_MARKER, _external_blocks,\n"
        "    _has_external_structure, _structured_external_blocks,\n"
        ")\n"
    )
    if anchor not in updated:
        raise RuntimeError("admission import anchor changed")
    updated = updated.replace(anchor, imports, 1)
    ast.parse(updated)
    ast.parse(read("src/memleaf/evidence_syntax.py"))
    ast.parse(read("src/memleaf/evidence_structure.py"))
    write(path, updated)
    if (ROOT / path).stat().st_size >= 40 * 1024:
        raise RuntimeError(f"admission.py is still too large: {(ROOT / path).stat().st_size}")


def standalone_import_prelude(module_file: str, module_name: str) -> str:
    return f'''from __future__ import annotations\n\ntry:\n    from .{module_file} import *\nexcept (ImportError, ValueError):\n    import importlib.util as _importlib_util\n    import sys as _sys\n    from pathlib import Path as _Path\n\n    _name = "_memleaf_hermes_{module_name}"\n    _module = _sys.modules.get(_name)\n    if _module is None:\n        _spec = _importlib_util.spec_from_file_location(_name, _Path(__file__).with_name("{module_file}.py"))\n        if _spec is None or _spec.loader is None:\n            raise ImportError("Hermes provider module {module_file} is unavailable")\n        _module = _importlib_util.module_from_spec(_spec)\n        _sys.modules[_name] = _module\n        _spec.loader.exec_module(_module)\n    globals().update({{name: getattr(_module, name) for name in getattr(_module, "__all__", ())}})\n\n'''


def split_hermes_provider() -> None:
    path = "src/memleaf/hermes_provider/__init__.py"
    source = read(path)
    tree = ast.parse(source)
    lines = source.splitlines(True)
    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
    client = classes.get("_MCPClient")
    provider = classes.get("MemleafMemoryProvider")
    if client is None or provider is None:
        raise RuntimeError("Hermes provider class layout changed")
    client_start = node_start(client)
    provider_start = node_start(provider)
    if not (0 < client_start < provider_start):
        raise RuntimeError("Hermes provider class order changed")

    shared = "".join(lines[:client_start]).rstrip() + "\n\n__all__ = [name for name in globals() if not name.startswith('__')]\n"
    client_body = "".join(lines[client_start:provider_start]).strip() + "\n"
    provider_body = "".join(lines[provider_start:]).strip() + "\n"
    # The old file has its own future import; only the shared module keeps that original preamble.
    write("src/memleaf/hermes_provider/_shared.py", shared)
    write(
        "src/memleaf/hermes_provider/_mcp_client.py",
        '"""Hermes stdio MCP transport and result-normalization internals."""\n'
        + standalone_import_prelude("_shared", "shared")
        + client_body
        + "\n__all__ = [name for name in globals() if not name.startswith('__')]\n",
    )
    write(
        "src/memleaf/hermes_provider/_provider.py",
        '"""Hermes MemoryProvider lifecycle, retrieval and capture orchestration."""\n'
        + standalone_import_prelude("_mcp_client", "mcp_client")
        + provider_body
        + "\n__all__ = [name for name in globals() if not name.startswith('__')]\n",
    )
    facade = '''"""Public Hermes MemoryProvider plugin surface.\n\nImplementation is split by responsibility so this package entry point remains reviewable.\nThe fallback loader preserves Hermes' standalone plugin-directory loading behavior.\n"""\nfrom __future__ import annotations\n\ntry:\n    from . import _provider as _impl\nexcept (ImportError, ValueError):\n    import importlib.util as _importlib_util\n    import sys as _sys\n    from pathlib import Path as _Path\n\n    _name = "_memleaf_hermes_provider_impl"\n    _impl = _sys.modules.get(_name)\n    if _impl is None:\n        _spec = _importlib_util.spec_from_file_location(_name, _Path(__file__).with_name("_provider.py"))\n        if _spec is None or _spec.loader is None:\n            raise ImportError("Hermes provider implementation is unavailable")\n        _impl = _importlib_util.module_from_spec(_spec)\n        _sys.modules[_name] = _impl\n        _spec.loader.exec_module(_impl)\n\nMemleafMemoryProvider = _impl.MemleafMemoryProvider\nregister = _impl.register\n\n__all__ = ["MemleafMemoryProvider", "register"]\n\ndef __getattr__(name: str):\n    return getattr(_impl, name)\n\ndef __dir__():\n    return sorted(set(globals()) | set(dir(_impl)))\n'''
    write(path, facade)
    for module in ("_shared.py", "_mcp_client.py", "_provider.py", "__init__.py"):
        ast.parse(read("src/memleaf/hermes_provider/" + module))
    if (ROOT / path).stat().st_size >= 8 * 1024:
        raise RuntimeError("Hermes provider facade is unexpectedly large")
    if "def register" not in provider_body:
        raise RuntimeError("Hermes provider register entry point was not preserved")

    installer_path = "src/memleaf/installer.py"
    installer = read(installer_path)
    old = 'required = ("__init__.py", "evidence_budget.py", "plugin.yaml", "README.md")'
    new = 'required = ("__init__.py", "_shared.py", "_mcp_client.py", "_provider.py", "evidence_budget.py", "plugin.yaml", "README.md")'
    if old not in installer:
        raise RuntimeError("installer provider resource list changed")
    write(installer_path, installer.replace(old, new, 1))


def split_test_file(path: str, class_name: str, support_module: str, base_name: str, part_prefix: str, target_bytes: int = 30 * 1024) -> list[str]:
    source = read(path)
    tree = ast.parse(source)
    lines = source.splitlines(True)
    test_class = next((node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name), None)
    if test_class is None:
        raise RuntimeError(f"{path}: class {class_name} not found")
    class_start = node_start(test_class)
    preamble = "".join(lines[:class_start]).rstrip() + "\n\n"
    if path.endswith("test_hermes_provider.py"):
        expected = "return module, MemoryProvider"
        if expected not in preamble:
            raise RuntimeError("Hermes test loader shape changed")
        preamble = preamble.replace(expected, "return module._impl, MemoryProvider", 1)

    helpers: list[ast.AST] = []
    tests: list[ast.AST] = []
    for node in test_class.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
            tests.append(node)
        else:
            helpers.append(node)
    if not tests:
        raise RuntimeError(f"{path}: no test methods found")

    support = preamble + f"class {base_name}(unittest.TestCase):\n"
    if helpers:
        support += "\n\n".join(node_text(lines, node).rstrip() for node in helpers) + "\n"
    else:
        support += "    pass\n"
    support += "\n__all__ = [name for name in globals() if not name.startswith('__')]\n"
    write("tests/" + support_module + ".py", support)

    chunks: list[list[str]] = []
    current: list[str] = []
    current_bytes = 0
    for node in tests:
        chunk = node_text(lines, node).rstrip() + "\n"
        size = len(chunk.encode("utf-8"))
        if current and current_bytes + size > target_bytes:
            chunks.append(current)
            current = []
            current_bytes = 0
        current.append(chunk)
        current_bytes += size
    if current:
        chunks.append(current)

    modules: list[str] = []
    for index, chunk_list in enumerate(chunks, 1):
        stem = f"{part_prefix}_{index:02d}"
        modules.append("tests." + stem)
        body = f"from tests.{support_module} import *\n\n\nclass {class_name}Part{index:02d}({base_name}):\n"
        body += "\n\n".join(item.rstrip() for item in chunk_list) + "\n"
        write("tests/" + stem + ".py", body)
        ast.parse(body)
        if len(body.encode("utf-8")) > 40 * 1024:
            raise RuntimeError(f"split test part still oversized: {stem}")

    # Keep a tiny import-compatibility facade for tests/modules that import support symbols.
    postamble = "".join(lines[getattr(test_class, "end_lineno"):])
    stub = f'"""Compatibility facade; tests are split across cohesive part modules."""\nfrom tests.{support_module} import *\n\n{class_name} = {base_name}\n'
    write(path, stub + "\n" + postamble)
    return modules


def split_large_tests() -> list[str]:
    hermes_modules = split_test_file(
        "tests/test_hermes_provider.py", "HermesProviderTests", "hermes_provider_support",
        "HermesProviderTestBase", "test_hermes_provider",
    )
    split_test_file(
        "tests/test_stage_b2a.py", "StageB2ATest", "stage_b2a_support",
        "StageB2ATestBase", "test_stage_b2a",
    )
    split_test_file(
        "tests/test_stage_b1.py", "StageB1Test", "stage_b1_support",
        "StageB1TestBase", "test_stage_b1",
    )
    return hermes_modules


def clean_benchmarks_and_packaging(hermes_modules: list[str]) -> None:
    for relative in (
        "scripts/benchmark_long_run.py",
        "benchmarks/results/v0.2.28-linux.json",
        "docs/performance.md",
    ):
        target = ROOT / relative
        if target.exists():
            target.unlink()

    manifest_path = "MANIFEST.in"
    manifest = read(manifest_path)
    manifest = "\n".join(
        line for line in manifest.splitlines()
        if line.strip() != "recursive-include tests *.py"
    ).rstrip() + "\n"
    write(manifest_path, manifest)

    ci_path = ".github/workflows/ci.yml"
    ci = read(ci_path)
    old_target = "tests.test_hermes_provider tests.test_hermes_stdio_transport"
    new_target = " ".join(hermes_modules + ["tests.test_hermes_stdio_transport"])
    if ci.count(old_target) != 2:
        raise RuntimeError("CI Hermes acceptance target changed")
    ci = ci.replace(old_target, new_target)
    old_sdist = '''      - name: Verify tests from the source distribution\n        run: |\n          mkdir -p "$RUNNER_TEMP/memleaf-sdist"\n          tar -xzf dist/memleaf-*.tar.gz -C "$RUNNER_TEMP/memleaf-sdist"\n          cd "$RUNNER_TEMP"/memleaf-sdist/memleaf-*\n          PYTHONPATH=src python -m unittest discover -s tests -p 'test_*.py'\n'''
    new_sdist = '''      - name: Verify source distribution is runtime-only and installable\n        run: |\n          python - <<'PY'\n          import glob\n          import tarfile\n          archive = glob.glob("dist/memleaf-*.tar.gz")[0]\n          with tarfile.open(archive, "r:gz") as bundle:\n              names = bundle.getnames()\n          forbidden = ("/tests/", "benchmark_long_run.py", "docs/performance.md", "benchmarks/results/")\n          assert not any(any(marker in name for marker in forbidden) for name in names), [\n              name for name in names if any(marker in name for marker in forbidden)\n          ][:20]\n          PY\n          python -m pip install --force-reinstall dist/*.tar.gz\n          python -c "import memleaf; print(memleaf.__version__)"\n'''
    if old_sdist not in ci:
        raise RuntimeError("CI sdist verification block changed")
    write(ci_path, ci.replace(old_sdist, new_sdist, 1))

    contributing_path = "CONTRIBUTING.md"
    contributing = read(contributing_path).rstrip() + "\n\n"
    note = '''## Maintainability\n\nKeep package entry points thin and split modules/tests by responsibility before unrelated behaviors accumulate in one file. Regression coverage belongs in the repository, but test modules should stay reviewable; tests are not shipped in release source distributions. Reproducible performance investigations should use temporary or external benchmark artifacts rather than committed large-run result fixtures in `main`.\n'''
    if "## Maintainability" not in contributing:
        contributing += note
    write(contributing_path, contributing)


def validate_layout(original_test_count: int) -> None:
    for relative in (
        "scripts/benchmark_long_run.py",
        "benchmarks/results/v0.2.28-linux.json",
        "docs/performance.md",
    ):
        if (ROOT / relative).exists():
            raise RuntimeError(f"benchmark asset still exists: {relative}")
    for path in (ROOT / "src").rglob("*.py"):
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for path in (ROOT / "tests").glob("*.py"):
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    current_test_count = 0
    for path in (ROOT / "tests").glob("test_*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        current_test_count += sum(
            1 for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")
        )
    if current_test_count != original_test_count:
        raise RuntimeError(f"test method count changed: {original_test_count} -> {current_test_count}")
    print("preserved test methods:", current_test_count)
    print("admission.py bytes:", (ROOT / "src/memleaf/admission.py").stat().st_size)
    print("hermes_provider/__init__.py bytes:", (ROOT / "src/memleaf/hermes_provider/__init__.py").stat().st_size)
    biggest = sorted(
        ((path.stat().st_size, path.relative_to(ROOT)) for path in (ROOT / "tests").glob("test_*.py")),
        reverse=True,
    )[:8]
    print("largest test modules:", biggest)


def main() -> None:
    original_test_count = 0
    for path in (ROOT / "tests").glob("test_*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        original_test_count += sum(
            1 for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")
        )
    split_admission()
    split_hermes_provider()
    hermes_modules = split_large_tests()
    clean_benchmarks_and_packaging(hermes_modules)
    validate_layout(original_test_count)


if __name__ == "__main__":
    main()

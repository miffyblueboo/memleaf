#!/usr/bin/env python3
from pathlib import Path

VERSION = "0.2.28"
DATE = "2026-09-06"


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise SystemExit(f"{path}: expected exactly one {old!r}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def main() -> int:
    replace_once(Path("pyproject.toml"), 'version = "0.2.27"', f'version = "{VERSION}"')
    replace_once(Path("src/memleaf/__init__.py"), '__version__ = "0.2.27"', f'__version__ = "{VERSION}"')

    changelog = Path("CHANGELOG.md")
    text = changelog.read_text(encoding="utf-8")
    marker = "## 0.2.27 — 2026-09-05\n"
    if marker not in text:
        raise SystemExit("CHANGELOG 0.2.27 anchor missing")
    if f"## {VERSION} " in text:
        raise SystemExit(f"CHANGELOG {VERSION} already exists")

    section = f'''## {VERSION} — {DATE}

- Separate rebuildable derived data in `_index/` from correctness/runtime state in `_state/`. Existing Vaults migrate processed-event, agent activation, host-ingest, retrieval-gate and compaction state crash-safely and idempotently; conflicting or corrupt legacy/current state fails closed, and `rebuild-index` never rewrites runtime state.
- Normalize supported legacy configuration into current names, including `capture.include_tool_output` to `capture.tool_evidence_mode`, reject conflicting legacy/current settings, preserve historical safe defaults, and document 0.2.x compatibility/deprecation behavior in `docs/config-migrations.md`.
- Move host activation bookkeeping to `_state/agents.json`, update installation/status paths accordingly, and keep Hermes/Codex shared-Vault behavior, permanent-memory global visibility, provenance-only session/source metadata, and native Hermes memory coexistence unchanged.
- Remove the remaining Core urgency-word classifier for unscheduled todos so ordering stays source-neutral; model-owned business semantics remain outside deterministic Core validation.
- Add reproducible 1k/10k/50k long-run benchmarking across retrieval, writes, lifecycle, locks, RSS and disk. The 50k-active dataset is explicitly an extreme stress boundary rather than a normal steady-state assumption; normal retrieval remains `Scope Map -> scope-constrained search -> read`, while UPDATE/NO_CHANGE, todo retirement, bounded history and compaction control active-memory growth.
- Do not introduce SQLite/FTS, vector storage, external databases, daemons or new runtime dependencies. Markdown remains the sole source of truth and `_index/` remains fully deletable/rebuildable.

'''
    changelog.write_text(text.replace(marker, section + marker, 1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

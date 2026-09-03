from pathlib import Path


def replace_exact(path: str, old: str, new: str, *, count: int | None = None) -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    occurrences = text.count(old)
    if occurrences == 0:
        raise SystemExit(f"{path}: missing {old!r}")
    if count is not None and occurrences < count:
        raise SystemExit(f"{path}: expected at least {count} occurrences of {old!r}, found {occurrences}")
    p.write_text(text.replace(old, new, count if count is not None else -1), encoding="utf-8")


replace_exact("pyproject.toml", 'version = "0.2.22"', 'version = "0.2.23"')
replace_exact("src/memleaf/__init__.py", '__version__ = "0.2.22"', '__version__ = "0.2.23"')
replace_exact("src/memleaf/hermes_provider/plugin.yaml", "version: 0.2.22", "version: 0.2.23")
replace_exact("README.md", "0.2.22", "0.2.23", count=2)
replace_exact("README.en.md", "0.2.22", "0.2.23", count=2)

changelog = Path("CHANGELOG.md")
text = changelog.read_text(encoding="utf-8")
anchor = "All notable changes to memleaf are documented here.\n\n"
if text.count(anchor) != 1:
    raise SystemExit("CHANGELOG anchor mismatch")
entry = """## 0.2.23 — 2026-09-03

- Add a strict `scope_correction` transaction for explicit cross-project corrections without weakening ordinary UPDATE scope isolation: uniquely recover the wrong active target, reuse its `memory_id` when appropriate, or retire it to history with `invalidated_reason: scope_correction` and `superseded_by` when a correct active survivor already exists.
- Deterministically split same-project `mixed_future_use` candidates after bounded model retries only when every clause is safely classifiable as durable project/fact state or an unfinished todo; ambiguous fragments remain deferred and valid siblings continue independently.
- Add bounded mail-tool evidence capture limited to message ID, subject, sender and sender domain, plus private per-scope domain identifiers; a unique domain/scope conflict defers extraction instead of writing a wrongly attributed project memory, while full tool output remains excluded.
- Preserve global `list_todos` pagination and unlimited aggregate managed reads from 0.2.20; no source/session ownership filter or aggregate read quota is reintroduced.
- Hermes still uses its existing Soft Gate in this package release. Generic fail-closed pre-final retrieval enforcement requires a Hermes host lifecycle hook and is tracked upstream in NousResearch/hermes-agent#101973.

"""
changelog.write_text(text.replace(anchor, anchor + entry, 1), encoding="utf-8")

print("v0.2.23 release metadata prepared")

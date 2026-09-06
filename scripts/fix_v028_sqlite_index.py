from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, text: str) -> None:
    (ROOT / path).write_text(text, encoding="utf-8")


# `_index/` is explicitly disposable. A user may delete the whole directory;
# the next explicit rebuild must recreate the directory before opening the
# temporary SQLite database.
path = "src/memleaf/sqlite_index.py"
text = read(path)
old = '''    def _full_rebuild(self) -> dict[str, int]:
        if self.path.is_symlink():
            raise DerivedIndexError("unsafe SQLite index path")
        temporary = self.path.with_name(self.path.name + ".tmp")
'''
new = '''    def _full_rebuild(self) -> dict[str, int]:
        if self.path.is_symlink():
            raise DerivedIndexError("unsafe SQLite index path")
        parent = self.path.parent
        if parent.exists() and (parent.is_symlink() or not parent.is_dir()):
            raise DerivedIndexError("unsafe SQLite index directory")
        parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
'''
if text.count(old) != 1:
    raise SystemExit("SQLite full rebuild insertion point changed")
text = text.replace(old, new, 1)
write(path, text)

# `tags.json` remains a supported disposable compatibility index. Search no
# longer consumes it for candidate selection, but it must still self-heal when
# an older installation or a user corrupts/removes legacy fields. Validation
# happens before the SQLite lookup; rebuilding it never touches `_state/`.
path = "src/memleaf/service.py"
text = read(path)
old = '''        from .sqlite_index import DerivedSearchIndex

        indexed = DerivedSearchIndex(self.vault).search(
'''
new = '''        # Preserve the historical self-healing contract of `_index/tags.json`.
        # This is a disposable compatibility index; validation/rebuild is
        # intentionally independent from the SQLite candidate accelerator.
        self._read_tags_index_unlocked()
        from .sqlite_index import DerivedSearchIndex

        indexed = DerivedSearchIndex(self.vault).search(
'''
if text.count(old) != 1:
    raise SystemExit("service search insertion point changed")
text = text.replace(old, new, 1)
write(path, text)

print("v0.2.28 SQLite rebuild compatibility fixes applied")

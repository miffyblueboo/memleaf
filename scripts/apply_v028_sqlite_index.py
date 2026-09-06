from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, text: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


sqlite_module = r'''"""Disposable SQLite/FTS5 acceleration derived entirely from Markdown.

The database lives under ``_index/`` and is never a source of truth.  It may be
removed at any time; explicit ``rebuild-index`` recreates it from Markdown.
Runtime/recovery state is deliberately excluded from this module.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .index import build_tags_index, extract_wikilinks, normalize_term
from .models import Memory
from .retrieval import (
    candidate_matches_query,
    filter_by_scope,
    fulltext_score,
    inherited_scopes,
    matching_index_terms,
    memory_scope_rank,
    query_terms,
)


SCHEMA_VERSION = 1
_KINDS = ("tags", "aliases", "keywords", "wikilinks")
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


class DerivedIndexError(RuntimeError):
    """A disposable derived-index failure."""


@dataclass
class IndexedRecord:
    memory: Memory
    path: Path
    area: str
    score: int = 0
    scope_rank: int = 0


class DerivedSearchIndex:
    """Rebuildable local SQLite index; Markdown remains authoritative."""

    def __init__(self, vault: Any):
        self.vault = vault
        self.path = Path(vault.search_index_path)

    @staticmethod
    def _connect(path: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA temp_store=MEMORY")
        return connection

    @staticmethod
    def _initialize(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS files (
                relpath TEXT PRIMARY KEY,
                area TEXT NOT NULL,
                mtime_ns INTEGER NOT NULL,
                size INTEGER NOT NULL,
                valid INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS memories (
                relpath TEXT PRIMARY KEY,
                area TEXT NOT NULL,
                memory_id TEXT NOT NULL,
                normalized_id TEXT NOT NULL,
                normalized_title TEXT NOT NULL,
                memory_type TEXT NOT NULL,
                status TEXT,
                due_date TEXT,
                updated TEXT NOT NULL,
                hit_count INTEGER NOT NULL,
                memory_json TEXT NOT NULL,
                search_blob TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS memories_id_idx
                ON memories(memory_id, area);
            CREATE INDEX IF NOT EXISTS memories_normalized_id_idx
                ON memories(normalized_id, area);
            CREATE INDEX IF NOT EXISTS memories_title_idx
                ON memories(normalized_title, area);
            CREATE INDEX IF NOT EXISTS memories_todo_idx
                ON memories(memory_type, area, status, due_date);
            CREATE TABLE IF NOT EXISTS scopes (
                relpath TEXT NOT NULL,
                scope TEXT NOT NULL,
                PRIMARY KEY(relpath, scope)
            );
            CREATE INDEX IF NOT EXISTS scopes_scope_idx ON scopes(scope, relpath);
            CREATE TABLE IF NOT EXISTS terms (
                relpath TEXT NOT NULL,
                kind TEXT NOT NULL,
                term TEXT NOT NULL,
                PRIMARY KEY(relpath, kind, term)
            );
            CREATE INDEX IF NOT EXISTS terms_term_idx ON terms(kind, term, relpath);
            """
        )
        try:
            connection.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                    relpath UNINDEXED,
                    title,
                    body,
                    tags,
                    aliases,
                    keywords,
                    wikilinks,
                    tokenize='unicode61 remove_diacritics 2'
                )
                """
            )
        except sqlite3.DatabaseError as error:
            raise DerivedIndexError("stdlib SQLite FTS5 is unavailable") from error
        connection.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )

    @staticmethod
    def _schema_ok(connection: sqlite3.Connection) -> bool:
        try:
            row = connection.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            if row is None or row[0] != str(SCHEMA_VERSION):
                return False
            connection.execute("SELECT count(*) FROM memory_fts").fetchone()
            return True
        except sqlite3.DatabaseError:
            return False

    def _managed_path(self, path: Path) -> tuple[str, str]:
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(self.vault.root).as_posix()
        except ValueError as error:
            raise DerivedIndexError("derived index source escapes Vault") from error
        if relative.startswith("knowledge/"):
            return relative, "knowledge"
        if relative.startswith("history/"):
            return relative, "history"
        raise DerivedIndexError("invalid derived index source area")

    @staticmethod
    def _delete_record(connection: sqlite3.Connection, relpath: str) -> None:
        connection.execute("DELETE FROM memory_fts WHERE relpath = ?", (relpath,))
        connection.execute("DELETE FROM terms WHERE relpath = ?", (relpath,))
        connection.execute("DELETE FROM scopes WHERE relpath = ?", (relpath,))
        connection.execute("DELETE FROM memories WHERE relpath = ?", (relpath,))

    @staticmethod
    def _memory_payload(memory: Memory) -> str:
        return json.dumps(
            memory.to_dict(),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _search_blob(memory: Memory) -> str:
        return normalize_term(
            "\n".join(
                [
                    memory.title,
                    memory.body,
                    *memory.tags,
                    *memory.aliases,
                    *memory.keywords,
                ]
            )
        )

    def _insert_memory(
        self,
        connection: sqlite3.Connection,
        *,
        relpath: str,
        area: str,
        memory: Memory,
    ) -> None:
        wikilinks = extract_wikilinks(memory.body)
        connection.execute(
            """
            INSERT INTO memories(
                relpath, area, memory_id, normalized_id, normalized_title,
                memory_type, status, due_date, updated, hit_count,
                memory_json, search_blob
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                relpath,
                area,
                memory.memory_id,
                normalize_term(memory.memory_id),
                normalize_term(memory.title),
                memory.type,
                memory.status,
                memory.due_date,
                memory.updated,
                memory.hit_count,
                self._memory_payload(memory),
                self._search_blob(memory),
            ),
        )
        connection.executemany(
            "INSERT OR IGNORE INTO scopes(relpath, scope) VALUES(?, ?)",
            ((relpath, scope) for scope in memory.scopes),
        )
        for kind, values in (
            ("tags", memory.tags),
            ("aliases", memory.aliases),
            ("keywords", memory.keywords),
            ("wikilinks", wikilinks),
        ):
            connection.executemany(
                "INSERT OR IGNORE INTO terms(relpath, kind, term) VALUES(?, ?, ?)",
                (
                    (relpath, kind, term)
                    for raw in values
                    if (term := normalize_term(raw))
                ),
            )
        connection.execute(
            """
            INSERT INTO memory_fts(relpath, title, body, tags, aliases, keywords, wikilinks)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                relpath,
                memory.title,
                memory.body,
                "\n".join(memory.tags),
                "\n".join(memory.aliases),
                "\n".join(memory.keywords),
                "\n".join(wikilinks),
            ),
        )

    def _upsert_path(self, connection: sqlite3.Connection, path: Path) -> None:
        relpath, area = self._managed_path(path)
        try:
            stat = path.stat()
        except OSError as error:
            raise DerivedIndexError("cannot stat Markdown source") from error
        self._delete_record(connection, relpath)
        valid = 0
        try:
            memory = Memory.from_markdown(path.read_text(encoding="utf-8"), path)
        except (OSError, UnicodeError, ValueError):
            memory = None
        if memory is not None:
            self._insert_memory(connection, relpath=relpath, area=area, memory=memory)
            valid = 1
        connection.execute(
            """
            INSERT OR REPLACE INTO files(relpath, area, mtime_ns, size, valid)
            VALUES(?, ?, ?, ?, ?)
            """,
            (relpath, area, int(stat.st_mtime_ns), int(stat.st_size), valid),
        )

    def _all_source_paths(self) -> list[Path]:
        return [
            *self.vault.list_markdown("knowledge"),
            *self.vault.list_markdown("history"),
        ]

    def _full_rebuild(self) -> dict[str, int]:
        if self.path.is_symlink():
            raise DerivedIndexError("unsafe SQLite index path")
        temporary = self.path.with_name(self.path.name + ".tmp")
        for suffix in ("", "-journal", "-wal", "-shm"):
            candidate = Path(str(temporary) + suffix)
            if candidate.exists() or candidate.is_symlink():
                if candidate.is_symlink() or not candidate.is_file():
                    raise DerivedIndexError("unsafe temporary SQLite index path")
                candidate.unlink()
        connection = self._connect(temporary)
        try:
            self._initialize(connection)
            with connection:
                for path in self._all_source_paths():
                    self._upsert_path(connection, path)
            connection.execute("PRAGMA optimize")
        finally:
            connection.close()
        os.replace(temporary, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        return self.counts()

    def ensure_ready(self) -> None:
        if self.path.is_symlink():
            raise DerivedIndexError("unsafe SQLite index path")
        if not self.path.exists():
            self._full_rebuild()
            return
        try:
            connection = self._connect(self.path)
            try:
                if not self._schema_ok(connection):
                    raise DerivedIndexError("stale SQLite index schema")
            finally:
                connection.close()
        except (sqlite3.DatabaseError, DerivedIndexError):
            self._full_rebuild()

    def sync(self, *, full: bool = False) -> dict[str, int]:
        """Synchronize changed Markdown; ``full`` recreates the database."""

        if full:
            return self._full_rebuild()
        self.ensure_ready()
        current: dict[str, tuple[Path, str, int, int]] = {}
        for path in self._all_source_paths():
            relpath, area = self._managed_path(path)
            try:
                stat = path.stat()
            except OSError:
                continue
            current[relpath] = (path, area, int(stat.st_mtime_ns), int(stat.st_size))
        connection = self._connect(self.path)
        try:
            known = {
                row["relpath"]: (int(row["mtime_ns"]), int(row["size"]))
                for row in connection.execute("SELECT relpath, mtime_ns, size FROM files")
            }
            with connection:
                for relpath in sorted(set(known) - set(current)):
                    self._delete_record(connection, relpath)
                    connection.execute("DELETE FROM files WHERE relpath = ?", (relpath,))
                for relpath, (path, _area, mtime_ns, size) in current.items():
                    if known.get(relpath) == (mtime_ns, size):
                        continue
                    self._upsert_path(connection, path)
        except sqlite3.DatabaseError:
            connection.close()
            return self._full_rebuild()
        finally:
            try:
                connection.close()
            except Exception:
                pass
        return self.counts()

    def refresh_paths(self, paths: Iterable[Path]) -> None:
        """Refresh known writes without scanning the whole Vault."""

        self.ensure_ready()
        connection = self._connect(self.path)
        try:
            with connection:
                for path in paths:
                    path = Path(path)
                    relpath, _area = self._managed_path(path)
                    if path.exists() and path.is_file() and not path.is_symlink():
                        self._upsert_path(connection, path)
                    else:
                        self._delete_record(connection, relpath)
                        connection.execute("DELETE FROM files WHERE relpath = ?", (relpath,))
        finally:
            connection.close()

    def counts(self) -> dict[str, int]:
        self.ensure_ready()
        connection = self._connect(self.path)
        try:
            result = {"knowledge": 0, "history": 0}
            for row in connection.execute(
                "SELECT area, count(*) AS count FROM memories GROUP BY area"
            ):
                if row["area"] in result:
                    result[row["area"]] = int(row["count"])
            return result
        finally:
            connection.close()

    @staticmethod
    def _decode(row: sqlite3.Row, vault: Any) -> IndexedRecord:
        try:
            memory = Memory.from_mapping(json.loads(row["memory_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise DerivedIndexError("invalid derived memory payload") from error
        relpath = str(row["relpath"])
        path = vault.root / Path(relpath)
        return IndexedRecord(memory=memory, path=path, area=str(row["area"]))

    def records(
        self,
        *,
        area: str | None = None,
        memory_type: str | None = None,
    ) -> list[IndexedRecord]:
        self.ensure_ready()
        clauses: list[str] = []
        parameters: list[Any] = []
        if area is not None:
            clauses.append("area = ?")
            parameters.append(area)
        if memory_type is not None:
            clauses.append("memory_type = ?")
            parameters.append(memory_type)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        connection = self._connect(self.path)
        try:
            rows = connection.execute(
                "SELECT relpath, area, memory_json FROM memories"
                + where
                + " ORDER BY CASE area WHEN 'knowledge' THEN 0 ELSE 1 END, relpath",
                parameters,
            ).fetchall()
            return [self._decode(row, self.vault) for row in rows]
        finally:
            connection.close()

    def find(self, memory_id: str, *, include_history: bool) -> list[IndexedRecord]:
        self.ensure_ready()
        areas = ("knowledge", "history") if include_history else ("knowledge",)
        placeholders = ",".join("?" for _ in areas)
        connection = self._connect(self.path)
        try:
            rows = connection.execute(
                f"""
                SELECT relpath, area, memory_json FROM memories
                WHERE memory_id = ? AND area IN ({placeholders})
                ORDER BY CASE area WHEN 'knowledge' THEN 0 ELSE 1 END, relpath
                """,
                (memory_id, *areas),
            ).fetchall()
            return [self._decode(row, self.vault) for row in rows]
        finally:
            connection.close()

    @staticmethod
    def _chunks(values: Sequence[str], size: int = 400) -> Iterable[Sequence[str]]:
        for index in range(0, len(values), size):
            yield values[index:index + size]

    def _rows_for_relpaths(
        self, connection: sqlite3.Connection, relpaths: set[str]
    ) -> list[IndexedRecord]:
        if not relpaths:
            return []
        rows: list[sqlite3.Row] = []
        ordered = sorted(relpaths)
        for chunk in self._chunks(ordered):
            marks = ",".join("?" for _ in chunk)
            rows.extend(
                connection.execute(
                    f"""
                    SELECT relpath, area, memory_json FROM memories
                    WHERE relpath IN ({marks})
                    ORDER BY CASE area WHEN 'knowledge' THEN 0 ELSE 1 END, relpath
                    """,
                    list(chunk),
                ).fetchall()
            )
        decoded = [self._decode(row, self.vault) for row in rows]
        # Match the previous active-first setdefault(memory_id) behavior.
        by_id: dict[str, IndexedRecord] = {}
        for record in sorted(decoded, key=lambda item: (0 if item.area == "knowledge" else 1, str(item.path))):
            by_id.setdefault(record.memory.memory_id, record)
        return list(by_id.values())

    @staticmethod
    def _normalized_query(query: str | Iterable[str]) -> str:
        if isinstance(query, str):
            value = query
        else:
            value = " ".join(str(item) for item in query)
        return normalize_term(value)

    @staticmethod
    def _text_probes(query: str | Iterable[str]) -> list[str]:
        normalized = DerivedSearchIndex._normalized_query(query)
        probes = [normalized] if normalized else []
        for term in query_terms(query):
            if term and term not in probes:
                probes.append(term)
        # Candidate matching deliberately permits partial CJK topic spans.
        # Three-character windows form a safe superset for the final Python
        # relevance predicate and keep the SQL layer source-neutral.
        for part in re.findall(r"[^\W_]+", normalized, re.UNICODE):
            if not _CJK.search(part) or len(part) < 3:
                continue
            for offset in range(0, len(part) - 2):
                probe = part[offset:offset + 3]
                if probe not in probes:
                    probes.append(probe)
        return probes

    def _candidate_relpaths(
        self,
        connection: sqlite3.Connection,
        query: str | Iterable[str],
        *,
        include_history: bool,
    ) -> set[str]:
        normalized = self._normalized_query(query)
        if not normalized:
            return set()
        areas = ("knowledge", "history") if include_history else ("knowledge",)
        marks = ",".join("?" for _ in areas)
        result: set[str] = set()

        # Exact identifier lookup is always retained.
        for row in connection.execute(
            f"SELECT relpath FROM memories WHERE normalized_id = ? AND area IN ({marks})",
            (normalized, *areas),
        ):
            result.add(str(row[0]))

        # FTS5 phrase search is the primary full-text accelerator.  The
        # bounded instr probes below are a semantic superset for substring/CJK
        # behavior and aliases/keywords, after which existing predicates make
        # the final relevance decision.
        phrase = '"' + normalized.replace('"', '""') + '"'
        try:
            for row in connection.execute(
                f"""
                SELECT f.relpath
                FROM memory_fts AS f
                JOIN memories AS m ON m.relpath = f.relpath
                WHERE memory_fts MATCH ? AND m.area IN ({marks})
                """,
                (phrase, *areas),
            ):
                result.add(str(row[0]))
        except sqlite3.DatabaseError:
            pass

        probes = [probe for probe in self._text_probes(query) if probe]
        if probes:
            conditions = " OR ".join("instr(search_blob, ?) > 0" for _ in probes)
            for row in connection.execute(
                f"SELECT relpath FROM memories WHERE area IN ({marks}) AND ({conditions})",
                (*areas, *probes),
            ):
                result.add(str(row[0]))

        # Indexed terms match in the reverse direction too: a memory tag or
        # wikilink may be embedded inside a longer user query.
        for row in connection.execute(
            f"""
            SELECT DISTINCT t.relpath
            FROM terms AS t
            JOIN memories AS m ON m.relpath = t.relpath
            WHERE m.area IN ({marks}) AND instr(?, t.term) > 0
            """,
            (*areas, normalized),
        ):
            result.add(str(row[0]))
        return result

    def _same_title_siblings(
        self,
        connection: sqlite3.Connection,
        records: Sequence[IndexedRecord],
        *,
        include_history: bool,
    ) -> list[IndexedRecord]:
        titles = sorted({normalize_term(item.memory.title) for item in records if item.memory.title})
        if not titles:
            return []
        areas = ("knowledge", "history") if include_history else ("knowledge",)
        result: list[IndexedRecord] = []
        for chunk in self._chunks(titles):
            title_marks = ",".join("?" for _ in chunk)
            area_marks = ",".join("?" for _ in areas)
            rows = connection.execute(
                f"""
                SELECT relpath, area, memory_json FROM memories
                WHERE normalized_title IN ({title_marks}) AND area IN ({area_marks})
                """,
                (*chunk, *areas),
            ).fetchall()
            result.extend(self._decode(row, self.vault) for row in rows)
        return result

    def _term_scores(
        self,
        connection: sqlite3.Connection,
        records: Sequence[IndexedRecord],
        query: str | Iterable[str],
    ) -> dict[str, int]:
        relpath_to_id = {
            item.path.resolve().relative_to(self.vault.root).as_posix(): item.memory.memory_id
            for item in records
        }
        index: dict[str, dict[str, list[str]]] = {kind: {} for kind in _KINDS}
        relpaths = sorted(relpath_to_id)
        for chunk in self._chunks(relpaths):
            marks = ",".join("?" for _ in chunk)
            for row in connection.execute(
                f"SELECT relpath, kind, term FROM terms WHERE relpath IN ({marks})",
                list(chunk),
            ):
                kind = str(row["kind"])
                if kind not in index:
                    continue
                term = str(row["term"])
                memory_id = relpath_to_id.get(str(row["relpath"]))
                if memory_id is None:
                    continue
                index[kind].setdefault(term, []).append(memory_id)
        return matching_index_terms(index, query)

    def search(
        self,
        query: str | Iterable[str],
        *,
        scope: str | Iterable[str] | None,
        include_history: bool,
        todo_status: str,
        limit: int | None,
        config: Mapping[str, Any],
        stable: bool,
        strict_candidates: bool,
    ) -> list[IndexedRecord]:
        self.ensure_ready()
        if todo_status not in ("active", "completed", "cancelled", "all"):
            raise ValueError("invalid todo status")
        connection = self._connect(self.path)
        try:
            candidate_relpaths = self._candidate_relpaths(
                connection, query, include_history=include_history
            )
            candidates = self._rows_for_relpaths(connection, candidate_relpaths)
            if not candidates:
                return []

            original_paths = {
                item.path.resolve().relative_to(self.vault.root).as_posix()
                for item in candidates
            }
            if scope is None:
                visible = list(candidates)
                ranks = {item.memory.memory_id: 0 for item in visible}
            else:
                siblings = self._same_title_siblings(
                    connection, candidates, include_history=include_history
                )
                combined: list[IndexedRecord] = []
                seen_paths: set[str] = set()
                for item in [*candidates, *siblings]:
                    key = item.path.resolve().relative_to(self.vault.root).as_posix()
                    if key in seen_paths:
                        continue
                    seen_paths.add(key)
                    combined.append(item)
                by_object = {id(item.memory): item for item in combined}
                scoped = filter_by_scope([item.memory for item in combined], scope, config)
                allowed = inherited_scopes(scope, config)
                visible = []
                ranks: dict[str, int] = {}
                for memory, rank in scoped:
                    item = by_object[id(memory)]
                    key = item.path.resolve().relative_to(self.vault.root).as_posix()
                    if key not in original_paths:
                        continue
                    visible.append(item)
                    ranks[item.memory.memory_id] = memory_scope_rank(memory, allowed)

            if not visible:
                return []
            term_scores = self._term_scores(connection, visible, query)
            has_indexed = any(item.memory.memory_id in term_scores for item in visible)
            records: list[IndexedRecord] = []
            if has_indexed:
                for item in visible:
                    score = term_scores.get(item.memory.memory_id)
                    if score is None:
                        continue
                    item.score = score + fulltext_score(item.memory, query)
                    records.append(item)
            else:
                for item in visible:
                    score = fulltext_score(item.memory, query)
                    if score:
                        item.score = score
                        records.append(item)

            if strict_candidates:
                indexed_ids = {item.memory.memory_id for item in records}
                for item in visible:
                    if item.memory.memory_id in indexed_ids:
                        continue
                    if not candidate_matches_query(item.memory, query):
                        continue
                    item.score = fulltext_score(item.memory, query)
                    records.append(item)

            normalized = self._normalized_query(query)
            if normalized:
                by_id = {item.memory.memory_id: item for item in visible}
                for item in visible:
                    if normalize_term(item.memory.memory_id) != normalized:
                        continue
                    if item.memory.memory_id not in {row.memory.memory_id for row in records}:
                        records.append(item)
                    item.score = max(item.score, 0) + 100000

            filtered: list[IndexedRecord] = []
            for item in records:
                if item.memory.type == "todo":
                    status = item.memory.status or "active"
                    if todo_status != "all" and status != todo_status:
                        continue
                item.scope_rank = ranks.get(item.memory.memory_id, 0)
                filtered.append(item)

            if stable:
                filtered.sort(
                    key=lambda item: (
                        item.score,
                        item.scope_rank,
                        item.memory.updated,
                        item.memory.memory_id,
                    ),
                    reverse=True,
                )
            else:
                filtered.sort(
                    key=lambda item: (
                        item.score,
                        item.scope_rank,
                        item.memory.hit_count,
                        item.memory.updated,
                        item.memory.memory_id,
                    ),
                    reverse=True,
                )
            if limit is not None:
                if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
                    raise ValueError("limit must be a non-negative integer")
                filtered = filtered[:limit]
            return filtered
        finally:
            connection.close()

    def tags_index(self) -> dict[str, Any]:
        self.ensure_ready()
        active = {kind: {} for kind in _KINDS}
        history = {kind: {} for kind in _KINDS}
        connection = self._connect(self.path)
        try:
            rows = connection.execute(
                """
                SELECT m.area, m.memory_id, t.kind, t.term
                FROM terms AS t JOIN memories AS m ON m.relpath = t.relpath
                ORDER BY m.area, t.kind, t.term, m.memory_id
                """
            )
            for row in rows:
                area = active if row["area"] == "knowledge" else history
                kind = str(row["kind"])
                if kind not in area:
                    continue
                area[kind].setdefault(str(row["term"]), []).append(str(row["memory_id"]))
            for group in (active, history):
                for kind in _KINDS:
                    for term, ids in list(group[kind].items()):
                        group[kind][term] = sorted(set(ids))
            return {
                "version": 1,
                "tags": active["tags"],
                "aliases": active["aliases"],
                "keywords": active["keywords"],
                "wikilinks": active["wikilinks"],
                "history": history,
            }
        finally:
            connection.close()
'''
write("src/memleaf/sqlite_index.py", sqlite_module)

# Vault owns the disposable index location but never creates correctness state there.
path = "src/memleaf/vault.py"
text = read(path)
needle = "    def native_sources_index_path(self) -> Path:\n        return self._inside(\"_index\", \"native_sources.json\")\n"
replacement = needle + "\n    @property\n    def search_index_path(self) -> Path:\n        \"\"\"Disposable SQLite/FTS search index derived from Markdown.\"\"\"\n        return self._inside(\"_index\", \"search.sqlite3\")\n"
if needle not in text:
    raise SystemExit("vault index insertion point missing")
text = text.replace(needle, replacement, 1)
write(path, text)

# Service: use direct Markdown for exact active reads, SQLite for candidate/todo
# directories, and full recreation only for the explicit rebuild command.
path = "src/memleaf/service.py"
text = read(path)

old = '''    def _find_records_unlocked(self, memory_id: str, include_history: bool = True) -> list[_Record]:
        matches = []
        for record in self._read_memories_unlocked("knowledge"):
            if record.memory.memory_id == memory_id:
                matches.append(record)
        if include_history:
            for record in self._read_memories_unlocked("history"):
                if record.memory.memory_id == memory_id:
                    matches.append(record)
        return matches
'''
new = '''    def _find_records_unlocked(self, memory_id: str, include_history: bool = True) -> list[_Record]:
        # Active ids map directly to their authoritative Markdown filename, so
        # exact reads never need the acceleration database to return content.
        active_path = self.vault.memory_path(memory_id, "knowledge")
        matches: list[_Record] = []
        if active_path.exists() and active_path.is_file() and not active_path.is_symlink():
            try:
                memory = Memory.from_markdown(active_path.read_text(encoding="utf-8"), active_path)
            except (OSError, UnicodeError, ValueError):
                memory = None
            if memory is not None and memory.memory_id == memory_id:
                matches.append(_Record(memory=memory, path=active_path, area="knowledge"))
        if not include_history:
            return matches
        from .sqlite_index import DerivedSearchIndex
        for item in DerivedSearchIndex(self.vault).find(memory_id, include_history=True):
            if item.area == "knowledge" and matches:
                continue
            matches.append(_Record(memory=item.memory, path=item.path, area=item.area))
        return matches

    def _indexed_records_unlocked(
        self, *, area: str | None = None, memory_type: str | None = None
    ) -> list[_Record]:
        from .sqlite_index import DerivedSearchIndex
        return [
            _Record(memory=item.memory, path=item.path, area=item.area)
            for item in DerivedSearchIndex(self.vault).records(area=area, memory_type=memory_type)
        ]
'''
if old not in text:
    raise SystemExit("find records block missing")
text = text.replace(old, new, 1)

start = text.index("    def _rebuild_index_unlocked(self) -> dict[str, int]:")
end = text.index("\n    def rebuild_index(self) -> dict[str, Any]:", start)
new_rebuild = '''    def _rebuild_index_unlocked(self, *, full: bool = False) -> dict[str, int]:
        """Refresh disposable indexes derived completely from Markdown."""
        from .sqlite_index import DerivedSearchIndex

        derived = DerivedSearchIndex(self.vault)
        result = derived.sync(full=full)
        atomic_write_json(self.vault.tags_index_path, derived.tags_index())
        event_keys: set[str] = set()
        for path in self.vault.list_markdown("inbox"):
            try:
                event_keys.update(extract_event_keys(path.read_text(encoding="utf-8")))
            except (OSError, UnicodeError):
                continue
        result["events"] = len(event_keys)
        return result
'''
text = text[:start] + new_rebuild + text[end:]
text = text.replace(
    "            result = self._rebuild_index_unlocked()\n            from .native_index import NativeIndexer\n",
    "            result = self._rebuild_index_unlocked(full=True)\n            from .native_index import NativeIndexer\n",
    1,
)

# Public todo enumeration uses indexed metadata rather than reparsing every file.
text = text.replace(
    '            active_records = [record for record in self._read_memories_unlocked("knowledge") if record.memory.type == "todo"]\n',
    '            active_records = self._indexed_records_unlocked(area="knowledge", memory_type="todo")\n',
    1,
)
text = text.replace(
    '                    for record in self._read_memories_unlocked("history")\n                    if record.memory.type == "todo"\n',
    '                    for record in self._indexed_records_unlocked(area="history", memory_type="todo")\n                    if record.memory.type == "todo"\n',
    1,
)

# Scope catalog needs metadata only and can use the derived directory.
text = text.replace(
    '        for record in self._read_memories_unlocked("knowledge"):\n            for raw_scope in record.memory.scopes:\n',
    '        for record in self._indexed_records_unlocked(area="knowledge"):\n            for raw_scope in record.memory.scopes:\n',
    1,
)

# Replace the expensive all-Markdown search implementation with the derived
# candidate layer while retaining the existing deterministic scoring rules in
# sqlite_index.py.
search_start = text.index("    def _search_unlocked(\n")
search_end = text.index("\n    def search(\n", search_start)
new_search = '''    def _search_unlocked(
        self,
        query: str | Iterable[str],
        *,
        scope: str | Iterable[str] | None,
        include_history: bool,
        todo_status: str,
        limit: Optional[int],
        context_only_global: bool = False,
        stable: bool = False,
        strict_candidates: bool = False,
    ) -> list[_Record]:
        if isinstance(query, str):
            query_value: str | list[str] = query
        else:
            query_value = list(query)
        scope_value = self._scope_query_values(scope)
        config = self.vault.config()
        if context_only_global and scope_value is None:
            scope_value = "global"
        if not query_value or (isinstance(query_value, str) and not query_value.strip()):
            return []
        from .sqlite_index import DerivedSearchIndex

        indexed = DerivedSearchIndex(self.vault).search(
            query_value,
            scope=scope_value,
            include_history=include_history,
            todo_status=todo_status,
            limit=limit,
            config=config,
            stable=stable,
            strict_candidates=strict_candidates,
        )
        return [
            _Record(
                memory=item.memory,
                path=item.path,
                area=item.area,
                score=item.score,
                scope_rank=item.scope_rank,
            )
            for item in indexed
        ]
'''
text = text[:search_start] + new_search + text[search_end:]

# Keep read-hit accounting reflected in the disposable sort cache without a
# whole-Vault scan.
needle = '                atomic_write_text(record.path, record.memory.to_markdown())\n'
replacement = needle + '                from .sqlite_index import DerivedSearchIndex\n                DerivedSearchIndex(self.vault).refresh_paths([record.path])\n'
if needle not in text:
    raise SystemExit("read hit write point missing")
text = text.replace(needle, replacement, 1)
write(path, text)

# Permanent acceptance tests for rebuildability, source authority, scope, FTS,
# state isolation and indexed todo/read paths.
test = r'''from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from memleaf.models import Memory
from memleaf.service import Memleaf


class SQLiteDerivedIndexV028Tests(unittest.TestCase):
    def _service(self):
        temporary = tempfile.TemporaryDirectory(prefix="memleaf-sqlite-v028-")
        root = Path(temporary.name) / "vault"
        service = Memleaf(root)
        return temporary, service

    def test_sqlite_is_disposable_index_and_rebuilds_from_markdown(self) -> None:
        temporary, service = self._service()
        with temporary:
            service.create_memory(
                memory_id="alpha",
                title="Alpha memory",
                body="needle-alpha durable body",
                tags=["topic-alpha"],
                scopes=["global"],
            )
            self.assertTrue(service.vault.search_index_path.is_file())
            self.assertEqual(service.vault.search_index_path.parent, service.vault.index_path)
            state_before = {
                path.name: path.read_bytes()
                for path in service.vault.state_path.iterdir()
                if path.is_file() and path.name != "vault.lock"
            }
            service.vault.search_index_path.unlink()
            result = service.search_candidates("needle-alpha")
            self.assertEqual(result["status"], "found")
            self.assertEqual(result["results"][0]["memory_id"], "alpha")
            self.assertTrue(service.vault.search_index_path.is_file())
            state_after = {
                path.name: path.read_bytes()
                for path in service.vault.state_path.iterdir()
                if path.is_file() and path.name != "vault.lock"
            }
            self.assertEqual(state_before, state_after)

    def test_explicit_rebuild_recreates_fts5_from_markdown(self) -> None:
        temporary, service = self._service()
        with temporary:
            memory = Memory.new(
                memory_id="manual",
                title="Manual",
                body="first body",
                tags=["manual-tag"],
                scopes=["global"],
            )
            path = service.vault.memory_path("manual", "knowledge")
            path.write_text(memory.to_markdown(), encoding="utf-8")
            service.rebuild_index()
            with sqlite3.connect(service.vault.search_index_path) as connection:
                fts_count = connection.execute("SELECT count(*) FROM memory_fts").fetchone()[0]
            self.assertEqual(fts_count, 1)
            memory.body = "externally changed needle-second"
            path.write_text(memory.to_markdown(), encoding="utf-8")
            # Explicit rebuild is the contract for direct external file edits.
            service.rebuild_index()
            result = service.search_candidates("needle-second")
            self.assertEqual([item["memory_id"] for item in result["results"]], ["manual"])

    def test_exact_read_uses_authoritative_markdown_not_cached_body(self) -> None:
        temporary, service = self._service()
        with temporary:
            service.create_memory(
                memory_id="read-source",
                title="Read source",
                body="old body",
                scopes=["global"],
            )
            path = service.vault.memory_path("read-source", "knowledge")
            memory = Memory.from_markdown(path.read_text(encoding="utf-8"), path)
            memory.body = "new authoritative body"
            path.write_text(memory.to_markdown(), encoding="utf-8")
            loaded = service.read("read-source")
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.body, "new authoritative body")

    def test_scope_and_todo_results_preserve_existing_contract(self) -> None:
        temporary, service = self._service()
        with temporary:
            config = service.vault.config()
            config["scopes"] = {
                "project:a": {"aliases": ["A"]},
                "project:b": {"aliases": ["B"]},
            }
            from memleaf.config import save_config
            save_config(service.vault.config_path, config)
            service.create_memory(
                memory_id="a-task",
                title="A task",
                body="shared-term project A",
                tags=["shared-term"],
                type="todo",
                scopes=["project:a"],
                status="active",
                due_date="2027-01-02",
            )
            service.create_memory(
                memory_id="b-task",
                title="B task",
                body="shared-term project B",
                tags=["shared-term"],
                type="todo",
                scopes=["project:b"],
                status="active",
                due_date="2027-01-03",
            )
            result = service.search_candidates("shared-term", scope="project:a")
            self.assertEqual([item["memory_id"] for item in result["results"]], ["a-task"])
            todos = service.list_todos(scope="project:a")
            self.assertEqual([item["memory_id"] for item in todos["results"]], ["a-task"])

    def test_corrupt_sqlite_is_rebuilt_because_it_is_not_state(self) -> None:
        temporary, service = self._service()
        with temporary:
            service.create_memory(
                memory_id="repair",
                title="Repair",
                body="repair needle",
                scopes=["global"],
            )
            service.vault.search_index_path.write_bytes(b"not a sqlite database")
            result = service.search_candidates("repair needle")
            self.assertEqual(result["status"], "found")
            self.assertEqual(result["results"][0]["memory_id"], "repair")


if __name__ == "__main__":
    unittest.main()
'''
write("tests/test_sqlite_index_v028.py", test)

# Document the derived-only database boundary and manual-edit rebuild contract.
doc = "docs/config-migrations.md"
text = read(doc)
section = '''\n## v0.2.28 disposable SQLite search index\n\n`_index/search.sqlite3` is a derived SQLite/FTS5 acceleration file. Markdown in `knowledge/` and `history/` remains authoritative; the database never stores runtime correctness/recovery state and may be deleted at any time. `memleaf rebuild-index` fully recreates it from Markdown. Normal memleaf writes refresh the derived index automatically. If an operator edits Markdown directly outside memleaf, run `memleaf rebuild-index` before relying on indexed search results; exact active-memory reads continue to read the Markdown file itself.\n'''
if "## v0.2.28 disposable SQLite search index" not in text:
    text = text.rstrip() + "\n" + section
write(doc, text)

print("v0.2.28 SQLite derived-index implementation applied")

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
index_path = ROOT / "src/memleaf/sqlite_index.py"
text = index_path.read_text(encoding="utf-8")

# Connection setup can fail after sqlite3.connect() succeeds (for example a
# concurrent rebuild changes the file while PRAGMAs are applied). Close the
# partially initialized handle before propagating the failure.
old = '''    @staticmethod
    def _connect(path: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA temp_store=MEMORY")
        return connection
'''
new = '''    @staticmethod
    def _connect(path: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(path, timeout=30.0)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA temp_store=MEMORY")
            return connection
        except Exception:
            connection.close()
            raise
'''
if text.count(old) != 1:
    raise SystemExit("runtime connection block changed")
text = text.replace(old, new, 1)

old = '''    @staticmethod
    def _connect_build(path: Path) -> sqlite3.Connection:
        """Open a brand-new disposable build database for bulk loading.

        The file is not published until the connection closes successfully
        and ``os.replace`` swaps it into `_index/`.  A crash can therefore
        discard this temporary database without affecting Markdown or state.
        """
        connection = sqlite3.connect(path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute("PRAGMA temp_store=MEMORY")
        connection.execute("PRAGMA locking_mode=EXCLUSIVE")
        return connection
'''
new = '''    @staticmethod
    def _connect_build(path: Path) -> sqlite3.Connection:
        """Open a brand-new disposable build database for bulk loading.

        The file is not published until the connection closes successfully
        and ``os.replace`` swaps it into `_index/`.  A crash can therefore
        discard this temporary database without affecting Markdown or state.
        """
        connection = sqlite3.connect(path, timeout=30.0)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=OFF")
            connection.execute("PRAGMA synchronous=OFF")
            connection.execute("PRAGMA temp_store=MEMORY")
            connection.execute("PRAGMA locking_mode=EXCLUSIVE")
            return connection
        except Exception:
            connection.close()
            raise
'''
if text.count(old) != 1:
    raise SystemExit("build connection block changed")
text = text.replace(old, new, 1)

# The optimized `_candidate_relpaths` is deliberately strict because the
# public candidate directory applies `candidate_matches_query` and has the 2s
# 50k latency contract. Internal planning/dedupe historically uses a broader
# indexed-first search. Preserve that separate semantic contract rather than
# forcing the strict directory prefilter onto processing.
marker = '''    def _same_title_siblings(
'''
legacy = '''    def _legacy_candidate_relpaths(
        self,
        connection: sqlite3.Connection,
        query: str | Iterable[str],
        *,
        include_history: bool,
    ) -> set[str]:
        """Return the broad historical candidate superset for internal search."""
        normalized = self._normalized_query(query)
        if not normalized:
            return set()
        areas = ("knowledge", "history") if include_history else ("knowledge",)
        marks = ",".join("?" for _ in areas)
        result: set[str] = set()

        for row in connection.execute(
            f"SELECT relpath FROM memories WHERE normalized_id = ? AND area IN ({marks})",
            (normalized, *areas),
        ):
            result.add(str(row[0]))

        phrase = '"' + normalized.replace('"', '""') + '"'
        try:
            rows = connection.execute(
                f"""
                SELECT f.relpath
                FROM memory_fts AS f
                JOIN memories AS m ON m.relpath = f.relpath
                WHERE memory_fts MATCH ? AND m.area IN ({marks})
                """,
                (phrase, *areas),
            )
            for row in rows:
                result.add(str(row[0]))
        except sqlite3.DatabaseError:
            pass

        probes = [probe for probe in self._text_probes(query) if probe]
        for probe_chunk in self._chunks(probes, size=100):
            conditions = " OR ".join("instr(search_blob, ?) > 0" for _ in probe_chunk)
            for row in connection.execute(
                f"SELECT relpath FROM memories WHERE area IN ({marks}) AND ({conditions})",
                (*areas, *probe_chunk),
            ):
                result.add(str(row[0]))

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

'''
if marker not in text:
    raise SystemExit("legacy candidate insertion point changed")
if "def _legacy_candidate_relpaths(" not in text:
    text = text.replace(marker, legacy + marker, 1)

old = '''            candidate_relpaths = self._candidate_relpaths(
                connection, query, include_history=include_history
            )
'''
new = '''            candidate_relpaths = (
                self._candidate_relpaths(
                    connection, query, include_history=include_history
                )
                if strict_candidates
                else self._legacy_candidate_relpaths(
                    connection, query, include_history=include_history
                )
            )
'''
if text.count(old) != 1:
    raise SystemExit("search candidate dispatch block changed")
text = text.replace(old, new, 1)
index_path.write_text(text, encoding="utf-8")

# Pin the regression at the public service boundary used by processing: an
# incoming change can still recover the older active topic as related context.
test_path = ROOT / "tests/test_sqlite_index_v028.py"
test = test_path.read_text(encoding="utf-8")
insert_before = '''    def test_long_cjk_query_is_chunked_without_sql_parameter_overflow(self) -> None:
'''
addition = '''    def test_internal_legacy_search_keeps_related_update_target_visible(self) -> None:
        temporary, service = self._service()
        with temporary:
            service.create_memory(
                memory_id="config",
                title="Orion 服务配置",
                body="Orion 服务配置：连接超时为 10 秒，重试次数为 1 次。",
                tags=["configuration"],
                type="fact",
                scopes=["project:Orion"],
            )
            query = "Orion 服务配置的连接超时已设为 30 秒。"
            matches = service.search(query, scope="project:Orion")
            self.assertEqual([memory.memory_id for memory in matches], ["config"])

'''
if insert_before not in test:
    raise SystemExit("internal-search test insertion point changed")
if "test_internal_legacy_search_keeps_related_update_target_visible" not in test:
    test = test.replace(insert_before, addition + insert_before, 1)
test_path.write_text(test, encoding="utf-8")

print("separated strict candidate and internal legacy SQLite retrieval")

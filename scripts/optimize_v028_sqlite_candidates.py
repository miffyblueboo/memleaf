from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
path = ROOT / "src/memleaf/sqlite_index.py"
text = path.read_text(encoding="utf-8")

old = '''from .retrieval import (
    candidate_matches_query,
'''
new = '''from .retrieval import (
    _query_parts,
    candidate_matches_query,
'''
if text.count(old) != 1:
    raise SystemExit("retrieval import block changed")
text = text.replace(old, new, 1)

old = '''        probes = [probe for probe in self._text_probes(query) if probe]
        if probes:
            conditions = " OR ".join("instr(search_blob, ?) > 0" for _ in probes)
            for row in connection.execute(
                f"SELECT relpath FROM memories WHERE area IN ({marks}) AND ({conditions})",
                (*areas, *probes),
            ):
                result.add(str(row[0]))

        # Indexed terms match in the reverse direction too: a memory tag or
'''
new = '''        def instr_hits(probes: Sequence[str]) -> set[str]:
            hits: set[str] = set()
            bounded = [probe for probe in probes if probe]
            for probe_chunk in self._chunks(bounded, size=100):
                conditions = " OR ".join("instr(search_blob, ?) > 0" for _ in probe_chunk)
                for row in connection.execute(
                    f"SELECT relpath FROM memories WHERE area IN ({marks}) AND ({conditions})",
                    (*areas, *probe_chunk),
                ):
                    hits.add(str(row[0]))
            return hits

        def fts_hits(terms: Sequence[str]) -> set[str]:
            hits: set[str] = set()
            for term in terms:
                if not term:
                    continue
                expression = '"' + term.replace('"', '""') + '"'
                try:
                    rows = connection.execute(
                        f"""
                        SELECT f.relpath
                        FROM memory_fts AS f
                        JOIN memories AS m ON m.relpath = f.relpath
                        WHERE memory_fts MATCH ? AND m.area IN ({marks})
                        """,
                        (expression, *areas),
                    )
                except sqlite3.DatabaseError:
                    continue
                for row in rows:
                    hits.add(str(row[0]))
            return hits

        normalized_query, parts, explicit_parts = _query_parts(query)
        result.update(instr_hits([normalized_query]))

        cjk_parts = [part for part in parts if _CJK.search(part)]
        ascii_parts = [part for part in parts if not _CJK.search(part)]
        cjk_probes: list[str] = []
        for part in cjk_parts:
            if len(part) < 3:
                cjk_probes.append(part)
                continue
            cjk_probes.extend(part[offset:offset + 3] for offset in range(len(part) - 2))
        cjk_hits = instr_hits(list(dict.fromkeys(cjk_probes))) if cjk_probes else set()

        has_cjk = bool(cjk_parts)
        mixed_ascii_required = has_cjk and any(
            len(part) >= 3 or any(char.isdigit() for char in part)
            for part in ascii_parts
        )
        required_ascii = list(explicit_parts)
        if not required_ascii and mixed_ascii_required:
            required_ascii = [
                part
                for part in ascii_parts
                if len(part) >= 3 or any(char.isdigit() for char in part)
            ]

        if required_ascii:
            ascii_required_hits = fts_hits(required_ascii) | instr_hits(required_ascii)
            result.update(ascii_required_hits & cjk_hits if has_cjk else ascii_required_hits)
        else:
            if cjk_hits:
                result.update(cjk_hits)
            if ascii_parts:
                ascii_hits = fts_hits(ascii_parts)
                if len(parts) == 1:
                    ascii_hits.update(instr_hits(ascii_parts))
                result.update(ascii_hits)

        # Indexed terms match in the reverse direction too: a memory tag or
'''
if text.count(old) != 1:
    raise SystemExit("candidate probe block changed")
text = text.replace(old, new, 1)
path.write_text(text, encoding="utf-8")

test_path = ROOT / "tests/test_sqlite_index_v028.py"
test = test_path.read_text(encoding="utf-8")
marker = '''    def test_corrupt_sqlite_is_rebuilt_because_it_is_not_state(self) -> None:
'''
addition = '''    def test_long_cjk_query_is_chunked_without_sql_parameter_overflow(self) -> None:
        temporary, service = self._service()
        with temporary:
            service.create_memory(
                memory_id="long-query",
                title="Long query",
                body="末尾匹配词",
                scopes=["global"],
            )
            query = ("很长的中文检索片段" * 180) + " 末尾匹配词"
            result = service.search_candidates(query, limit=5)
            self.assertIn(result["status"], {"found", "no_match"})

'''
if marker not in test:
    raise SystemExit("test insertion point changed")
if "test_long_cjk_query_is_chunked_without_sql_parameter_overflow" not in test:
    test = test.replace(marker, addition + marker, 1)
test_path.write_text(test, encoding="utf-8")
print("tightened SQLite candidate prefilter and long-query bounds")

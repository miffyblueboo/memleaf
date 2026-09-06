from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
path = ROOT / "src/memleaf/sqlite_index.py"
text = path.read_text(encoding="utf-8")

old = '''    @staticmethod
    def _initialize(connection: sqlite3.Connection) -> None:
'''
new = '''    @staticmethod
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

    @staticmethod
    def _initialize(connection: sqlite3.Connection) -> None:
'''
if text.count(old) != 1:
    raise SystemExit("connect-build insertion point changed")
text = text.replace(old, new, 1)

old = '''    def _upsert_path(self, connection: sqlite3.Connection, path: Path) -> None:
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
'''
new = '''    def _insert_fresh_path(self, connection: sqlite3.Connection, path: Path) -> None:
        """Insert one source into a brand-new database without DELETE probes."""
        relpath, area = self._managed_path(path)
        try:
            stat = path.stat()
        except OSError as error:
            raise DerivedIndexError("cannot stat Markdown source") from error
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
            INSERT INTO files(relpath, area, mtime_ns, size, valid)
            VALUES(?, ?, ?, ?, ?)
            """,
            (relpath, area, int(stat.st_mtime_ns), int(stat.st_size), valid),
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
'''
if text.count(old) != 1:
    raise SystemExit("fresh insert replacement point changed")
text = text.replace(old, new, 1)

old = '''        connection = self._connect(temporary)
        try:
            self._initialize(connection)
            with connection:
                for path in self._all_source_paths():
                    self._upsert_path(connection, path)
            connection.execute("PRAGMA optimize")
'''
new = '''        connection = self._connect_build(temporary)
        try:
            self._initialize(connection)
            with connection:
                for path in self._all_source_paths():
                    self._insert_fresh_path(connection, path)
            connection.execute("PRAGMA optimize")
'''
if text.count(old) != 1:
    raise SystemExit("bulk rebuild replacement point changed")
text = text.replace(old, new, 1)

path.write_text(text, encoding="utf-8")
print("optimized disposable SQLite bulk rebuild")

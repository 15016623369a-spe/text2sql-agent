"""Read-only SQLite access for Text-to-SQL and evaluation."""
from __future__ import annotations

import math
import sqlite3
import time
from collections.abc import Iterable
from contextlib import AbstractContextManager, closing, contextmanager
from decimal import Decimal
from itertools import chain
from pathlib import Path
from typing import Protocol

_QUERY_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_RECURSIVE}
_BLOCKED_FUNCTIONS = {"edit", "eval", "fts3_tokenizer", "load_extension", "readfile", "writefile"}


def _authorize_select(action, _arg1, arg2, _database, _source):
    if action == sqlite3.SQLITE_FUNCTION:
        return sqlite3.SQLITE_DENY if (arg2 or "").casefold() in _BLOCKED_FUNCTIONS else sqlite3.SQLITE_OK
    return sqlite3.SQLITE_OK if action in _QUERY_ACTIONS else sqlite3.SQLITE_DENY


def quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


class ReadOnlyViolation(sqlite3.DatabaseError):
    """The statement does not compile as an authorized read query."""


class ReadOnlyConnection(Protocol):
    def dry_run(self, sql: str) -> None: ...

    def scan(self, sql: str, *, max_columns: int | None = None) -> AbstractContextManager[
        tuple[list[str], Iterable[tuple]]
    ]: ...


def result_row_bytes(row: tuple) -> int:
    size = 0
    values = [iter(row)]
    while values:
        for value in values[-1]:
            if isinstance(value, str):
                size += len(value) * 4
            elif isinstance(value, bytes):
                size += len(value)
            elif isinstance(value, Decimal):
                size += len(str(value)) * 4
            else:
                size += 16
                if isinstance(value, (list, tuple)):
                    values.append(iter(value))
                    break
                if isinstance(value, dict):
                    values.append(chain(value.keys(), value.values()))
                    break
        else:
            values.pop()
    return size


class ReadOnlyDB:
    def __init__(self, path: str | Path, timeout: float = 5.0):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Query timeout must be finite and positive")
        self._path = Path(path)
        self._timeout = timeout
        self._conn: sqlite3.Connection | None = None

    def __enter__(self) -> "ReadOnlyDB":
        uri = self._path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=self._timeout)
        try:
            conn.execute("PRAGMA query_only = ON")
            conn.execute("PRAGMA trusted_schema = OFF")
            conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 8 * 1024 * 1024)
            conn.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 256 * 1024)
            conn.set_authorizer(_authorize_select)
        except BaseException:
            conn.close()
            raise
        self._conn = conn
        return self

    def __exit__(self, *_exc) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def dry_run(self, sql: str) -> None:
        """Compile one read query without stepping the original statement."""
        conn = self._conn
        if conn is None:
            raise RuntimeError("Open the database before validating SQL")
        saw_select = False

        def authorize(action, arg1, arg2, database, source):
            nonlocal saw_select
            if action == sqlite3.SQLITE_SELECT:
                saw_select = True
            return _authorize_select(action, arg1, arg2, database, source)

        conn.set_authorizer(authorize)
        try:
            self.query("EXPLAIN " + sql, limit=0)
        finally:
            conn.set_authorizer(_authorize_select)
        # SQLite can compile commands such as VACUUM without authorizer callbacks.
        if not saw_select:
            raise ReadOnlyViolation("Expected a read-only query")

    def references(self, sql: str, *, through_cte: str | None = None) -> set[tuple[str, str]]:
        """Collect table reads and optionally require every read to use the given CTE."""
        conn = self._conn
        if conn is None:
            raise RuntimeError("Open the database before inspecting references")
        reads: set[tuple[str, str]] = set()

        def collect(action, arg1, arg2, database, source):
            if action == sqlite3.SQLITE_READ and arg1:
                if through_cte is not None and source != through_cte:
                    return sqlite3.SQLITE_DENY
                reads.add((arg1, arg2 or ""))
            return _authorize_select(action, arg1, arg2, database, source)

        conn.set_authorizer(collect)
        try:
            self.query("EXPLAIN " + sql, limit=0)
        finally:
            conn.set_authorizer(_authorize_select)
        return reads

    @contextmanager
    def scan(self, sql: str, *, max_columns: int | None = None):
        """Stream one read-only result while the SQLite deadline is active."""
        conn = self._conn
        if conn is None:
            raise RuntimeError("Open the database before querying")
        deadline = time.monotonic() + self._timeout

        def expired() -> bool:
            return time.monotonic() >= deadline

        conn.set_progress_handler(expired, 1000)
        try:
            with closing(conn.execute(sql)) as cursor:
                if cursor.description is None:
                    raise sqlite3.ProgrammingError("Expected one query returning columns")
                columns = [item[0] for item in cursor.description]
                if max_columns is not None and len(columns) > max_columns:
                    raise ReadOnlyViolation("Query returned too many columns")
                yield columns, cursor
            if expired():
                raise TimeoutError("SQLite query exceeded its execution deadline")
        except sqlite3.OperationalError as exc:
            if exc.sqlite_errorcode == sqlite3.SQLITE_INTERRUPT and expired():
                raise TimeoutError("SQLite query exceeded its execution deadline") from exc
            raise
        finally:
            conn.set_progress_handler(None, 0)

    def query(
        self, sql: str, limit: int | None = 20, *,
        max_columns: int | None = None, max_result_bytes: int | None = None,
    ) -> tuple[list[str], list[tuple]]:
        """Execute a read query with a deadline and optional preview limits."""
        with self.scan(sql, max_columns=max_columns) as (columns, cursor):
            if max_result_bytes is None:
                rows = cursor.fetchall() if limit is None else cursor.fetchmany(limit)
            else:
                rows = []
                size = 0
                while limit is None or len(rows) < limit:
                    row = cursor.fetchone()
                    if row is None:
                        break
                    size += result_row_bytes(row)
                    if size > max_result_bytes:
                        raise ReadOnlyViolation("Query result exceeds the preview size limit")
                    rows.append(row)
        return columns, rows

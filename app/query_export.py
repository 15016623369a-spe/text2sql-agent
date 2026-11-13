"""Capture bounded CSV snapshots of query results and store them for their owner."""
from __future__ import annotations

import base64
import csv
import io
import logging
import sqlite3
from collections.abc import Callable
from contextlib import closing, contextmanager
from contextvars import ContextVar
from pathlib import Path
from time import time

MAX_CSV_BYTES = 8 * 1024 * 1024
_CACHE_BYTES = 64 * 1024 * 1024
_RETENTION_SECONDS = 24 * 60 * 60
_BOM = b"\xef\xbb\xbf"
_logger = logging.getLogger(__name__)


def spreadsheet_cell(value):
    """Keep untrusted text inert in spreadsheet programs."""
    if isinstance(value, bytes):
        return "base64:" + base64.b64encode(value).decode("ascii")
    if isinstance(value, str):
        stripped = value.lstrip()
        if value.startswith(("\t", "\r", "\n")) or stripped.startswith(("=", "+", "-", "@")):
            return "'" + value
    return value


class CsvBuffer:
    def __init__(self):
        self._bytes = io.BytesIO(_BOM)
        self._bytes.seek(0, io.SEEK_END)
        self._line = io.StringIO(newline="")
        self._writer = csv.writer(self._line)

    def append(self, row) -> bool:
        self._line.seek(0)
        self._line.truncate()
        self._writer.writerow(spreadsheet_cell(value) for value in row)
        encoded = self._line.getvalue().encode("utf-8")
        if self._bytes.tell() + len(encoded) > MAX_CSV_BYTES:
            return False
        self._bytes.write(encoded)
        return True

    def contents(self) -> bytes:
        return self._bytes.getvalue()


def csv_result(columns, rows) -> bytes | None:
    output = CsvBuffer()
    if output.append(columns) and all(output.append(row) for row in rows):
        return output.contents()
    return None


class QueryExportCapture:
    """Transfer the export from SQL execution without placing it in a checkpoint."""

    def __init__(self):
        self.data: bytes | None = None

    @contextmanager
    def active(self):
        token = _capture.set(self)
        try:
            yield
        finally:
            _capture.reset(token)


_capture: ContextVar[QueryExportCapture | None] = ContextVar("query_export", default=None)


def current_capture() -> QueryExportCapture | None:
    return _capture.get()


class QueryExportStore:
    def __init__(self, path: Path):
        self.path = path
        with closing(sqlite3.connect(path, timeout=5)) as conn, conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS query_exports (
                    sequence INTEGER PRIMARY KEY,
                    trace_id TEXT NOT NULL UNIQUE,
                    owner TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    payload BLOB NOT NULL
                )
            """)

    def put(self, trace_id: str, owner: str, payload: bytes) -> None:
        if len(payload) > MAX_CSV_BYTES:
            raise ValueError("CSV export exceeds the size limit")
        now = time()
        with closing(sqlite3.connect(self.path, timeout=5)) as conn, conn:
            conn.execute("DELETE FROM query_exports WHERE created_at < ?", (now - _RETENTION_SECONDS,))
            conn.execute("INSERT INTO query_exports (trace_id, owner, created_at, payload) VALUES (?, ?, ?, ?)",
                         (trace_id, owner, now, payload))
            conn.execute("""
                DELETE FROM query_exports WHERE sequence IN (
                    SELECT sequence FROM (
                        SELECT sequence, SUM(LENGTH(payload)) OVER (ORDER BY sequence DESC) AS retained_bytes
                        FROM query_exports
                    ) WHERE retained_bytes > ?
                )
            """, (_CACHE_BYTES,))

    def get(self, trace_id: str, owner: str) -> bytes | None:
        with closing(sqlite3.connect(self.path, timeout=5)) as conn:
            row = conn.execute("SELECT payload FROM query_exports WHERE trace_id = ? AND owner = ? AND created_at >= ?",
                               (trace_id, owner, time() - _RETENTION_SECONDS)).fetchone()
        return row[0] if row else None


def result_delivery(store: QueryExportStore, owner: str, source: str, run: Callable[[], dict]):
    capture = QueryExportCapture()

    def execute() -> dict:
        with capture.active():
            return run()

    def prepare(reply: dict, trace_id: str) -> None:
        kind = reply.get("kind")
        if kind not in {"query", "answer"}:
            return
        reply["data_source"] = source
        payload = capture.data
        if kind == "answer":
            payload = csv_result(["营收（元）"], [[reply["revenue_cny"]]])
        elif payload is None and reply.get("total_rows") == len(reply["rows"]):
            payload = csv_result(reply["columns"], reply["rows"])
        if payload is None:
            reply["export_unavailable"] = (
                "未在执行上限内统计完整结果，请缩小查询范围后再导出。"
                if reply.get("total_rows") is None else "结果超过 8 MiB，缩小范围后可导出完整 CSV。"
            )
            return
        try:
            store.put(trace_id, owner, payload)
        except sqlite3.Error as exc:
            _logger.error("Query CSV persistence failed (%s)", type(exc).__name__)
            reply["export_unavailable"] = "暂时无法保存完整 CSV，请稍后重试。"
        else:
            reply["export_url"] = f"/api/v1/results/{trace_id}/csv"

    return execute, prepare

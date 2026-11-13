"""Store the latest file analysis and its reusable upload per session."""
import json
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path


class FileAnalysisHistory:
    def __init__(self, path: Path):
        self.path = path
        with closing(sqlite3.connect(path, timeout=5)) as conn, conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS file_analysis (
                    owner TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    trace_id TEXT NOT NULL,
                    snapshot TEXT NOT NULL,
                    upload BLOB NOT NULL,
                    PRIMARY KEY (owner, session_id)
                ) WITHOUT ROWID;
                CREATE TABLE IF NOT EXISTS file_analysis_requests (
                    owner TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    PRIMARY KEY (owner, session_id)
                ) WITHOUT ROWID;
            """)

    @contextmanager
    def track(self, owner: str, session_id: str, request_id: str):
        """Reserve the latest publication and release it after an unsuccessful analysis."""
        key = (owner, session_id, request_id)
        with closing(sqlite3.connect(self.path, timeout=5)) as conn, conn:
            conn.execute("""
                INSERT INTO file_analysis_requests (owner, session_id, request_id) VALUES (?, ?, ?)
                ON CONFLICT(owner, session_id) DO UPDATE SET request_id = excluded.request_id
            """, key)
        try:
            yield
        finally:
            with closing(sqlite3.connect(self.path, timeout=5)) as conn, conn:
                conn.execute("DELETE FROM file_analysis_requests WHERE owner=? AND session_id=? AND request_id=?", key)

    def save(self, owner: str, session_id: str, request_id: str, snapshot: dict, upload: bytes) -> bool:
        payload = json.dumps(snapshot, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        with closing(sqlite3.connect(self.path, timeout=5)) as conn, conn:
            claimed = conn.execute(
                "DELETE FROM file_analysis_requests WHERE owner=? AND session_id=? AND request_id=?",
                (owner, session_id, request_id),
            )
            if not claimed.rowcount:
                return False
            conn.execute("""
                INSERT INTO file_analysis (owner, session_id, trace_id, snapshot, upload) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(owner, session_id) DO UPDATE SET
                    trace_id=excluded.trace_id, snapshot=excluded.snapshot, upload=excluded.upload
            """, (owner, session_id, snapshot["trace"]["trace_id"], payload, upload))
            return True

    def snapshot(self, owner: str, session_id: str) -> dict | None:
        with closing(sqlite3.connect(self.path, timeout=5)) as conn:
            row = conn.execute("SELECT snapshot FROM file_analysis WHERE owner=? AND session_id=?",
                               (owner, session_id)).fetchone()
        return json.loads(row[0]) if row is not None else None

    def upload(self, owner: str, session_id: str, trace_id: str) -> bytes | None:
        with closing(sqlite3.connect(self.path, timeout=5)) as conn:
            row = conn.execute("SELECT upload FROM file_analysis WHERE owner=? AND session_id=? AND trace_id=?",
                               (owner, session_id, trace_id)).fetchone()
        return row[0] if row is not None else None

    def delete(self, owner: str, session_id: str) -> None:
        with closing(sqlite3.connect(self.path, timeout=5)) as conn, conn:
            key = (owner, session_id)
            conn.execute("DELETE FROM file_analysis_requests WHERE owner=? AND session_id=?", key)
            conn.execute("DELETE FROM file_analysis WHERE owner=? AND session_id=?", key)

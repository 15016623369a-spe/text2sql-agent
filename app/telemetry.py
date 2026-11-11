"""Trace timings and model usage without recording request data."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections import Counter
from contextlib import closing, contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from queue import Full, Queue
from threading import Lock, Thread
from time import perf_counter
from uuid import uuid4

TOKEN_FIELDS = (
    "prompt_tokens", "completion_tokens", "total_tokens",
    "prompt_cache_hit_tokens", "prompt_cache_miss_tokens",
)
_current: ContextVar[Trace | None] = ContextVar("request_trace", default=None)
_logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _usage(raw: object) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    tokens = {key: raw[key] for key in TOKEN_FIELDS if type(raw.get(key)) is int and raw[key] >= 0}
    if "total_tokens" not in tokens and {"prompt_tokens", "completion_tokens"} <= tokens.keys():
        tokens["total_tokens"] = tokens["prompt_tokens"] + tokens["completion_tokens"]
    return tokens


class Trace:
    def __init__(self, operation: str, notify):
        self.id = str(uuid4())
        self.operation = operation
        self.started = _now()
        self.clock = perf_counter()
        self.spans: list[dict] = []
        self.notify = notify

    @contextmanager
    def active(self):
        token = _current.set(self)
        try:
            yield
        finally:
            _current.reset(token)

    def finish(self, status: str, error_type: str | None) -> dict:
        calls = [item for item in self.spans if item["name"] == "model_call"]
        usage = Counter(dict.fromkeys(TOKEN_FIELDS, 0))
        for item in calls:
            usage.update(item["usage"])
        return {
            "trace_id": self.id, "operation": self.operation, "status": status,
            "started_at": self.started, "ended_at": _now(),
            "latency_ms": round((perf_counter() - self.clock) * 1000, 2),
            "model_calls": len(calls), "model_calls_with_usage": sum(bool(item["usage"]) for item in calls),
            "usage": dict(usage), "error_type": error_type, "spans": self.spans,
        }


@contextmanager
def span(name: str):
    trace = _current.get()
    if trace is None:
        yield None
        return
    trace.notify({"event": "stage", "stage": name})
    entry = {"id": str(uuid4()), "name": name, "started_at": _now(), "error_type": None, "usage": {}}
    started = perf_counter()
    trace.spans.append(entry)
    try:
        yield entry
    except Exception as exc:
        entry["error_type"] = type(exc).__name__
        raise
    finally:
        entry["usage"] = _usage(entry["usage"])
        entry["ended_at"] = _now()
        entry["latency_ms"] = round((perf_counter() - started) * 1000, 2)


class TraceStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path, timeout=5)) as conn, conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS traces (id TEXT PRIMARY KEY, payload TEXT NOT NULL, owner TEXT NOT NULL DEFAULT '')"
            )
            if "owner" not in {column[1] for column in conn.execute("PRAGMA table_info(traces)")}:
                conn.execute("ALTER TABLE traces ADD COLUMN owner TEXT NOT NULL DEFAULT ''")

    def put(self, report: dict, *, owner: str = "") -> None:
        with closing(sqlite3.connect(self.path, timeout=5)) as conn, conn:
            conn.execute(
                "INSERT INTO traces (id, payload, owner) VALUES (?, ?, ?)",
                (report["trace_id"], json.dumps(report), owner),
            )

    def get(self, trace_id: str, *, owner: str = "") -> dict | None:
        with closing(sqlite3.connect(self.path, timeout=5)) as conn:
            row = conn.execute("SELECT payload FROM traces WHERE id = ? AND owner = ?", (trace_id, owner)).fetchone()
        return json.loads(row[0]) if row else None


def export_langsmith(report: dict) -> None:
    if os.getenv("RAG_LANGSMITH_EXPORT", "").lower() not in {"1", "true"}:
        return
    key = os.getenv("LANGSMITH_API_KEY")
    if not key:
        _logger.warning("LangSmith export enabled without LANGSMITH_API_KEY")
        return

    from langsmith import Client
    from urllib3.util.retry import Retry

    try:
        client = Client(
            api_key=key, api_url=os.getenv("LANGSMITH_ENDPOINT") or "https://api.smith.langchain.com",
            timeout_ms=(1500, 3000), retry_config=Retry(total=0),
            auto_batch_tracing=False, omit_traced_runtime_info=True, tracing_mode="langsmith",
        )
        project = os.getenv("LANGSMITH_PROJECT", "rag-step8")
        trace_id = report["trace_id"]
        client.create_run(
            report["operation"], {}, "chain", id=trace_id, trace_id=trace_id,
            project_name=project, start_time=report["started_at"], end_time=report["ended_at"],
            error=report["error_type"], outputs={
                "latency_ms": report["latency_ms"], "usage": report["usage"],
                "model_calls": report["model_calls"],
            },
        )
        for item in report["spans"]:
            usage = item["usage"]
            model = item["name"] == "model_call"
            outputs = {"latency_ms": item["latency_ms"]}
            if model and {"prompt_tokens", "completion_tokens", "total_tokens"} <= usage.keys():
                outputs["usage_metadata"] = {
                    "input_tokens": usage["prompt_tokens"], "output_tokens": usage["completion_tokens"],
                    "total_tokens": usage["total_tokens"],
                }
            client.create_run(
                item["name"], {}, "llm" if model else "tool", id=item["id"],
                parent_run_id=trace_id, trace_id=trace_id, project_name=project,
                start_time=item["started_at"], end_time=item["ended_at"],
                error=item["error_type"], outputs=outputs,
            )
    except Exception as exc:
        _logger.warning("LangSmith export failed (%s)", type(exc).__name__)


_exports: Queue[dict] = Queue(maxsize=32)
_export_lock = Lock()
_export_thread: Thread | None = None


def _export_loop() -> None:
    while True:
        report = _exports.get()
        try:
            export_langsmith(report)
        except Exception:
            _logger.warning("Optional trace export failed")
        finally:
            _exports.task_done()


def enqueue_export(report: dict) -> None:
    """Never hold a workflow slot while an optional remote sink is slow."""
    global _export_thread
    if os.getenv("RAG_LANGSMITH_EXPORT", "").lower() not in {"1", "true"}:
        return
    with _export_lock:
        if _export_thread is None:
            _export_thread = Thread(target=_export_loop, name="trace-export", daemon=True)
            _export_thread.start()
    try:
        _exports.put_nowait(report)
    except Full:
        _logger.warning("Optional trace export queue is full; local trace retained")

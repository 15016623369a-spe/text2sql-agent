"""Run bounded blocking workflows while reporting real progress over NDJSON."""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from threading import BoundedSemaphore, Lock

from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from langsmith import tracing_context

from app.file_sandbox import SandboxError
from app.telemetry import Trace, TraceStore, enqueue_export

_slots = BoundedSemaphore(4)
_owner_lock = Lock()
_owner_slots: dict[str, int] = {}
_logger = logging.getLogger(__name__)


class OperationError(ValueError):
    """A validated, user-facing workflow error."""


def stream_operation(
    store: TraceStore, operation: str, run: Callable[[], dict], *, owner: str = "",
) -> StreamingResponse:
    with _owner_lock:
        if owner and _owner_slots.get(owner, 0) >= 2:
            raise HTTPException(429, "当前账号的并行任务已达上限")
        if not _slots.acquire(blocking=False):
            raise HTTPException(503, "当前处理任务较多，请稍后再试")
        if owner:
            _owner_slots[owner] = _owner_slots.get(owner, 0) + 1

    def release() -> None:
        with _owner_lock:
            if owner:
                if _owner_slots[owner] == 1:
                    del _owner_slots[owner]
                else:
                    _owner_slots[owner] -= 1
            _slots.release()

    loop = asyncio.get_running_loop()
    events: asyncio.Queue[dict | None] = asyncio.Queue()

    def emit(event: dict | None) -> None:
        if not loop.is_closed():
            loop.call_soon_threadsafe(events.put_nowait, event)

    trace = Trace(operation, emit)

    def work() -> None:
        status, error_type = "ok", None
        try:
            with trace.active(), tracing_context(enabled=False):
                result = run()
            if result.get("kind") == "error":
                status, error_type = "error", "WorkflowError"
            # Catch serialization errors in the worker before sending a result.
            _line(result)
            emit({"event": "result", "data": result})
        except Exception as exc:
            status, error_type = "error", type(exc).__name__
            message = str(exc) if isinstance(exc, (OperationError, SandboxError)) else "处理失败，请凭追踪编号查看服务状态"
            emit({"event": "error", "message": message})
        finally:
            try:
                report = trace.finish(status, error_type)
                try:
                    store.put(report, owner=owner)
                except Exception as exc:
                    _logger.error("Local trace persistence failed (%s)", type(exc).__name__)
                    report["stored"] = False
                emit({"event": "done", **{key: value for key, value in report.items() if key != "spans"}})
                emit(None)
                enqueue_export(report)
            finally:
                release()

    try:
        # A disconnect closes the stream, while the bounded worker finishes and records its trace.
        # In particular an accepted approval is allowed to reach its durable checkpoint.
        loop.run_in_executor(None, work)
    except RuntimeError:
        release()
        raise

    async def body():
        yield _line({"event": "started", "trace_id": trace.id})
        while True:
            try:
                event = await asyncio.wait_for(events.get(), timeout=10)
            except TimeoutError:
                event = {"event": "heartbeat"}
            if event is None:
                return
            yield _line({"trace_id": trace.id, **event})

    return StreamingResponse(
        body(), media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no", "X-Trace-Id": trace.id},
    )


def _line(event: dict) -> bytes:
    return (json.dumps(event, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode()

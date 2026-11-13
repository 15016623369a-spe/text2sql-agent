"""FastAPI entry point for the conversational, SQL, and upload workflows."""
from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
from collections.abc import Callable
from contextlib import asynccontextmanager
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response, StreamingResponse
from langsmith import tracing_context
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StringConstraints
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from app.commerce_demo import ApprovalID
from app.config import PROJECT_ROOT, load_settings
from app.conversation_history import ConversationHistory
from app.file_analysis import analyze_upload, preview_upload
from app.file_analysis_history import FileAnalysisHistory
from app.gateway_identity import configured_key, gateway_user, owner_id
from app.product_assistant import answer_data_turn
from app.product_errors import chat_http_error, is_chat_request
from app.product_sql import DB_ID_PATTERN, database_path, list_databases, query_database
from app.product_stream import OperationError, stream_operation
from app.query_export import QueryExportStore, result_delivery
from app.step6_demo import conversation_lock, run_conversation
from app.telemetry import TraceStore, span
from app.tools.db import ReadOnlyViolation
from app.tools.postgres import MAX_POSTGRES_TABLES, POSTGRES_SOURCE_ID, PostgresDB, PostgresSource
from app.upload_body import BoundedJSON, bounded_body
from sandbox.step7.protocol import (
    MAX_HEADER_DEPTH, MAX_HEADER_ROW, MAX_SHEETS, MAX_UPLOAD_BYTES, FileLayout,
)

Question = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
DatabaseID = Annotated[str, StringConstraints(pattern=DB_ID_PATTERN)]
TableName = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=63)]
TableSelection = Annotated[list[TableName], Field(min_length=1, max_length=MAX_POSTGRES_TABLES)]


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Chat(Payload):
    session_id: UUID
    question: Question
    live: StrictBool = False
    database: DatabaseID | None = None
    tables: TableSelection | None = None
    evidence: str = Field(default="", max_length=1500)


class Approval(Payload):
    session_id: UUID
    approval_id: ApprovalID
    approve: StrictBool


class DatabaseQuery(Payload):
    database: DatabaseID
    question: Question
    tables: TableSelection | None = None
    evidence: str = Field(default="", max_length=1500)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.root = Path(os.getenv("RAG_DATA_DIR", PROJECT_ROOT / "data" / "step8")).resolve()
    app.state.traces = TraceStore(app.state.root / "traces.sqlite")
    app.state.history = ConversationHistory(app.state.root / "conversation_history.sqlite")
    app.state.file_history = FileAnalysisHistory(app.state.root / "file_analysis.sqlite")
    app.state.query_exports = QueryExportStore(app.state.root / "query_exports.sqlite")
    bird = os.getenv("RAG_BIRD_ROOT")
    app.state.bird = Path(bird).resolve() if bird else None
    app.state.settings = load_settings()
    app.state.postgres = PostgresSource.from_env()
    app.state.postgres_metadata_slots = asyncio.Semaphore(4)
    app.state.gateway_key = configured_key()
    app.state.upload_slots = asyncio.Semaphore(4)
    if app.state.gateway_key and app.state.settings.llm_configured:
        model_url = urlsplit(app.state.settings.llm_base_url or "")
        if model_url.scheme != "https" or not model_url.hostname or model_url.username or model_url.query or model_url.fragment:
            raise RuntimeError("Production model API must be configured with an HTTPS base URL")
    yield


app = FastAPI(title="Text2SQL Agent", version="1.0.0", lifespan=lifespan)
app.add_middleware(BoundedJSON)
_STREAM = {200: {"content": {"application/x-ndjson": {}}, "description": "Workflow events and final trace summary"}}


@app.middleware("http")
async def private_responses(request: Request, call_next):
    key = request.app.state.gateway_key
    request.state.owner = ""
    if key is not None and request.url.path != "/healthz":
        user = gateway_user(request.headers, key)
        if user is None:
            detail = "需要有效的入口身份"
            if is_chat_request(request):
                return await chat_http_error(request, 401, detail)
            return JSONResponse({"detail": detail}, status_code=401, headers={"Cache-Control": "no-store"})
        request.state.owner = owner_id(user)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            allowed_origin = "https://" + request.headers.get("host", "").casefold()
            if request.headers.get("sec-fetch-site") == "cross-site" or (origin is not None and origin.casefold() != allowed_origin):
                detail = "跨站请求被拒绝"
                if is_chat_request(request):
                    return await chat_http_error(request, 403, detail)
                return JSONResponse({"detail": detail}, status_code=403, headers={"Cache-Control": "no-store"})
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(RequestValidationError)
async def validation_failure(request, _exc):
    detail = "请求格式有误，请检查字段、类型和长度"
    if is_chat_request(request):
        return await chat_http_error(request, 422, detail)
    return JSONResponse({"detail": detail}, status_code=422)


@app.exception_handler(HTTPException)
async def http_failure(request, exc):
    if is_chat_request(request):
        return await chat_http_error(request, exc.status_code, exc.detail, headers=exc.headers)
    return await http_exception_handler(request, exc)


@app.get("/healthz")
def health():
    return {"status": "ok"}


def _conversation_root(owner: str) -> Path:
    root = app.state.root
    return root / "users" / owner / "conversation" if owner else root / "conversation"


def _conversation(
    command: Literal["ask", "decide", "status"], session_id: UUID, settings=None,
    *, owner: str, assistant=None, data_source=None, data_tables: tuple[str, ...] | None = None,
    lock_held: bool = False, **fields,
):
    args = argparse.Namespace(command=command, session=str(session_id), **fields)
    try:
        with span(f"conversation_{command}"):
            return run_conversation(
                _conversation_root(owner), args, settings, assistant=assistant, data_source=data_source,
                data_tables=data_tables, lock_held=lock_held,
            )
    except ValueError as exc:
        if command == "decide":
            raise OperationError("本会话没有对应的待审批操作，或该审批已处理") from exc
        raise


def _live_settings():
    settings = app.state.settings
    if not (settings.llm_configured and settings.llm_base_url):
        raise HTTPException(409, "请先配置模型的 API 地址和密钥")
    return settings


def _resolve_database(database_id: str) -> Path | PostgresSource:
    if database_id == POSTGRES_SOURCE_ID and app.state.postgres is not None:
        return app.state.postgres
    root = app.state.bird
    database = database_path(root, database_id) if root is not None else None
    if database is None:
        raise HTTPException(404, "该只读数据库未配置")
    return database


def _table_selection(names: list[str]) -> tuple[str, ...]:
    if len(set(names)) != len(names) or any(
        any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in name)
        or len(name.encode("utf-8")) > 63
        for name in names
    ):
        raise HTTPException(422, "所选表名无效或重复")
    return tuple(sorted(names))


async def _postgres_catalog(
    source: PostgresSource, tables: tuple[str, ...] | None = None, *, include_samples: bool = False,
) -> list[dict]:
    def read():
        with PostgresDB(source, tables=tables) as db:
            return db.catalog(include_samples=include_samples)

    slots = app.state.postgres_metadata_slots
    if slots.locked():
        raise HTTPException(503, "读取表结构的请求较多，请稍后再试")
    async with slots:
        try:
            catalog = await run_in_threadpool(read)
        except ReadOnlyViolation:
            if tables is not None:
                raise HTTPException(422, "所选表不在当前 PostgreSQL 数据源的可用范围内") from None
            raise HTTPException(503, "暂时无法读取 PostgreSQL 表结构，请稍后再试") from None
        except (sqlite3.Error, TimeoutError):
            raise HTTPException(503, "暂时无法读取 PostgreSQL 表结构，请检查连接后重试") from None
    if tables is not None and set(tables) != {row["name"] for row in catalog}:
        raise HTTPException(422, "所选表不在当前 PostgreSQL 数据源的可用范围内")
    return catalog


async def _validated_tables(database: Path | PostgresSource | None, names: list[str] | None) -> tuple[str, ...] | None:
    if names is None:
        return None
    if not isinstance(database, PostgresSource):
        raise HTTPException(422, "仅 PostgreSQL 数据源支持指定查询表")
    tables = _table_selection(names)
    await _postgres_catalog(database, tables)
    return tables


def _session_stream(
    request: Request, operation: str, session_id: UUID, prompt: str, run: Callable[[], dict], *,
    data_source: str | None = None,
):
    history = request.app.state.history
    owner = request.state.owner
    prepare = None
    if operation == "chat":
        run, prepare = result_delivery(request.app.state.query_exports, owner, data_source or "演示订单库", run)

    def record(reply: dict, trace: dict) -> None:
        history.append(owner, str(session_id), prompt, reply, trace)

    return stream_operation(
        request.app.state.traces, operation, run, owner=owner, on_result=prepare, on_outcome=record,
        guard=lambda: conversation_lock(_conversation_root(owner)),
    )


@app.post("/api/v1/chat", response_class=StreamingResponse, responses=_STREAM)
async def chat(body: Chat, request: Request):
    settings = _live_settings() if body.live else None
    database = _resolve_database(body.database) if body.database is not None else None
    tables = await _validated_tables(database, body.tables)

    def assistant(demo_database, question, previous, pending):
        return answer_data_turn(
            question, previous, pending, database=database or demo_database, demo=database is None,
            settings=settings, evidence=body.evidence, tables=tables,
        )

    owner = request.state.owner
    return _session_stream(request, "chat", body.session_id, body.question, lambda: _conversation(
        "ask", body.session_id, settings, owner=owner, assistant=assistant, data_source=body.database,
        question=body.question, data_tables=tables, lock_held=True,
    ), data_source=body.database)


@app.get("/api/v1/sessions/{session_id}")
def session(session_id: UUID, request: Request):
    with tracing_context(enabled=False):
        return _conversation("status", session_id, owner=request.state.owner)


@app.get("/api/v1/sessions/{session_id}/messages")
def session_messages(
    session_id: UUID, request: Request, limit: Annotated[int, Query(ge=1, le=50)] = 20,
    before: Annotated[int, Query(ge=1, le=2**63 - 1)] | None = None,
):
    return request.app.state.history.page(request.state.owner, str(session_id), limit, before)


@app.post("/api/v1/approvals", response_class=StreamingResponse, responses=_STREAM)
async def approval(body: Approval, request: Request):
    owner = request.state.owner
    action = "批准更新" if body.approve else "拒绝更新"
    return _session_stream(request, "approval", body.session_id, action, lambda: _conversation(
        "decide", body.session_id, owner=owner, approval_id=body.approval_id, approve=body.approve, lock_held=True,
    ))


SheetParam = Annotated[int, Query(ge=0, lt=MAX_SHEETS)]
HeaderRowParam = Annotated[int, Query(ge=1, le=MAX_HEADER_ROW)]
HeaderDepthParam = Annotated[int, Query(ge=1, le=MAX_HEADER_DEPTH)]


async def _upload_bytes(request: Request) -> bytes:
    if request.app.state.gateway_key and request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/octet-stream":
        raise HTTPException(415, "上传需要 application/octet-stream")
    slots = request.app.state.upload_slots
    if slots.locked():
        raise HTTPException(503, "上传任务较多，请稍后再试")
    async with slots:
        return await bounded_body(request, MAX_UPLOAD_BYTES)


@app.post("/api/v1/files/preview", response_class=StreamingResponse, responses=_STREAM)
async def preview_file(
    request: Request, filename: Annotated[str, Query(min_length=1, max_length=255)],
    sheet: SheetParam | None = None, header_row: HeaderRowParam | None = None, header_depth: HeaderDepthParam | None = None,
):
    data = await _upload_bytes(request)
    layout = FileLayout(sheet=sheet, header_row=header_row, header_depth=header_depth)

    def preview():
        try:
            return {"kind": "preview", "preview": preview_upload(filename, data, layout).model_dump(mode="json")}
        except ValueError as exc:
            raise OperationError(str(exc)) from exc

    return stream_operation(app.state.traces, "file_preview", preview, owner=request.state.owner)


@app.post(
    "/api/v1/files", response_class=StreamingResponse, responses=_STREAM,
    openapi_extra={"requestBody": {"required": True, "content": {"application/octet-stream": {"schema": {
        "type": "string", "format": "binary",
    }}}}},
)
async def files(
    request: Request, filename: Annotated[str, Query(min_length=1, max_length=255)],
    question: Annotated[str, Query(min_length=1, max_length=500)], live: bool = False,
    sheet: SheetParam | None = None, header_row: HeaderRowParam | None = None, header_depth: HeaderDepthParam | None = None,
    session_id: UUID | None = None,
):
    settings = _live_settings() if live else None
    data = await _upload_bytes(request)
    layout = FileLayout(sheet=sheet, header_row=header_row, header_depth=header_depth)

    def analyze():
        try:
            result = analyze_upload(filename, data, question, settings, layout=layout)
        except ValueError as exc:
            raise OperationError(str(exc)) from exc
        return {
            "kind": result.kind, "message": result.narrative,
            "report": result.report.model_dump(mode="json", exclude={"message"}) if result.report is not None else None,
        }

    owner = request.state.owner
    if session_id is None:
        return stream_operation(app.state.traces, "file_analysis", analyze, owner=owner)

    history = request.app.state.file_history
    key = (owner, str(session_id), str(uuid4()))

    def record(reply: dict, trace: dict) -> None:
        if reply.get("kind") not in {"file", "clarification", "unsupported"}:
            trace["history_stored"] = False
            return
        snapshot = {
            "filename": filename, "digest": sha256(data).hexdigest(), "question": question, "live": live,
            "layout": layout.model_dump(exclude_none=True), "result": reply, "trace": trace,
        }
        trace["history_stored"] = history.save(*key, snapshot, data)

    return stream_operation(app.state.traces, "file_analysis", analyze, owner=owner, on_outcome=record,
                            guard=lambda: history.track(*key))


@app.get("/api/v1/sessions/{session_id}/file-analysis")
def file_analysis_snapshot(session_id: UUID, request: Request):
    return request.app.state.file_history.snapshot(request.state.owner, str(session_id))


@app.get("/api/v1/sessions/{session_id}/file-analysis/upload")
def file_analysis_upload(session_id: UUID, trace_id: UUID, request: Request):
    data = request.app.state.file_history.upload(request.state.owner, str(session_id), str(trace_id))
    if data is None:
        raise HTTPException(404, "未找到对应的已保存文件，请刷新会话")
    return Response(data, media_type="application/octet-stream", headers={
        "Content-Disposition": 'attachment; filename="saved-upload"',
        "X-Content-Type-Options": "nosniff",
    })


@app.delete("/api/v1/sessions/{session_id}/file-analysis", status_code=204)
def delete_file_analysis(session_id: UUID, request: Request):
    request.app.state.file_history.delete(request.state.owner, str(session_id))
    return Response(status_code=204)


@app.get("/api/v1/databases")
def databases():
    settings = app.state.settings
    available = list_databases(app.state.bird)
    postgres = app.state.postgres is not None
    if postgres and POSTGRES_SOURCE_ID not in available:
        available.append(POSTGRES_SOURCE_ID)
    return {
        "databases": available,
        "postgres": postgres,
        "model_configured": bool(settings.llm_configured and settings.llm_base_url),
    }


@app.get("/api/v1/databases/{database_id}/schema")
async def database_schema(database_id: DatabaseID, table: TableName | None = None, include_samples: bool = False):
    database = _resolve_database(database_id)
    if not isinstance(database, PostgresSource):
        raise HTTPException(404, "该数据源不提供表结构浏览")
    if include_samples and table is None:
        raise HTTPException(422, "查看示例值前请选择一张表")
    tables = _table_selection([table]) if table is not None else None
    return {"database": database_id, "tables": await _postgres_catalog(database, tables, include_samples=include_samples)}


@app.post("/api/v1/query", response_class=StreamingResponse, responses=_STREAM)
async def query(body: DatabaseQuery, request: Request):
    database = _resolve_database(body.database)
    tables = await _validated_tables(database, body.tables)
    settings = _live_settings()
    owner = request.state.owner

    def execute():
        result = query_database(database, body.question, body.evidence, settings, tables=tables)
        if tables is not None and result["kind"] == "query":
            result["source_tables"] = list(tables)
        return result

    run, prepare = result_delivery(app.state.query_exports, owner, body.database, execute)
    return stream_operation(app.state.traces, "text2sql", run, owner=owner, on_result=prepare)


@app.get("/api/v1/results/{trace_id}/csv")
def query_csv(trace_id: UUID, request: Request):
    payload = request.app.state.query_exports.get(str(trace_id), request.state.owner)
    if payload is None:
        raise HTTPException(404, "未找到完整 CSV，结果可能已过期，请重新查询")
    return Response(payload, media_type="text/csv", headers={
        "Content-Disposition": f'attachment; filename="query-{trace_id}.csv"',
    })


@app.get("/api/v1/traces/{trace_id}")
def trace(trace_id: UUID, request: Request):
    report = app.state.traces.get(str(trace_id), owner=request.state.owner)
    if report is None:
        raise HTTPException(404, "未找到追踪记录")
    return report

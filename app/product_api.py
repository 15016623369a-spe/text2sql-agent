"""FastAPI entry point for the conversational, SQL, and upload workflows."""
from __future__ import annotations

import argparse
import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
from typing import Annotated, Literal
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import StreamingResponse
from langsmith import tracing_context
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StringConstraints
from starlette.responses import JSONResponse

from app.commerce_demo import ApprovalID
from app.config import PROJECT_ROOT, load_settings
from app.file_analysis import analyze_upload, preview_upload
from app.gateway_identity import configured_key, gateway_user, owner_id
from app.product_assistant import answer_data_turn
from app.product_sql import DB_ID_PATTERN, database_path, list_databases, query_database
from app.product_stream import OperationError, stream_operation
from app.step6_demo import run_conversation
from app.telemetry import TraceStore, span
from app.upload_body import BoundedJSON, bounded_body
from sandbox.step7.protocol import (
    MAX_HEADER_DEPTH, MAX_HEADER_ROW, MAX_SHEETS, MAX_UPLOAD_BYTES, FileLayout,
)

Question = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Chat(Payload):
    session_id: UUID
    question: Question
    live: StrictBool = False
    database: Annotated[str, StringConstraints(pattern=DB_ID_PATTERN)] | None = None
    evidence: str = Field(default="", max_length=1500)


class Approval(Payload):
    session_id: UUID
    approval_id: ApprovalID
    approve: StrictBool


class DatabaseQuery(Payload):
    database: Annotated[str, StringConstraints(pattern=DB_ID_PATTERN)]
    question: Question
    evidence: str = Field(default="", max_length=1500)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.root = Path(os.getenv("RAG_DATA_DIR", PROJECT_ROOT / "data" / "step8")).resolve()
    app.state.traces = TraceStore(app.state.root / "traces.sqlite")
    bird = os.getenv("RAG_BIRD_ROOT")
    app.state.bird = Path(bird).resolve() if bird else None
    app.state.settings = load_settings()
    app.state.gateway_key = configured_key()
    app.state.upload_slots = asyncio.Semaphore(4)
    if app.state.gateway_key and app.state.settings.llm_configured:
        model_url = urlsplit(app.state.settings.llm_base_url or "")
        if model_url.scheme != "https" or not model_url.hostname or model_url.username or model_url.query or model_url.fragment:
            raise RuntimeError("Production model API must be configured with an HTTPS base URL")
    yield


app = FastAPI(title="Text2SQL Agent", version="0.8.0", lifespan=lifespan)
app.add_middleware(BoundedJSON)
_STREAM = {200: {"content": {"application/x-ndjson": {}}, "description": "Workflow events and final trace summary"}}


@app.middleware("http")
async def private_responses(request: Request, call_next):
    key = request.app.state.gateway_key
    request.state.owner = ""
    if key is not None and request.url.path != "/healthz":
        user = gateway_user(request.headers, key)
        if user is None:
            return JSONResponse({"detail": "需要有效的入口身份"}, status_code=401, headers={"Cache-Control": "no-store"})
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            allowed_origin = "https://" + request.headers.get("host", "").casefold()
            if request.headers.get("sec-fetch-site") == "cross-site" or (origin is not None and origin.casefold() != allowed_origin):
                return JSONResponse({"detail": "跨站请求被拒绝"}, status_code=403, headers={"Cache-Control": "no-store"})
        request.state.owner = owner_id(user)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(RequestValidationError)
async def validation_failure(_request, _exc):
    return JSONResponse({"detail": "请求格式有误，请检查字段、类型和长度"}, status_code=422)


@app.get("/healthz")
def health():
    return {"status": "ok"}


def _conversation(
    command: Literal["ask", "decide", "status"], session_id: UUID, settings=None,
    *, owner: str, assistant=None, data_source=None, **fields,
):
    args = argparse.Namespace(command=command, session=str(session_id), **fields)
    try:
        with span(f"conversation_{command}"):
            root = app.state.root / "conversation"
            if owner:
                root = app.state.root / "users" / owner / "conversation"
            return run_conversation(root, args, settings, assistant=assistant, data_source=data_source)
    except ValueError as exc:
        if command == "decide":
            raise OperationError("本会话没有对应的待审批操作，或该审批已处理") from exc
        raise


def _live_settings():
    settings = app.state.settings
    if not (settings.llm_configured and settings.llm_base_url):
        raise HTTPException(409, "请先配置模型的 API 地址和密钥")
    return settings


@app.post("/api/v1/chat", response_class=StreamingResponse, responses=_STREAM)
async def chat(body: Chat, request: Request):
    settings = _live_settings() if body.live else None
    database = None
    if body.database is not None:
        root = app.state.bird
        database = database_path(root, body.database) if root is not None else None
        if database is None:
            raise HTTPException(404, "该只读数据库未配置")

    def assistant(demo_database, question, previous, pending):
        return answer_data_turn(
            question, previous, pending, database=database or demo_database, demo=database is None,
            settings=settings, evidence=body.evidence,
        )

    owner = request.state.owner
    return stream_operation(app.state.traces, "chat", lambda: _conversation(
        "ask", body.session_id, settings, owner=owner, assistant=assistant, data_source=body.database,
        question=body.question,
    ), owner=owner)


@app.get("/api/v1/sessions/{session_id}")
def session(session_id: UUID, request: Request):
    with tracing_context(enabled=False):
        return _conversation("status", session_id, owner=request.state.owner)


@app.post("/api/v1/approvals", response_class=StreamingResponse, responses=_STREAM)
async def approval(body: Approval, request: Request):
    owner = request.state.owner
    return stream_operation(app.state.traces, "approval", lambda: _conversation(
        "decide", body.session_id, owner=owner, approval_id=body.approval_id, approve=body.approve,
    ), owner=owner)


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
    sheet: SheetParam | None = None, header_row: HeaderRowParam | None = None, header_depth: HeaderDepthParam = 1,
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
    sheet: SheetParam | None = None, header_row: HeaderRowParam | None = None, header_depth: HeaderDepthParam = 1,
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

    return stream_operation(app.state.traces, "file_analysis", analyze, owner=request.state.owner)


@app.get("/api/v1/databases")
def databases():
    settings = app.state.settings
    return {
        "databases": list_databases(app.state.bird),
        "model_configured": bool(settings.llm_configured and settings.llm_base_url),
    }


@app.post("/api/v1/query", response_class=StreamingResponse, responses=_STREAM)
async def query(body: DatabaseQuery, request: Request):
    root = app.state.bird
    database = database_path(root, body.database) if root is not None else None
    if database is None:
        raise HTTPException(404, "该只读数据库未配置")
    settings = _live_settings()
    return stream_operation(app.state.traces, "text2sql", lambda: query_database(
        database, body.question, body.evidence, settings,
    ), owner=request.state.owner)


@app.get("/api/v1/traces/{trace_id}")
def trace(trace_id: UUID, request: Request):
    report = app.state.traces.get(str(trace_id), owner=request.state.owner)
    if report is None:
        raise HTTPException(404, "未找到追踪记录")
    return report

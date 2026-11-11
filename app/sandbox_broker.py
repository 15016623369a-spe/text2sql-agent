"""Unix-socket service. The only supported operation launches the fixed Step 7 image."""
import asyncio
import subprocess

from fastapi import FastAPI, HTTPException, Request
from pydantic import TypeAdapter
from starlette.concurrency import run_in_threadpool

from app.file_sandbox import SandboxError, local_docker, run_sandbox, worker_image
from app.upload_body import bounded_body
from sandbox.step7.protocol import MAX_PLAN_BYTES, MAX_UPLOAD_BYTES, WorkerRequest, WorkerResult

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
_request = TypeAdapter(WorkerRequest)
_slots = asyncio.Semaphore(4)


@app.get("/healthz")
def health():
    try:
        prefix, env = local_docker()
        subprocess.run(
            [*prefix, "image", "inspect", worker_image()], env=env, check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1.5,
        )
    except (OSError, subprocess.SubprocessError, SandboxError) as exc:
        raise HTTPException(503, "分析容器尚未就绪") from exc
    return {"status": "ok"}


@app.post("/analyze", response_model=WorkerResult)
async def analyze(request: Request):
    if _slots.locked():
        raise HTTPException(503, "分析服务繁忙")
    async with _slots:
        return await _analyze(request)


async def _analyze(request: Request):
    body = await bounded_body(request, MAX_UPLOAD_BYTES + MAX_PLAN_BYTES + 1)
    raw_plan, sep, data = body.partition(b"\n")
    if not sep or not data or len(raw_plan) > MAX_PLAN_BYTES or len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(422, "文件分析请求格式有误")
    try:
        plan = _request.validate_json(raw_plan)
    except ValueError as exc:
        raise HTTPException(422, "文件分析计划格式有误") from exc
    try:
        return await run_in_threadpool(run_sandbox, plan, data)
    except SandboxError as exc:
        raise HTTPException(422, str(exc)) from exc

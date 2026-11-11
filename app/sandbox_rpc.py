"""Send only an approved analysis plan and the file to the local broker."""
from pathlib import Path

import httpx
from pydantic import TypeAdapter

from app.file_sandbox import SandboxError
from sandbox.step7.protocol import WorkerRequest, WorkerResult

_reply = TypeAdapter(WorkerResult)


def analyze_via_broker(socket: Path, request: WorkerRequest, data: bytes) -> WorkerResult:
    payload = request.model_dump_json().encode() + b"\n" + data
    try:
        with httpx.Client(
            transport=httpx.HTTPTransport(uds=str(socket)), base_url="http://sandbox",
            timeout=40, trust_env=False,
        ) as client:
            response = client.post("/analyze", content=payload)
        if response.status_code == 422:
            raise SandboxError(response.json()["detail"])
        response.raise_for_status()
        return _reply.validate_json(response.content)
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        raise SandboxError("文件分析服务暂不可用") from exc

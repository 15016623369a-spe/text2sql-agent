"""Pass an upload to the local Docker worker without mounting host paths."""
from __future__ import annotations

import os
import re
import subprocess
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError

from sandbox.step7.protocol import (
    MAX_REPLY_BYTES, WorkerFailure, WorkerRequest, WorkerResult,
)

IMAGE = "rag-step7-sandbox:1"
TIMEOUT_SECONDS = 30
SANDBOX_FLAGS = (
    "--rm", "--interactive", "--pull=never", "--network=none", "--ipc=none",
    "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
    "--user=65532:65532", "--pids-limit=64", "--memory=512m", "--memory-swap=512m",
    "--cpus=1", "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=64m", "--log-driver=none",
)
_reply = TypeAdapter(WorkerResult | WorkerFailure)


class SandboxError(RuntimeError):
    """A user-facing analysis or local Docker error."""


def local_docker() -> tuple[list[str], dict[str, str]]:
    endpoint = os.environ.get("DOCKER_HOST")
    if not endpoint or os.environ.get("DOCKER_CONTEXT"):
        try:
            current = subprocess.run(
                ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
                capture_output=True, text=True, timeout=5, check=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SandboxError("无法找到本机 Docker，请确认 Docker Desktop 已安装并启动") from exc
        endpoint = current.stdout.strip()
    parsed = urlsplit(endpoint)
    if parsed.scheme != "unix" or parsed.netloc or not parsed.path.startswith("/") or parsed.query or parsed.fragment:
        raise SandboxError("文件分析只允许本机 Unix socket 上的 Docker")
    env = {key: value for key, value in os.environ.items() if key not in {"DOCKER_CONTEXT", "DOCKER_HOST"}}
    return ["docker", "--host", endpoint], env


def worker_image() -> str:
    image = os.getenv("RAG_SANDBOX_IMAGE", IMAGE)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/@:-]{0,254}", image):
        raise SandboxError("分析镜像配置无效")
    return image


def run_sandbox(request: WorkerRequest, data: bytes) -> WorkerResult:
    prefix, env = local_docker()
    name = f"rag-step7-{uuid4().hex}"
    cmd = [*prefix, "run", *SANDBOX_FLAGS, "--name", name, worker_image()]
    payload = request.model_dump_json().encode() + b"\n" + data
    try:
        completed = subprocess.run(cmd, input=payload, capture_output=True, timeout=TIMEOUT_SECONDS, env=env)
    except subprocess.TimeoutExpired as exc:
        try:
            subprocess.run([*prefix, "rm", "-f", name], capture_output=True, timeout=5, env=env)
        except (OSError, subprocess.SubprocessError):
            pass
        raise SandboxError("分析超时，请缩小文件后再试") from exc
    except OSError as exc:
        raise SandboxError("无法启动本机 Docker 沙箱") from exc

    if completed.returncode == 125:
        raise SandboxError("Docker 沙箱无法启动，请确认 Docker Desktop 和 Step 7 镜像已就绪")
    if completed.returncode != 0:
        raise SandboxError("沙箱未完成分析，文件可能无法读取或超出资源上限")
    if len(completed.stdout) > MAX_REPLY_BYTES:
        raise SandboxError("分析结果超过可展示的大小")
    try:
        reply = _reply.validate_json(completed.stdout)
    except ValidationError as exc:
        raise SandboxError("沙箱返回的分析结果格式有误") from exc
    if isinstance(reply, WorkerFailure):
        raise SandboxError(reply.error)
    return reply

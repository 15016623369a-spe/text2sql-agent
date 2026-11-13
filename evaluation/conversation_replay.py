"""Record or replay model responses for the synthetic product API regression only."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib import request as urllib_request

from app.config import _load_dotenv
from app.telemetry import TOKEN_FIELDS

_BASE_URL = "https://api.deepseek.com"
_MODEL = "deepseek-v4-pro"
_MAX_BODY_BYTES = 1024 * 1024
_OPEN = urllib_request.urlopen


class _Response(io.BytesIO):
    status = 200


class _Cassette:
    def __init__(self, replay: Path | None):
        self.replaying = replay is not None
        self.entries: dict[str, list[dict]] = {}
        self.used: dict[str, int] = {}
        self.errors: list[str] = []
        if replay is not None:
            fixture = json.loads(replay.read_text(encoding="utf-8"))
            if fixture["format"] != 1 or fixture["model"] != _MODEL:
                raise ValueError("Unknown synthetic model cassette format or model")
            self.entries = fixture["responses"]

    def open(self, request: urllib_request.Request, *, timeout: float) -> _Response:
        if request.full_url != f"{_BASE_URL}/chat/completions" or request.get_method() != "POST":
            raise ValueError("The synthetic recorder only handles the configured model endpoint")
        payload = json.loads(request.data)
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        key = hashlib.sha256(encoded).hexdigest()
        if self.replaying:
            responses = self.entries.get(key, [])
            index = self.used.get(key, 0)
            if index >= len(responses):
                self.errors.append(f"Missing completion for prompt {key[:12]}")
                raise ValueError("No recorded response for this exact model request")
            self.used[key] = index + 1
            response = responses[index]
        else:
            with _OPEN(request, timeout=timeout) as upstream:
                raw = upstream.read(_MAX_BODY_BYTES + 1)
                if upstream.status != 200 or len(raw) > _MAX_BODY_BYTES:
                    raise ValueError("Cannot record an unsuccessful or oversized model response")
            response = self._sanitized(json.loads(raw))
            self.entries.setdefault(key, []).append(response)
        return _Response(json.dumps(response, ensure_ascii=False).encode())

    @staticmethod
    def _sanitized(body: dict) -> dict:
        content = body["choices"][0]["message"]["content"]
        if not isinstance(content, str):
            raise ValueError("Expected synthetic SQL or clarification as model response text")
        secret = os.environ.get("LLM_API_KEY")
        if secret and secret in content:
            raise ValueError("Refusing to record a response that contains the model credential")
        # Only retain the completion used by the app and fixed numeric usage counters.
        usage = body.get("usage") or {}
        return {
            "choices": [{"message": {"content": content}}],
            "usage": {name: usage[name] for name in TOKEN_FIELDS if type(usage.get(name)) is int and usage[name] >= 0},
        }

    def finish(self) -> list[str]:
        for key, responses in self.entries.items():
            if self.replaying and len(responses) != self.used.get(key, 0):
                self.errors.append(f"Unused completion for prompt {key[:12]}")
        return self.errors

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fixture = {"format": 1, "model": _MODEL, "responses": self.entries}
        path.write_text(json.dumps(fixture, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run_recorded(run, *, record: Path | None, replay: Path | None, env_file: Path | None) -> dict:
    from fastapi.testclient import TestClient

    from app.product_api import app

    with TemporaryDirectory(prefix="rag-conversations-") as state, patch.dict(os.environ, {
        "RAG_DATA_DIR": state, "RAG_DEPLOYMENT": "local", "RAG_GATEWAY_KEY": "", "RAG_BIRD_ROOT": "",
        "RAG_LANGSMITH_EXPORT": "false", "LANGSMITH_TRACING": "false",
        "LLM_PROVIDER": "deepseek", "LLM_BASE_URL": _BASE_URL, "LLM_MODEL": _MODEL, "LLM_TEMPERATURE": "0",
    }):
        if replay is not None:
            os.environ["LLM_API_KEY"] = "synthetic-replay"
        else:
            _load_dotenv(env_file)
            if not os.environ.get("LLM_API_KEY", "").strip():
                raise ValueError("Set LLM_API_KEY before recording the synthetic conversations")
        cassette = _Cassette(replay)
        with patch("app.llm.urllib.request.urlopen", cassette.open), TestClient(app) as client:
            def send(payload):
                response = client.post("/api/v1/chat", json=payload)
                if response.status_code != 200:
                    raise ValueError("Unexpected status from the synthetic product API")
                return [json.loads(line) for line in response.iter_lines() if line.strip()]

            report = run(send, transport="replay" if replay is not None else "record")
        if errors := cassette.finish():
            report["cassette_errors"] = errors
        if record is not None and report["passed"] == report["scenarios"] and report["model_calls"] and not errors:
            cassette.write(record)
    return report

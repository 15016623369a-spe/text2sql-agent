"""Minimal OpenAI-compatible Chat Completions client."""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

from app.config import Settings
from app.telemetry import price_model_call, span

_MAX_RESPONSE_BYTES = 1024 * 1024


def chat_completion(
    settings: Settings,
    messages: list[dict[str, str]],
    *,
    max_tokens: int,
    json_output: bool = False,
) -> tuple[int, dict]:
    if not settings.llm_configured or not settings.llm_base_url:
        raise ValueError("Set LLM_API_KEY and LLM_BASE_URL before calling the model")

    payload = {
        "model": settings.llm_model,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    if json_output:
        payload["response_format"] = {"type": "json_object"}
        payload["temperature"] = settings.llm_temperature
    if urllib.parse.urlsplit(settings.llm_base_url).hostname == "api.deepseek.com":
        payload["thinking"] = {"type": "disabled"}

    request = urllib.request.Request(
        settings.llm_base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {settings.llm_api_key}",
        },
        method="POST",
    )
    with span("model_call") as measurement:
        with urllib.request.urlopen(request, timeout=30 if json_output else 15) as response:
            status = response.status
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(raw) > _MAX_RESPONSE_BYTES:
                raise ValueError("Model response exceeded the size limit")
            body = json.loads(raw)
        if not isinstance(body, dict):
            raise ValueError("Expected a JSON object from Chat Completions")
        if measurement is not None:
            measurement["usage"] = body.get("usage")
            price_model_call(measurement, settings.llm_base_url, settings.llm_model)
    return status, body


def completion_text(response: dict) -> str:
    content = response["choices"][0]["message"]["content"]
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Model returned no usable text")
    return content

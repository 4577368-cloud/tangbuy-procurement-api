"""OpenAI 兼容 LLM 客户端。"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any, Optional

import httpx

from app.core.config import get_settings


class ToolCall:
    def __init__(self, id: str, name: str, arguments: dict[str, str]):
        self.id = id
        self.name = name
        self.arguments = arguments


class LlmResponse:
    def __init__(self, content: Optional[str], tool_calls: list[ToolCall]):
        self.content = content
        self.tool_calls = tool_calls


def _llm_config() -> tuple[str, str, str]:
    settings = get_settings()
    base = settings.llm_model_base_url.strip().rstrip("/")
    key = settings.llm_model_api_key.strip()
    model = settings.llm_model_model_id.strip()
    if not base or not key or not model:
        raise RuntimeError(
            "LLM 未配置。请在 .env.local 设置 LLM_MODEL_BASE_URL / LLM_MODEL_API_KEY / LLM_MODEL_MODEL_ID"
        )
    return base, key, model


def _safe_parse_args(raw: str) -> dict[str, str]:
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return {k: str(v if v is not None else "") for k, v in parsed.items()}
    except json.JSONDecodeError:
        pass
    return {}


def chat_completion(
    messages: list[dict[str, Any]],
    tools: Optional[list[dict[str, Any]]] = None,
) -> LlmResponse:
    base, key, model = _llm_config()
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": 2000,
    }
    if tools:
        body["tools"] = [{"type": "function", "function": t} for t in tools]
        body["tool_choice"] = "auto"

    with httpx.Client(timeout=120.0) as client:
        res = client.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=body,
        )
    if res.status_code >= 400:
        raise RuntimeError(f"LLM 请求失败 ({res.status_code}): {res.text[:300]}")

    data = res.json()
    message = (data.get("choices") or [{}])[0].get("message") or {}
    tool_calls = [
        ToolCall(
            tc["id"],
            tc["function"]["name"],
            _safe_parse_args(tc["function"].get("arguments", "{}")),
        )
        for tc in (message.get("tool_calls") or [])
    ]
    return LlmResponse(message.get("content"), tool_calls)


def build_assistant_tool_call_message(tool_calls: list[ToolCall]) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": json.dumps(tc.arguments, ensure_ascii=False)},
            }
            for tc in tool_calls
        ],
    }


def new_tool_call_id() -> str:
    return f"call-{uuid.uuid4().hex[:12]}"

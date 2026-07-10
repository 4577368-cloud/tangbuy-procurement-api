"""OpenAI 兼容 LLM 客户端。"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any, Iterator, Optional

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

    try:
        data = res.json()
    except ValueError as exc:
        raise RuntimeError(f"LLM 返回非 JSON：{res.text[:200]}") from exc
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


def chat_completion_stream(
    messages: list[dict[str, Any]],
    *,
    temperature: float = 0.2,
    max_tokens: int = 1200,
) -> Iterator[str]:
    """流式文本增量（OpenAI 兼容 SSE 解析）。"""
    base, key, model = _llm_config()
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
    }

    with httpx.Client(timeout=120.0) as client:
        with client.stream(
            "POST",
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=body,
        ) as res:
            if res.status_code >= 400:
                raise RuntimeError(f"LLM 请求失败 ({res.status_code}): {res.read().decode()[:300]}")
            for line in res.iter_lines():
                if not line:
                    continue
                text = line.decode("utf-8") if isinstance(line, bytes) else line
                if not text.startswith("data:"):
                    continue
                payload = text[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    data = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                choices = data.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                content = delta.get("content")
                if content:
                    yield str(content)


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


def _parse_json_object(text: str) -> Optional[dict[str, Any]]:
    trimmed = (text or "").strip()
    start = trimmed.find("{")
    end = trimmed.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(trimmed[start : end + 1])
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def vision_chat_completion(
    text: str,
    image_url: str,
    *,
    temperature: float = 0.2,
    max_tokens: int = 800,
) -> str:
    """多模态 LLM：文本 + 商品主图 URL。"""
    import base64

    base, key, model = _llm_config()
    image_ref = (image_url or "").strip()
    if image_ref and not image_ref.startswith("data:"):
        try:
            with httpx.Client(timeout=20.0, follow_redirects=True) as client:
                res = client.get(
                    image_ref,
                    headers={"User-Agent": "Mozilla/5.0 tangbuy-procurement"},
                )
            if res.status_code < 400:
                ct = (res.headers.get("content-type") or "image/jpeg").split(";")[0]
                if ct.startswith("image/") and len(res.content) <= 4 * 1024 * 1024:
                    image_ref = f"data:{ct};base64,{base64.b64encode(res.content).decode('ascii')}"
        except Exception:
            pass

    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    if image_ref:
        content.append({"type": "image_url", "image_url": {"url": image_ref}})

    body = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    with httpx.Client(timeout=120.0) as client:
        res = client.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=body,
        )
    if res.status_code >= 400:
        raise RuntimeError(f"视觉模型请求失败 ({res.status_code}): {res.text[:300]}")

    data = res.json()
    message = (data.get("choices") or [{}])[0].get("message") or {}
    return str(message.get("content") or "").strip()


def parse_json_from_llm(text: str) -> Optional[dict[str, Any]]:
    return _parse_json_object(text)

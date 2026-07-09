"""出站 HTTP（httpx，忽略平台 HTTP_PROXY）。"""

from __future__ import annotations

import os
from typing import Any, Optional
from urllib.parse import urlparse, urlunparse

import httpx


def _apply_connect_ip_override(url: str, connect_ip_env: str) -> tuple[str, dict[str, str]]:
    """DNS 不可解析时（如 Render 部分区域），用 IP 直连并保留 Host 头。"""
    connect_ip = os.environ.get(connect_ip_env, "").strip()
    if not connect_ip:
        return url, {}
    parsed = urlparse(url)
    host = (parsed.hostname or "").strip()
    if not host:
        return url, {}
    port = parsed.port
    netloc = f"{connect_ip}:{port}" if port else connect_ip
    return urlunparse(parsed._replace(netloc=netloc)), {"Host": host}


def request_json(
    method: str,
    url: str,
    *,
    headers: Optional[dict[str, str]] = None,
    json_body: Optional[dict[str, Any]] = None,
    timeout: int = 45,
    connect_ip_env: str = "",
) -> dict[str, Any]:
    req_headers = dict(headers or {})
    if connect_ip_env:
        url, host_headers = _apply_connect_ip_override(url, connect_ip_env)
        req_headers.update(host_headers)
    try:
        with httpx.Client(trust_env=False, timeout=timeout, follow_redirects=True) as client:
            resp = client.request(method.upper(), url, headers=req_headers, json=json_body)
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, dict) else {"raw": data}
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:500]
        raise RuntimeError(f"HTTP {exc.response.status_code}: {detail}") from exc
    except httpx.RequestError as exc:
        raise RuntimeError(f"请求失败: {exc}") from exc

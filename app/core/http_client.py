"""出站 HTTP（httpx，忽略平台 HTTP_PROXY）。"""

from __future__ import annotations

import socket
from contextlib import contextmanager
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

_ORIGINAL_GETADDRINFO = socket.getaddrinfo


@contextmanager
def _resolve_host_to_ip(hostname: str, ip: str):
    """将指定域名解析到固定 IP（Render 等环境 DNS 不可用时），TLS 仍校验原域名证书。"""
    target = (hostname or "").strip()
    addr = (ip or "").strip()
    if not target or not addr:
        yield
        return

    def patched(host, port, family=0, type=0, proto=0, flags=0):
        if host == target:
            return _ORIGINAL_GETADDRINFO(addr, port, family, type, proto, flags)
        return _ORIGINAL_GETADDRINFO(host, port, family, type, proto, flags)

    socket.getaddrinfo = patched  # type: ignore[assignment]
    try:
        yield
    finally:
        socket.getaddrinfo = _ORIGINAL_GETADDRINFO


def request_json(
    method: str,
    url: str,
    *,
    headers: Optional[dict[str, str]] = None,
    json_body: Optional[dict[str, Any]] = None,
    timeout: int = 45,
    connect_ip: str = "",
) -> dict[str, Any]:
    host = (urlparse(url).hostname or "").strip()
    try:
        with _resolve_host_to_ip(host, connect_ip):
            with httpx.Client(trust_env=False, timeout=timeout, follow_redirects=True) as client:
                resp = client.request(method.upper(), url, headers=headers, json=json_body)
                resp.raise_for_status()
                data = resp.json()
                return data if isinstance(data, dict) else {"raw": data}
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:500]
        raise RuntimeError(f"HTTP {exc.response.status_code}: {detail}") from exc
    except httpx.RequestError as exc:
        raise RuntimeError(f"请求失败: {exc}") from exc

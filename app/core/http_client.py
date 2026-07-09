"""出站 HTTP（httpx，忽略平台 HTTP_PROXY）。"""

from __future__ import annotations

from typing import Any, Optional

import httpx


def request_json(
    method: str,
    url: str,
    *,
    headers: Optional[dict[str, str]] = None,
    json_body: Optional[dict[str, Any]] = None,
    timeout: int = 45,
) -> dict[str, Any]:
    try:
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

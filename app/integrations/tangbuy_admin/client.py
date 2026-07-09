"""Tangbuy Admin listOrderDetail HTTP 客户端。"""

from __future__ import annotations

import json
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request

from app.core.http_client import urlopen_direct

from app.core.config import get_settings
from app.integrations.tangbuy_admin.token_store import resolve_admin_token

LIST_ORDER_DETAIL = "/order/listOrderDetail"


class TangbuyAdminError(Exception):
    def __init__(self, message: str, *, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


def _admin_token() -> str:
    token = resolve_admin_token().strip()
    if not token or token == "your-admin-bearer-token":
        raise TangbuyAdminError(
            "未配置 Admin Token：请从 admin.tangbuy.cc 复制 cURL，运行 "
            "python3 scripts/sync_admin_token_from_curl.py <curl文件>"
        )
    return token


def admin_post(path: str, body: dict[str, Any], *, timeout: int = 45) -> dict[str, Any]:
    settings = get_settings()
    normalized = path if path.startswith("/") else f"/{path}"
    url = f"{settings.tangbuy_admin_base_url.rstrip('/')}{normalized}"
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json;charset=UTF-8",
            "Accept": "application/json",
            "Authorization": f"Bearer {_admin_token()}",
        },
        method="POST",
    )
    try:
        with urlopen_direct(req, timeout=timeout) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise TangbuyAdminError(f"Admin HTTP {exc.code}: {detail}", status=exc.code) from exc
    except URLError as exc:
        raise TangbuyAdminError(f"Admin 请求失败: {exc.reason}") from exc

    code = raw.get("code")
    if code in (401, "401"):
        raise TangbuyAdminError(
            "Admin Token 已失效：请登录 admin.tangbuy.cc 复制新 Admin-Token 到 TANGBUY_ADMIN_TOKEN 并重启 API",
            status=401,
        )
    if code not in (200, "200", 0, "0"):
        msg = raw.get("msg") or f"Admin 返回 code={code}"
        raise TangbuyAdminError(msg, status=int(code) if str(code).isdigit() else None)
    data = raw.get("data")
    return data if isinstance(data, dict) else {"raw": raw}


def list_order_detail(body: dict[str, Any], *, timeout: int = 90) -> dict[str, Any]:
    settings = get_settings()
    url = f"{settings.tangbuy_admin_base_url.rstrip('/')}{LIST_ORDER_DETAIL}"
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json;charset=UTF-8",
            "Accept": "application/json",
            "Authorization": f"Bearer {_admin_token()}",
        },
        method="POST",
    )
    try:
        with urlopen_direct(req, timeout=timeout) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise TangbuyAdminError(f"Admin HTTP {exc.code}: {detail}", status=exc.code) from exc
    except URLError as exc:
        raise TangbuyAdminError(f"Admin 请求失败: {exc.reason}") from exc

    code = raw.get("code")
    if code in (401, "401"):
        raise TangbuyAdminError(
            "Admin Token 已失效：请登录 admin.tangbuy.cc 复制新 Admin-Token 到 TANGBUY_ADMIN_TOKEN 并重启 API",
            status=401,
        )
    if code not in (200, "200", 0, "0"):
        msg = raw.get("msg") or f"Admin 返回 code={code}"
        raise TangbuyAdminError(msg, status=int(code) if str(code).isdigit() else None)

    data = raw.get("data")
    if not isinstance(data, dict):
        raise TangbuyAdminError("Admin 响应缺少 data")
    return data

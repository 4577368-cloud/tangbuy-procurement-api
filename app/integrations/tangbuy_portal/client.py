"""Tangbuy Portal 商品详情 gateway（itemGet）。"""

from __future__ import annotations

import json
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request

from app.core.http_client import urlopen_direct

from app.core.config import get_settings


class TangbuyPortalError(Exception):
    def __init__(self, message: str, *, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


def _portal_token() -> str:
    token = get_settings().tangbuy_portal_token.strip()
    if not token:
        raise TangbuyPortalError(
            "未配置 Portal Token：请设置 TANGBUY_PORTAL_TOKEN（www.tangbuy.cc Bearer JWT）"
        )
    return token


def item_get(*, product_page_url: str, timeout: int = 45) -> dict[str, Any]:
    """GET /gateway/product/v3/itemGet?url=..."""
    settings = get_settings()
    base = settings.tangbuy_portal_base_url.rstrip("/")
    encoded = quote(product_page_url.strip(), safe="")
    url = f"{base}/product/v3/itemGet?url={encoded}"
    req = Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {_portal_token()}",
            "currency": settings.tangbuy_portal_currency,
            "device": "pc",
            "lang": "cn",
        },
        method="GET",
    )
    try:
        with urlopen_direct(req, timeout=timeout) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise TangbuyPortalError(f"Portal HTTP {exc.code}: {detail}", status=exc.code) from exc
    except URLError as exc:
        raise TangbuyPortalError(f"Portal 请求失败: {exc.reason}") from exc

    code = raw.get("code")
    if code not in (200, "200"):
        msg = raw.get("msg") or raw.get("message") or f"code={code}"
        raise TangbuyPortalError(f"itemGet 失败：{msg}")
    data = raw.get("data")
    if not isinstance(data, dict):
        raise TangbuyPortalError("itemGet 返回无 data")
    item = data.get("item")
    if not isinstance(item, dict):
        raise TangbuyPortalError("itemGet 返回无 item")
    return item

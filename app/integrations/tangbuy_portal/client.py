"""Tangbuy Portal 商品详情 gateway（itemGet）。"""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import quote

from app.core.config import get_settings
from app.core.http_client import request_json


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
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {_portal_token()}",
        "currency": settings.tangbuy_portal_currency,
        "device": "pc",
        "lang": "cn",
    }
    try:
        raw = request_json("GET", url, headers=headers, timeout=timeout)
    except RuntimeError as exc:
        raise TangbuyPortalError(f"Portal {exc}") from exc

    code = raw.get("code")
    if code not in (200, "200"):
        msg = raw.get("msg") or raw.get("message") or f"code={code}"
        raise TangbuyPortalError(f"itemGet 失败：{msg}")
    data = raw.get("data")
    if not isinstance(data, dict):
        raise TangbuyPortalError("itemGet 响应缺少 data")
    return data

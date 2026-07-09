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


def _unwrap_item_get_payload(raw: dict[str, Any]) -> dict[str, Any]:
    code = raw.get("code")
    if code not in (200, "200"):
        msg = raw.get("msg") or raw.get("message") or f"code={code}"
        raise TangbuyPortalError(f"itemGet 失败：{msg}")

    data = raw.get("data")
    if data is None:
        raise TangbuyPortalError("itemGet 无商品数据")
    if not isinstance(data, dict):
        raise TangbuyPortalError("itemGet 响应缺少 data")

    # Portal 新格式：{ item, itemResultCode, detailUrl, validToken }
    if "item" in data:
        item = data.get("item")
        if not isinstance(item, dict):
            result_code = data.get("itemResultCode")
            raise TangbuyPortalError(f"itemGet 无商品详情（itemResultCode={result_code}）")
        return item

    # 旧格式：字段直接在 data 上
    if data.get("itemName") or data.get("productSkus") or data.get("itemId"):
        return data

    raise TangbuyPortalError("itemGet 响应缺少 data")


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
        raw = request_json(
            "GET",
            url,
            headers=headers,
            timeout=timeout,
            connect_ip=settings.tangbuy_portal_connect_ip,
        )
    except RuntimeError as exc:
        raise TangbuyPortalError(f"Portal {exc}") from exc

    return _unwrap_item_get_payload(raw)

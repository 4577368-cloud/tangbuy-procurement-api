"""Tangbuy Admin 接单池。"""

from __future__ import annotations

import json
from typing import Any, Optional

from app.core.config import get_settings
from app.integrations.tangbuy_admin.client import admin_post_any

LIST_ORDER_POOL = "/order/listOrderPool"
CONFIRM_LIST = "/order/confirmList"
MAX_CONFIRM_BATCH = 50


def list_order_pool(
    *,
    page_num: int = 1,
    page_size: int = 200,
    storage_no: Optional[int] = None,
    order_status: int = 1,
) -> Any:
    settings = get_settings()
    body: dict[str, Any] = {
        "pageNum": max(1, page_num),
        "pageSize": max(1, min(200, page_size)),
        "storageNo": storage_no if storage_no is not None else settings.tangbuy_admin_storage_no,
        "storeSource": None,
        "shopDiscountTag": None,
        "lang": None,
        "minVip": None,
        "maxVip": None,
        "userVip": None,
        "storeName": None,
        "discountType": None,
        "commentType": None,
        "festivalTag": None,
        "orderByColumn": 1,
        "poolType": "normal",
        "storeTags": None,
        "orderStatus": order_status,
    }
    return admin_post_any(LIST_ORDER_POOL, body)


def confirm_order_list(order_nos: list[str]) -> Any:
    cleaned = [str(n).strip() for n in order_nos if str(n).strip()]
    if not cleaned:
        raise ValueError("orderNos 不能为空")
    if len(cleaned) > MAX_CONFIRM_BATCH:
        raise ValueError(f"单次接单不超过 {MAX_CONFIRM_BATCH} 条")
    ids_json = json.dumps(cleaned, ensure_ascii=False)
    return admin_post_any(CONFIRM_LIST, {"ids": ids_json})

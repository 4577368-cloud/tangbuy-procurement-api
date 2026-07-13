"""Tangbuy Admin 1688 预订购与平台下单。"""

from __future__ import annotations

from typing import Any, Optional

from app.core.config import get_settings
from app.integrations.tangbuy_admin.client import admin_post_any, admin_get_any

ALIBABA_PRE_PURCHASE = "/order/alibaba/alibabaPrePurchase"
WAIT_GENERATE_ORDER_LIST = "/order/bnm/waitGenerateOrderList"
PLATFORM_ORDER_CREATE = "/trade/platform/order/create"
PLATFORM_ORDER_QUERY = "/trade/platform/order/query"


def alibaba_pre_purchase(item_nos: list[str]) -> Any:
    """调用 Admin 1688 预订购；itemNos 为子单号 ord_line_no。"""
    cleaned = [str(n).strip() for n in item_nos if str(n).strip()]
    if not cleaned:
        raise ValueError("itemNos 不能为空")
    return admin_post_any(ALIBABA_PRE_PURCHASE, {"itemNos": cleaned})


def wait_generate_order_list(
    *,
    page_num: int = 1,
    page_size: int = 200,
    order_no: Optional[str] = None,
    storage_no: Optional[int] = None,
    buyer_name: Optional[str] = None,
) -> Any:
    """待 1688 平台下单列表（goodsStatus=54）。"""
    settings = get_settings()
    body: dict[str, Any] = {
        "pageNum": max(1, page_num),
        "pageSize": max(1, min(200, page_size)),
        "storeName": None,
        "storeComment": None,
        "companyId": None,
        "priceDiffTag": None,
        "orderNo": order_no,
        "minFare": None,
        "maxFare": None,
        "discountType": None,
        "buyer": buyer_name or settings.tangbuy_admin_buyer_name,
        "goodsStatus": 54,
        "orderStatus": 2,
        "orderByColumn": 1,
        "storageNo": storage_no if storage_no is not None else settings.tangbuy_admin_storage_no,
        "storeTags": "",
    }
    return admin_post_any(WAIT_GENERATE_ORDER_LIST, body)


def create_platform_order(
    order_targets: list[dict[str, Any]],
    *,
    storage_no: Optional[int] = None,
    buyer_id: Optional[int] = None,
    buyer_name: Optional[str] = None,
    buyer_company_id: Optional[int] = None,
    buyer_company_name: Optional[str] = None,
) -> Any:
    """在 1688 平台创建采购订单。"""
    cleaned_targets: list[dict[str, Any]] = []
    for target in order_targets:
        order_no = str(target.get("orderNo") or "").strip()
        item_nos = [str(n).strip() for n in (target.get("itemNos") or []) if str(n).strip()]
        if not order_no or not item_nos:
            continue
        cleaned_targets.append(
            {
                "orderNo": order_no,
                "itemNos": item_nos,
                "remark": str(target.get("remark") or ""),
            }
        )
    if not cleaned_targets:
        raise ValueError("orderTargets 不能为空")

    settings = get_settings()
    body = {
        "orderTargets": cleaned_targets,
        "storageNo": storage_no if storage_no is not None else settings.tangbuy_admin_storage_no,
        "buyerId": buyer_id if buyer_id is not None else settings.tangbuy_admin_buyer_id,
        "buyerName": buyer_name or settings.tangbuy_admin_buyer_name,
        "buyerCompanyId": (
            buyer_company_id if buyer_company_id is not None else settings.tangbuy_admin_buyer_company_id
        ),
        "buyerCompanyName": buyer_company_name or settings.tangbuy_admin_buyer_company_name,
    }
    return admin_post_any(PLATFORM_ORDER_CREATE, body)


def query_platform_orders(
    *,
    page_num: int = 1,
    page_size: int = 30,
    status: int = 0,
    checking: int = 0,
    trade_order_no: Optional[str] = None,
    order_no: Optional[str] = None,
    item_no: Optional[str] = None,
) -> dict[str, Any]:
    """1688 订单回传列表（GET /trade/platform/order/query）。"""
    params: dict[str, Any] = {
        "pageNum": max(1, page_num),
        "pageSize": max(1, min(200, page_size)),
        "status": status,
        "checking": checking,
    }
    if trade_order_no:
        params["tradeOrderNo"] = trade_order_no.strip()
    if order_no:
        params["orderNo"] = order_no.strip()
    if item_no:
        params["itemNo"] = item_no.strip()
    raw = admin_get_any(PLATFORM_ORDER_QUERY, params=params, timeout=60)
    rows = raw.get("rows")
    return {
        "total": int(raw.get("total") or 0),
        "rows": rows if isinstance(rows, list) else [],
    }

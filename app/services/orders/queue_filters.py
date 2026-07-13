"""订单中心队列 → Admin listOrderDetail 筛选参数。"""

from __future__ import annotations

from typing import Any, Optional

# 与 web status-enums ORDER_QUEUE_TO_ORD_LINE_STAT 主码对齐
QUEUE_GOODS_STATUS: dict[str, int] = {
    "pending_procurement": 23,
    "pending_payment": -1,
    "ordered": 22,
    "shipped": 5,
    "in_warehouse": 10,
    "dispatched": 30,
    "exception": 25,
    "reverse": 24,
}

QUEUE_ORDER_STATUS: dict[str, int] = {
    "pending_procurement": 2,
    "pending_payment": 2,
    "ordered": 2,
}


def pending_procurement_admin_filters(event_type_pending: int = 5888) -> list[dict[str, Any]]:
    """待采购在 Admin 侧分散在多个 goodsStatus，需合并拉取。"""
    return [
        # 有量桶优先（避免 goodsStatus=0 空请求导致后续桶失败）
        {"goodsStatus": 23, "orderStatus": 2, "eventType": event_type_pending},
        {"goodsStatus": 54, "orderStatus": None, "eventType": None},
        # 待接单（新支付订单常见 goodsStatus=0 / orderStatus=1）
        {"goodsStatus": 0, "orderStatus": 1, "eventType": None},
    ]


def _base_list_body(
    *,
    page: int,
    page_size: int,
    storage_no: int,
) -> dict[str, Any]:
    return {
        "pageNum": max(page, 1),
        "pageSize": max(min(page_size, 200), 1),
        "storageNo": storage_no,
        "buyer": None,
        "orderByColumn": "5",
        "returnStatus": 0,
        "confirmStatus": 0,
        "buyerCidList": None,
        "rangPriceItem": None,
    }


def resolve_order_queue(row: dict[str, Any]) -> Optional[str]:
    goods = row.get("ord_line_stat")
    order_stat = row.get("ord_stat")
    rtn = row.get("rtn_stat") or 0
    abn = row.get("abn_type_cd") or 0

    if rtn and int(rtn) != 0:
        return "reverse"
    if abn and int(abn) != 0:
        return "exception"
    if goods in (25, 14, 33):
        return "exception"
    if goods in (24, 11):
        return "reverse"
    if goods in (0, 23, 54):
        return "pending_procurement"
    if goods in (-2, -1, 2, 55):
        return "pending_payment"
    if goods == 22:
        return "ordered"
    if goods in (5, 6, 8):
        return "shipped"
    if goods in (9, 10, 28, 29, 37, 58):
        return "in_warehouse"
    if goods in (30, 31):
        return "dispatched"
    if order_stat == 0:
        return "pending_payment"
    if order_stat in (1, 2):
        return "pending_procurement"
    return None


def build_list_body(
    *,
    page: int,
    page_size: int,
    storage_no: int,
    queue: Optional[str] = None,
    ord_line_no: Optional[str] = None,
    ord_no: Optional[str] = None,
    event_type_pending: int = 5888,
) -> dict[str, Any]:
    body: dict[str, Any] = _base_list_body(
        page=page,
        page_size=page_size,
        storage_no=storage_no,
    )
    if ord_line_no:
        body["itemNo"] = ord_line_no
        return body
    if ord_no:
        body["orderNo"] = ord_no
        return body

    if queue and queue != "all":
        goods_status = QUEUE_GOODS_STATUS.get(queue)
        if goods_status is not None:
            body["goodsStatus"] = goods_status
        order_status = QUEUE_ORDER_STATUS.get(queue)
        if order_status is not None:
            body["orderStatus"] = order_status
        if queue == "pending_procurement":
            body["eventType"] = event_type_pending
        else:
            body["eventType"] = None
    else:
        body["eventType"] = None
        body["orderStatus"] = None

    return body

"""订单回传 API → 子单级 1688 平台订单字段。"""

from __future__ import annotations

from typing import Any, Optional


def _num(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        n = float(v)
        return n if n == n else None
    except (TypeError, ValueError):
        return None


def _first_logistics(platform_detail: dict[str, Any]) -> dict[str, Any]:
    logistic = platform_detail.get("logisticInfo")
    if not isinstance(logistic, dict):
        return {}
    items = logistic.get("items")
    if not isinstance(items, list) or not items:
        return {}
    first = items[0]
    return first if isinstance(first, dict) else {}


def _match_detail_item(
    platform_detail: dict[str, Any],
    *,
    goods_id: Optional[str],
    quantity: Optional[float],
) -> Optional[dict[str, Any]]:
    items = platform_detail.get("items")
    if not isinstance(items, list):
        return None
    candidates = [i for i in items if isinstance(i, dict)]
    if not candidates:
        return None
    if goods_id:
        for item in candidates:
            if str(item.get("goodsId") or "") == goods_id:
                return item
    if quantity is not None:
        for item in candidates:
            if _num(item.get("quantity")) == quantity:
                return item
    return candidates[0] if len(candidates) == 1 else None


def map_platform_row_to_line_fields(
    platform_row: dict[str, Any],
    *,
    ord_line_no: str,
    order_no: Optional[str] = None,
) -> dict[str, Any]:
    """将一条订单回传记录映射为单个子单的宽表扩展字段。"""
    key = ord_line_no.strip()
    detail = platform_row.get("platformOrderDetail")
    detail_dict = detail if isinstance(detail, dict) else {}

    line_item: Optional[dict[str, Any]] = None
    tang_order_no = (order_no or "").strip()
    orders = platform_row.get("orders")
    if isinstance(orders, list):
        for order in orders:
            if not isinstance(order, dict):
                continue
            if tang_order_no and str(order.get("orderNo") or "") != tang_order_no:
                continue
            items = order.get("items")
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                if str(item.get("itemNo") or "").strip() == key:
                    line_item = item
                    tang_order_no = tang_order_no or str(order.get("orderNo") or "").strip()
                    break
            if line_item:
                break

    goods_id = None
    quantity = None
    if line_item:
        goods_url = str(line_item.get("goodsUrl") or "")
        if "/offer/" in goods_url:
            goods_id = goods_url.split("/offer/")[-1].split(".")[0].split("?")[0]
        quantity = _num(line_item.get("quantity"))

    detail_item = _match_detail_item(detail_dict, goods_id=goods_id, quantity=quantity)
    logistics = _first_logistics(detail_dict)

    platform_id = str(platform_row.get("platformOrderId") or detail_dict.get("id") or "").strip()
    out: dict[str, Any] = {
        "ord_line_no": key,
        "plt_ord_id": platform_id or None,
        "plt_trade_ord_no": str(platform_row.get("tradeOrderNo") or "").strip() or None,
        "plt_tang_ord_no": tang_order_no or None,
        "plt_goods_amt": _num(platform_row.get("platformGoodsAmount")),
        "plt_post_fee": _num(platform_row.get("platformPostFee")),
        "plt_total_amt": _num(platform_row.get("platformTotalAmount")),
        "plt_benefit_goods_amt": _num(platform_row.get("benefitGoodsAmount")),
        "plt_benefit_post_fee": _num(platform_row.get("benefitPostFee")),
        "plt_benefit_total_amt": _num(platform_row.get("benefitTotalAmount")),
        "plt_list_goods_amt": _num(platform_row.get("orderGoodsAmount")),
        "plt_list_post_fee": _num(platform_row.get("orderPostFee")),
        "plt_list_total_amt": _num(platform_row.get("orderTotalAmount")),
        "plt_sync_time": platform_row.get("lastSyncTime"),
        "plt_ord_detail_url": platform_row.get("platformOrderDetailUrl"),
        "plt_ord_status": detail_dict.get("status"),
        "plt_refund_status": detail_dict.get("refundStatus"),
        "plt_pay_time": detail_dict.get("payTime"),
        "plt_seller_login": detail_dict.get("sellerLoginId"),
        "plt_logistics_company": logistics.get("logisticsCompanyName"),
        "plt_logistics_no": logistics.get("mailNo"),
    }

    if line_item:
        out["plt_line_goods_amt"] = _num(line_item.get("amount"))
        out["plt_line_compare_prc"] = _num(line_item.get("comparePrice"))
        out["plt_line_qty"] = _num(line_item.get("quantity"))

    if detail_item:
        out["plt_line_unit_prc"] = _num(detail_item.get("price"))
        out["plt_line_detail_amt"] = _num(detail_item.get("amount"))
        out["plt_line_sku_desc"] = detail_item.get("skuDesc")
        out["plt_line_logistics_status"] = detail_item.get("logisticsStatus")

    if platform_id:
        out["pur_no"] = platform_id

    return {k: v for k, v in out.items() if v is not None}


def index_platform_rows_by_line(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """从订单回传列表构建 ord_line_no → 字段映射。"""
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        orders = row.get("orders")
        if not isinstance(orders, list):
            continue
        for order in orders:
            if not isinstance(order, dict):
                continue
            order_no = str(order.get("orderNo") or "").strip()
            items = order.get("items")
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                line_no = str(item.get("itemNo") or "").strip()
                if not line_no or line_no in indexed:
                    continue
                indexed[line_no] = map_platform_row_to_line_fields(
                    row,
                    ord_line_no=line_no,
                    order_no=order_no,
                )
    return indexed

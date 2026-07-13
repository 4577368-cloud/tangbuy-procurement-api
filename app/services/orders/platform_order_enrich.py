"""将订单回传快照合并进宽表行。"""

from __future__ import annotations

from typing import Any

from app.services.orders import platform_order_store

_PLATFORM_FIELDS = (
    "plt_ord_id",
    "plt_trade_ord_no",
    "plt_tang_ord_no",
    "plt_goods_amt",
    "plt_post_fee",
    "plt_total_amt",
    "plt_benefit_goods_amt",
    "plt_benefit_post_fee",
    "plt_benefit_total_amt",
    "plt_list_goods_amt",
    "plt_list_post_fee",
    "plt_list_total_amt",
    "plt_sync_time",
    "plt_ord_detail_url",
    "plt_ord_status",
    "plt_refund_status",
    "plt_pay_time",
    "plt_seller_login",
    "plt_logistics_company",
    "plt_logistics_no",
    "plt_line_goods_amt",
    "plt_line_compare_prc",
    "plt_line_qty",
    "plt_line_unit_prc",
    "plt_line_detail_amt",
    "plt_line_qty",
    "plt_line_detail_amt",
    "plt_line_sku_desc",
    "plt_line_logistics_status",
)


def enrich_row_platform_order_fields(row: dict[str, Any]) -> dict[str, Any]:
    key = str(row.get("ord_line_no") or "").strip()
    if not key:
        return row
    snap = platform_order_store.get_platform_order(key)
    if not snap:
        return row
    for field in _PLATFORM_FIELDS:
        val = snap.get(field)
        if val is not None and val != "":
            row[field] = val
    if snap.get("plt_ord_id") and not row.get("pur_no"):
        row["pur_no"] = snap["plt_ord_id"]
    return row

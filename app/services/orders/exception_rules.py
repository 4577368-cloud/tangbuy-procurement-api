"""订单异常规则（对齐 Web real-order-finance / ord-line-ui-mappers）。"""

from __future__ import annotations

from typing import Any, Literal, Optional

from app.services.orders.purchase_cost import resolve_purchase_cost_basis
from app.services.orders.procurement_scope import (
    allows_finance_reason,
    is_in_procurement_scope,
)
from app.services.orders.queue_filters import resolve_order_queue

EPS = 0.02
MARGIN_THRESHOLD = 5.0

ExceptionBand = Literal["action", "attention", "observe"]


def _num(v: Any, fallback: float = 0.0) -> float:
    try:
        n = float(v)
        return n if n == n else fallback  # noqa: PLR0124
    except (TypeError, ValueError):
        return fallback


def resolve_health(row: dict[str, Any]) -> str:
    if _num(row.get("abn_type_cd")) != 0:
        return "needs_action"
    if (row.get("ord_line_stat") or -999) in (25, 33):
        return "needs_action"
    if _num(row.get("rtn_stat")) != 0:
        return "needs_action"
    if _num(row.get("apply_refund_stat")) != 0:
        return "needs_action"
    return "normal"


def resolve_customer_paid(row: dict[str, Any]) -> float:
    product_amt = _num(row.get("pur_amt"))
    if product_amt <= 0:
        product_amt = _num(row.get("pur_prc")) * _num(row.get("ord_cnt"), 1)
    shipping_amt = _num(row.get("post_fee"))
    payable = round(product_amt + shipping_amt, 2)
    raw = round(_num(row.get("ds_ord_amt")), 2)
    if payable <= 0:
        return raw if raw > 0 else 0.0
    if raw <= 0:
        return payable
    if (
        shipping_amt > 0
        and abs(raw - product_amt) < EPS
        and abs(raw - payable) > EPS
    ):
        return payable
    if abs(raw - payable) < EPS:
        return payable
    return raw


def classify_exception_reason(
    row: dict[str, Any],
) -> Optional[tuple[ExceptionBand, str]]:
    """返回 (异常档位, 原因标签)；非异常返回 None。band 与 classify_exception 一致。"""
    if not is_in_procurement_scope(row):
        return None

    queue = resolve_order_queue(row) or "pending_procurement"
    health = resolve_health(row)

    if row.get("sku_mismatch") and queue == "pending_procurement":
        return "action", "规格不符"

    if row.get("note_block_procurement") and queue == "pending_procurement":
        signal_type = row.get("note_signal_type")
        if signal_type == "SKU_MISMATCH":
            return "action", "备注-规格变更"
        return "attention", "备注待核"

    cost = resolve_purchase_cost_basis(row)
    purchase_payable = _num(cost.get("purchase_payable"))
    suggested_gap = _num(cost.get("suggested_price_gap"))

    if suggested_gap > EPS and queue == "pending_procurement":
        return "attention", "采购价高于建议"

    customer_paid = resolve_customer_paid(row)
    margin = round(customer_paid - purchase_payable, 2)
    margin_pct = (margin / customer_paid * 100) if customer_paid > 0 else 0.0

    if customer_paid + EPS < purchase_payable:
        if allows_finance_reason(queue, "负毛利"):
            return "action", "负毛利"
    elif abs(margin) < EPS:
        if allows_finance_reason(queue, "零毛利"):
            return "action", "零毛利"
    elif margin_pct + EPS < MARGIN_THRESHOLD:
        if allows_finance_reason(queue, "低毛利"):
            return "attention", "低毛利"

    if queue == "reverse":
        if _num(row.get("apply_refund_stat")) != 0:
            return "action", "退款申请"
        if _num(row.get("rtn_stat")) != 0:
            return "action", "退换货"
        if _num(row.get("abn_type_cd")) != 0:
            return "action", "订单异常"
        return None
    if health == "needs_action" or queue == "exception":
        if _num(row.get("abn_type_cd")) != 0:
            return "action", "订单异常"
        if _num(row.get("rtn_stat")) != 0:
            return "action", "退款退货"
        if (row.get("ord_line_stat") or -999) in (25, 33):
            return "action", "状态异常"
        return "action", "异常待处理"
    return None


def classify_exception(row: dict[str, Any]) -> Optional[ExceptionBand]:
    result = classify_exception_reason(row)
    return result[0] if result else None


def scan_exception_summary(
    rows: list[dict[str, Any]],
) -> dict[str, int]:
    action = 0
    attention = 0
    observe = 0
    blocking = 0
    for row in rows:
        band = classify_exception(row)
        if band is None:
            continue
        if band == "action":
            action += 1
            blocking += 1
        elif band == "attention":
            attention += 1
        else:
            observe += 1
    return {
        "action_required": action,
        "needs_attention": attention,
        "watch_list": observe,
        "blocking": blocking,
        "total": action + attention + observe,
    }


def scan_aftersale_breakdown(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """按售后/异常具体状态聚合 → [{label, count}]，与前端 resolveAftersaleStatus 同口径。

    每行按业务优先级归一为单一类别（退款 > 退换货 > 订单异常），互斥计数。
  """
    labels = ("退款申请", "退换货", "订单异常")
    counts: dict[str, int] = {k: 0 for k in labels}
    for row in rows:
        if _num(row.get("apply_refund_stat")) != 0:
            counts["退款申请"] += 1
        elif _num(row.get("rtn_stat")) != 0:
            counts["退换货"] += 1
        elif _num(row.get("abn_type_cd")) != 0:
            counts["订单异常"] += 1
    return [
        {"label": label, "count": counts[label]}
        for label in labels
        if counts[label] > 0
    ]


def scan_exception_reasons(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """聚合异常原因 → [{reason, count}]，按数量降序。"""
    counts: dict[str, int] = {}
    for row in rows:
        result = classify_exception_reason(row)
        if result is None:
            continue
        reason = result[1]
        counts[reason] = counts.get(reason, 0) + 1
    return [
        {"reason": reason, "count": count}
        for reason, count in sorted(
            counts.items(), key=lambda kv: kv[1], reverse=True
        )
    ]

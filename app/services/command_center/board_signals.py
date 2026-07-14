"""指挥中心看板信号 — 与 Web orderToExceptionSignal + order-signal-board 同口径。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.services.orders.exception_rules import (
    EPS,
    resolve_customer_paid,
    resolve_health,
    resolve_margin_threshold_pct,
)
from app.services.orders.procurement_scope import (
    allows_finance_signal,
    is_in_procurement_scope,
)
from app.services.orders.purchase_cost import resolve_purchase_cost_basis
from app.services.orders.queue_filters import resolve_order_queue

BOARD_SIGNAL_KEYS = frozenset(
    {
        "PAY_AMOUNT_GAP",
        "SKU_MISMATCH",
        "MOQ_VIOLATION",
        "SHIP_OVERDUE",
        "STOCKOUT",
        "SHIP_NO_TRACKING",
    }
)

_DEFER_PREP_STATS = frozenset({0, 23, 54})
_ORDERED_STAT = 22
_SHIP_OVERDUE_HOURS = 48


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _row_queue(row: dict[str, Any]) -> str:
    return resolve_order_queue(row) or "pending_procurement"


def _product_for_row(
    row: dict[str, Any], products: list[dict[str, Any]]
) -> Optional[dict[str, Any]]:
    line = str(row.get("ord_line_no") or "")
    tid = row.get("tangbuy_product_id") or row.get("item_id")
    for product in products:
        if product.get("replaced_by_product_id"):
            continue
        if line and line in (product.get("linked_ord_lines") or []):
            return product
    if tid:
        for product in products:
            if product.get("replaced_by_product_id"):
                continue
            if str(product.get("tangbuy_product_id") or "") == str(tid):
                return product
    return None


def _detect_stockout(
    row: dict[str, Any], products: list[dict[str, Any]]
) -> Optional[dict[str, Any]]:
    if _row_queue(row) != "pending_procurement":
        return None
    product = _product_for_row(row, products)
    if not product:
        return None
    inventory = product.get("inventory_total")
    out_of_stock = product.get("stock_status") == "out" or (
        inventory is not None and int(inventory or 0) <= 0
    )
    if not out_of_stock:
        return None
    return {
        "signal_type": "STOCKOUT",
        "urgency": "immediate",
        "is_blocking": True,
    }


def _detect_ship_overdue(row: dict[str, Any]) -> Optional[dict[str, Any]]:
    if _row_queue(row) != "ordered":
        return None
    if row.get("ord_line_stat") not in (_ORDERED_STAT, str(_ORDERED_STAT)):
        return None
    # 已有国内段运单 / 签收时间，不算超时未发货
    if str(row.get("exprs_no") or row.get("pkg_exprs_no") or "").strip():
        return None
    if row.get("sign_time") or int(row.get("is_exprs_dlyd") or 0) == 1:
        return None
    ref = _parse_iso(row.get("pur_time")) or _parse_iso(row.get("pay_time"))
    if not ref:
        return None
    hours = (datetime.now(timezone.utc) - ref).total_seconds() / 3600
    if hours < _SHIP_OVERDUE_HOURS:
        return None
    return {
        "signal_type": "SHIP_OVERDUE",
        "urgency": "today",
        "is_blocking": True,
    }


def _detect_finance_signal(row: dict[str, Any]) -> Optional[dict[str, Any]]:
    queue = _row_queue(row)
    cost = resolve_purchase_cost_basis(row)
    purchase_payable = float(cost.get("purchase_payable") or 0)
    customer_paid = resolve_customer_paid(row)
    margin = round(customer_paid - purchase_payable, 2)
    margin_pct = (margin / customer_paid * 100) if customer_paid > 0 else 0.0

    prep_stat = row.get("ord_line_stat")
    try:
        prep_stat_int = int(prep_stat) if prep_stat is not None else None
    except (TypeError, ValueError):
        prep_stat_int = None
    defer = queue == "pending_procurement" and prep_stat_int in _DEFER_PREP_STATS

    suggested_gap = float(cost.get("suggested_price_gap") or 0)
    cost_basis = str(cost.get("purchase_cost_basis") or "")
    if suggested_gap > EPS and queue == "pending_procurement" and cost_basis != "platform":
        if allows_finance_signal(queue, "SUGGESTED_PRICE_GAP"):
            return {
                "signal_type": "SUGGESTED_PRICE_GAP",
                "urgency": "attention",
                "is_blocking": False,
            }

    if customer_paid + EPS < purchase_payable:
        if not allows_finance_signal(queue, "PAY_AMOUNT_GAP"):
            return None
        if defer and cost_basis != "platform":
            return None
        return {
            "signal_type": "PAY_AMOUNT_GAP",
            "urgency": "immediate",
            "is_blocking": True,
        }

    if abs(margin) < EPS:
        if not allows_finance_signal(queue, "ZERO_MARGIN"):
            return None
        if defer and cost_basis != "platform":
            return None
        return {
            "signal_type": "ZERO_MARGIN",
            "urgency": "immediate",
            "is_blocking": True,
        }

    if margin_pct + EPS < resolve_margin_threshold_pct():
        if not allows_finance_signal(queue, "LOW_MARGIN"):
            return None
        return {
            "signal_type": "LOW_MARGIN",
            "urgency": "attention",
            "is_blocking": False,
        }

    return None


def _detect_sku_mismatch(row: dict[str, Any]) -> Optional[dict[str, Any]]:
    if _row_queue(row) != "pending_procurement":
        return None
    if not row.get("sku_mismatch"):
        return None
    return {
        "signal_type": "SKU_MISMATCH",
        "urgency": "today",
        "is_blocking": True,
    }


def _has_active_reverse_issue(row: dict[str, Any]) -> bool:
    try:
        if float(row.get("apply_refund_stat") or 0) != 0:
            return True
        if float(row.get("rtn_stat") or 0) != 0:
            return True
    except (TypeError, ValueError):
        pass
    if resolve_health(row) == "needs_action":
        return True
    return False


def _is_exception_order(row: dict[str, Any], products: list[dict[str, Any]]) -> bool:
    if not is_in_procurement_scope(row):
        return False

    queue = _row_queue(row)
    blockers = row.get("pipeline_blockers") or []
    if isinstance(blockers, list) and blockers:
        return True

    if _detect_sku_mismatch(row):
        return True

    finance = _detect_finance_signal(row)
    if finance and finance.get("is_blocking"):
        return True

    if row.get("note_block_procurement") and queue == "pending_procurement":
        return True

    health = resolve_health(row)
    if health == "needs_action" and queue != "reverse":
        return True
    if queue == "exception":
        return True

    if queue == "reverse" and _has_active_reverse_issue(row):
        return True

    return False


def row_to_board_signal(
    row: dict[str, Any],
    products: list[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """单行最多一个看板信号，优先级对齐 Web orderToExceptionSignal。"""
    if not is_in_procurement_scope(row):
        return None

    stockout = _detect_stockout(row, products)
    if stockout:
        return stockout

    ship = _detect_ship_overdue(row)
    if ship:
        return ship

    if not _is_exception_order(row, products):
        return None

    finance = _detect_finance_signal(row)
    if finance:
        return finance

    # 规格备注优先于纯规格比对（与 Web orderToExceptionSignal 一致）
    if row.get("note_block_procurement") and _row_queue(row) == "pending_procurement":
        if str(row.get("note_signal_type") or "") == "SKU_MISMATCH":
            return {
                "signal_type": "SKU_MISMATCH",
                "urgency": "today",
                "is_blocking": True,
            }

    sku = _detect_sku_mismatch(row)
    if sku:
        return sku

    return None


def _urgency_band(urgency: str) -> str:
    if urgency in ("immediate", "today"):
        return "action"
    if urgency == "attention":
        return "attention"
    return "observe"


def aggregate_board_signal_stats(
    rows: list[dict[str, Any]],
    products: list[dict[str, Any]],
) -> dict[str, Any]:
    """聚合 6 类严重看板信号，分档计数。"""
    counts_all: dict[str, int] = {k: 0 for k in BOARD_SIGNAL_KEYS}
    counts_action: dict[str, int] = {k: 0 for k in BOARD_SIGNAL_KEYS}
    bands = {"all": 0, "action": 0, "attention": 0, "observe": 0}

    for row in rows:
        signal = row_to_board_signal(row, products)
        if not signal:
            continue
        signal_type = str(signal.get("signal_type") or "")
        if signal_type not in BOARD_SIGNAL_KEYS:
            continue
        urgency = str(signal.get("urgency") or "today")
        band = _urgency_band(urgency)

        counts_all[signal_type] = counts_all.get(signal_type, 0) + 1
        bands["all"] += 1
        bands[band] += 1
        if band == "action":
            counts_action[signal_type] = counts_action.get(signal_type, 0) + 1

    return {
        "board_signal_counts": counts_all,
        "board_signal_counts_action": counts_action,
        "board_band_counts": bands,
    }

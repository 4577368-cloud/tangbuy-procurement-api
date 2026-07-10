"""采购履约范围与指挥中心统计口径。"""

from __future__ import annotations

from typing import Any, Literal

TERMINAL_ORD_STAT = frozenset({4, 7, 11, 24, 34})
CANCELLED_DS_ORD_STAT = frozenset({9, 10, 11})
CANCELLED_ORD_LINE_STAT = frozenset({11, 24})

FinanceSignalMode = Literal["full", "pay_gap_only", "none"]


def is_in_procurement_scope(row: dict[str, Any]) -> bool:
    if not row.get("pay_time"):
        return False
    if row.get("is_ord_del") == 1 or row.get("is_line_del") == 1:
        return False
    ord_stat = row.get("ord_stat")
    if ord_stat is not None and int(ord_stat) in TERMINAL_ORD_STAT:
        return False
    ds_ord_stat = row.get("ds_ord_stat")
    if ds_ord_stat is not None and int(ds_ord_stat) in CANCELLED_DS_ORD_STAT:
        return False
    ord_line_stat = row.get("ord_line_stat")
    if ord_line_stat is not None and int(ord_line_stat) in CANCELLED_ORD_LINE_STAT:
        return False
    return True


def finance_signal_mode_for_queue(queue: str) -> FinanceSignalMode:
    if queue in ("pending_procurement", "pending_payment"):
        return "full"
    if queue in ("ordered", "reverse"):
        return "pay_gap_only"
    return "none"


def allows_finance_reason(queue: str, reason: str) -> bool:
    """按中文异常原因过滤（exception_rules 内部使用）。"""
    mode = finance_signal_mode_for_queue(queue)
    if mode == "full":
        return True
    if mode == "pay_gap_only":
        return reason in ("负毛利", "成本倒挂", "利润为负")
    return False


def allows_finance_signal(queue: str, signal_type: str) -> bool:
    """按信号枚举过滤（与前端 signal_type 对齐）。"""
    mode = finance_signal_mode_for_queue(queue)
    if mode == "full":
        return True
    if mode == "pay_gap_only":
        return signal_type == "PAY_AMOUNT_GAP"
    return False

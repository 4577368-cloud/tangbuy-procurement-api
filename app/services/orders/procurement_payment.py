"""二期：1688 待支付付款编排（Admin 支付 API 待接入）。"""

from __future__ import annotations

from typing import Any, Optional

from app.services.orders.purchase_cost import resolve_purchase_cost_basis
from app.services.orders.procurement_release import _num, _resolve_customer_paid


class ProcurementPaymentError(Exception):
    def __init__(self, message: str, *, code: str = "payment_failed") -> None:
        super().__init__(message)
        self.code = code


def evaluate_payment_gap(row: dict[str, Any]) -> dict[str, Any]:
    """待支付阶段：检测用户实付与采购应付差额（补款卡点）。"""
    cost = resolve_purchase_cost_basis(row)
    payable = _num(cost.get("purchase_payable"))
    paid = _resolve_customer_paid(row)
    gap = round(payable - paid, 2)
    needs_topup = paid + 0.02 < payable
    return {
        "needs_topup": needs_topup,
        "customer_paid": paid,
        "purchase_payable": payable,
        "gap": gap if needs_topup else 0.0,
    }


def submit_1688_payment(
    ord_line_no: str,
    *,
    operator: Optional[str] = None,
) -> dict[str, Any]:
    """调用 Admin 1688 付款接口（二期占位，待接真实 API）。"""
    raise ProcurementPaymentError(
        "1688 付款 API 尚未接入，请在 Admin 完成支付后由同步回写 stat=22",
        code="not_implemented",
    )

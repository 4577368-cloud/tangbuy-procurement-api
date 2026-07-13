"""1688 实单优先的采购成本口径。"""

import pytest

from app.services.orders.purchase_cost import resolve_purchase_cost_basis


def test_platform_basis_overrides_suggested_price():
    row = {
        "ord_cnt": 2,
        "pur_prc": 50.0,
        "post_fee": 10.0,
        "pur_sugg_prc": 40.0,
        "pur_sugg_post_prc": 8.0,
        "plt_total_amt": 9.5,
        "plt_goods_amt": 5.7,
        "plt_post_fee": 3.9,
        "plt_line_detail_amt": 5.6,
        "plt_line_unit_prc": 2.8,
        "plt_line_qty": 2,
    }
    cost = resolve_purchase_cost_basis(row)
    assert cost["purchase_cost_basis"] == "platform"
    assert cost["purchase_payable"] == pytest.approx(9.5, abs=0.1)
    assert cost["estimated_payable"] > cost["purchase_payable"]


def test_listing_when_no_platform_data():
    row = {
        "ord_cnt": 1,
        "pur_prc": 100.0,
        "post_fee": 10.0,
        "pur_sugg_prc": 90.0,
        "pur_sugg_post_prc": 10.0,
    }
    cost = resolve_purchase_cost_basis(row)
    assert cost["purchase_cost_basis"] == "suggested"
    assert cost["purchase_payable"] == 100.0

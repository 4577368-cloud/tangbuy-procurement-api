"""采购成本口径（建议价 / 结算价 / 挂价）。"""

from __future__ import annotations

from typing import Any, Literal

EPS = 0.02
PurchaseCostBasis = Literal["listing", "suggested", "settlement"]


def _num(v: Any, fallback: float = 0.0) -> float:
    try:
        n = float(v)
        return n if n == n else fallback
    except (TypeError, ValueError):
        return fallback


def resolve_purchase_cost_basis(row: dict[str, Any]) -> dict[str, Any]:
    qty = max(1.0, _num(row.get("ord_cnt"), 1))
    listing_product = _num(row.get("pur_amt"))
    if listing_product <= 0:
        listing_product = _num(row.get("pur_prc")) * qty
    listing_shipping = _num(row.get("post_fee"))
    listing_payable = round(listing_product + listing_shipping, 2)
    discount = round(_num(row.get("pur_comm_disc_amt")), 2)

    suggested_unit = _num(row.get("pur_sugg_prc"))
    settlement_unit = _num(row.get("settlement_real_amt"))

    if suggested_unit > 0:
        product_amt = round(max(0.0, suggested_unit * qty - discount), 2)
        shipping_amt = round(_num(row.get("pur_sugg_post_prc"), listing_shipping), 2)
        payable = round(product_amt + shipping_amt, 2)
        return {
            "purchase_cost_basis": "suggested",
            "pur_product_amt": product_amt,
            "pur_shipping_amt": shipping_amt,
            "purchase_payable": payable,
            "listing_payable": listing_payable,
            "suggested_price_gap": round(max(0.0, payable - listing_payable), 2),
        }

    if settlement_unit > 0:
        product_amt = round(max(0.0, settlement_unit * qty - discount), 2)
        payable = round(product_amt + listing_shipping, 2)
        return {
            "purchase_cost_basis": "settlement",
            "pur_product_amt": product_amt,
            "pur_shipping_amt": listing_shipping,
            "purchase_payable": payable,
            "listing_payable": listing_payable,
            "suggested_price_gap": 0.0,
        }

    product_amt = round(max(0.0, listing_product - discount), 2) if discount else listing_product
    payable = round(product_amt + listing_shipping, 2)
    return {
        "purchase_cost_basis": "listing",
        "pur_product_amt": product_amt,
        "pur_shipping_amt": listing_shipping,
        "purchase_payable": payable,
        "listing_payable": listing_payable,
        "suggested_price_gap": 0.0,
    }


def enrich_row_purchase_cost_fields(row: dict[str, Any]) -> dict[str, Any]:
    cost = resolve_purchase_cost_basis(row)
    row.update(cost)
    return row

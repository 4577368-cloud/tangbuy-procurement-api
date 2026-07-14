"""采购成本口径（1688 实单 / 建议价 / 结算价 / 挂价）。"""

from __future__ import annotations

from typing import Any, Literal, Optional

EPS = 0.02
PurchaseCostBasis = Literal["listing", "suggested", "settlement", "platform"]


def resolve_margin_threshold_pct() -> float:
    """与配置中心 gross_margin_threshold 一致（默认 15%）。"""
    from app.config.business_config import normalize_business_config
    from app.config.store import get_business_config

    cfg = normalize_business_config(get_business_config())
    return float(cfg.get("gross_margin_threshold") or 15)


def _num(v: Any, fallback: float = 0.0) -> float:
    try:
        n = float(v)
        return n if n == n else fallback
    except (TypeError, ValueError):
        return fallback


def _resolve_platform_payable(row: dict[str, Any]) -> Optional[dict[str, float]]:
    """有订单回传时，按子单行拆分 1688 实付（商品 + 分摊运费）。"""
    plt_total = _num(row.get("plt_total_amt"))
    if plt_total <= 0:
        return None

    plt_goods = _num(row.get("plt_goods_amt"))
    plt_post = _num(row.get("plt_post_fee"))
    line_detail = _num(row.get("plt_line_detail_amt"))
    line_unit = _num(row.get("plt_line_unit_prc"))
    line_qty = max(1.0, _num(row.get("plt_line_qty")) or _num(row.get("ord_cnt"), 1))

    if line_detail > 0:
        product_amt = line_detail
    elif line_unit > 0:
        product_amt = round(line_unit * line_qty, 2)
    elif plt_goods > 0:
        product_amt = plt_goods
    else:
        product_amt = round(max(0.0, plt_total - plt_post), 2)

    if plt_post > 0 and plt_goods > EPS and product_amt > 0:
        if abs(product_amt - plt_goods) < EPS:
            shipping_amt = plt_post
        elif product_amt < plt_goods - EPS:
            shipping_amt = round(plt_post * (product_amt / plt_goods), 2)
        else:
            shipping_amt = plt_post
    elif plt_post > 0:
        shipping_amt = plt_post
    else:
        shipping_amt = round(max(0.0, plt_total - product_amt), 2)

    payable = round(product_amt + shipping_amt, 2)
    if payable <= 0:
        payable = plt_total
    return {
        "pur_product_amt": product_amt,
        "pur_shipping_amt": shipping_amt,
        "purchase_payable": payable,
    }


def _settlement_as_unit_price(
    settlement: float,
    *,
    pur_prc: float,
    listing_product: float,
    qty: float,
) -> bool:
    """Admin settlementRealAmount：现网多为行金额（≈pur_amt），宽表注释写成了单价。

    仅当其明显接近单价 pur_prc、且明显不像行合计时，才按单价 × 数量。
    """
    if settlement <= 0:
        return False
    if qty <= 1:
        return True
    if listing_product > 0 and abs(settlement - listing_product) <= max(0.05, listing_product * 0.02):
        return False
    if pur_prc > 0 and abs(settlement - pur_prc) <= max(0.05, pur_prc * 0.02):
        return True
    if listing_product > 0 and settlement >= listing_product * 0.5:
        return False
    if pur_prc > 0 and settlement <= pur_prc * 2.5:
        return True
    return False


def _resolve_estimated_cost_basis(
    row: dict[str, Any],
    listing_product: float,
    listing_shipping: float,
    listing_payable: float,
    discount: float,
) -> dict[str, Any]:
    qty = max(1.0, _num(row.get("ord_cnt"), 1))
    suggested_unit = _num(row.get("pur_sugg_prc"))
    settlement_raw = _num(row.get("settlement_real_amt"))
    pur_prc = _num(row.get("pur_prc"))

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

    if settlement_raw > 0:
        if _settlement_as_unit_price(
            settlement_raw,
            pur_prc=pur_prc,
            listing_product=listing_product,
            qty=qty,
        ):
            product_amt = round(max(0.0, settlement_raw * qty - discount), 2)
        else:
            # 行金额：勿再 × qty（否则会出现「放大数量倍」的假补款）
            product_amt = round(max(0.0, settlement_raw - discount), 2)
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


def resolve_purchase_cost_basis(row: dict[str, Any]) -> dict[str, Any]:
    qty = max(1.0, _num(row.get("ord_cnt"), 1))
    listing_product = _num(row.get("pur_amt"))
    if listing_product <= 0:
        listing_product = _num(row.get("pur_prc")) * qty
    listing_shipping = _num(row.get("post_fee"))
    listing_payable = round(listing_product + listing_shipping, 2)
    discount = round(_num(row.get("pur_comm_disc_amt")), 2)

    platform = _resolve_platform_payable(row)
    if platform:
        estimated_basis = _resolve_estimated_cost_basis(
            row, listing_product, listing_shipping, listing_payable, discount
        )
        return {
            "purchase_cost_basis": "platform",
            "pur_product_amt": platform["pur_product_amt"],
            "pur_shipping_amt": platform["pur_shipping_amt"],
            "purchase_payable": platform["purchase_payable"],
            "listing_payable": listing_payable,
            "suggested_price_gap": estimated_basis.get("suggested_price_gap", 0.0),
            "estimated_payable": estimated_basis.get("purchase_payable", listing_payable),
            "estimated_cost_basis": estimated_basis.get("purchase_cost_basis", "listing"),
        }

    return _resolve_estimated_cost_basis(row, listing_product, listing_shipping, listing_payable, discount)


def enrich_row_purchase_cost_fields(row: dict[str, Any]) -> dict[str, Any]:
    cost = resolve_purchase_cost_basis(row)
    row.update(cost)
    return row

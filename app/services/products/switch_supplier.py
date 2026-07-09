"""换供：用备选 B 完整替换商品 A（可作为后续采购下单货源）。

不以演示裁剪字段：图搜能带回的标题/价/店/图/SKU/类目/库存/起订量等全部写入 B，
并回写子单货源覆盖。
"""

from __future__ import annotations

import sys
from typing import Any, Optional

from app.core.paths import PROJECT_ROOT
from app.services.orders import disposition_store
from app.services.products.service import (
    _build_tier_prices,
    _new_product_id,
    _now_iso,
    map_product_category_by_id,
)
from app.services.products.store import (
    find_by_source_product_id,
    find_product_for_ord_line,
    get_product_by_id,
    load_products,
    save_products,
)

_SCRIPTS = PROJECT_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


class SwitchSupplierError(Exception):
    def __init__(self, message: str, *, code: str = "switch_failed") -> None:
        super().__init__(message)
        self.code = code


def _find_alt(product: dict[str, Any], offer_id: str) -> Optional[dict[str, Any]]:
    for alt in product.get("alternative_suppliers") or []:
        if str(alt.get("offer_id") or "").strip() == offer_id:
            return alt
    return None


def _pick_product_a(
    *,
    product_id: Optional[str],
    ord_line_no: Optional[str],
) -> dict[str, Any]:
    if product_id:
        p = get_product_by_id(product_id)
        if p:
            return p
    if ord_line_no:
        p = find_product_for_ord_line({"ord_line_no": ord_line_no})
        if p:
            return p
    raise SwitchSupplierError("未找到原商品", code="not_found")


def _as_int(raw: Any) -> Optional[int]:
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _as_float(raw: Any) -> Optional[float]:
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _hydrate_alt_from_image_search(
    alt: dict[str, Any],
    offer_id: str,
    *,
    image_url: str,
    search_query: str = "",
) -> dict[str, Any]:
    """用同源图搜按 offer_id 补齐缺字段（标题必补，其它能补则补）。"""
    merged = dict(alt)
    img = (image_url or "").strip()
    if not img:
        return merged
    try:
        import newton_cli

        result = newton_cli.search_image(img, limit=20, query=(search_query or None) or None)
        if not result.get("success"):
            return merged
        for cand in (result.get("data") or {}).get("similar_products") or []:
            if not isinstance(cand, dict):
                continue
            if str(cand.get("product_id") or "").strip() != offer_id:
                continue
            field_map = {
                "title": "title",
                "supplier": "shop_name",
                "detail_url": "detail_url",
                "image_url": "image_url",
                "price": "unit_price",
                "sold_count": "sold_count",
                "yx_index": "yx_index",
                "sku_id": "sku_id",
                "sku_title": "sku_title",
                "cate_id": "cate_id",
                "industry_name": "industry_name",
                "min_order_qty": "min_order_qty",
                "inventory": "inventory",
                "selling_points": "selling_points",
                "service_tags": "service_tags",
                "offer_tags": "offer_tags",
            }
            for src, dst in field_map.items():
                cur = merged.get(dst)
                empty = cur in (None, "", "（无标题）") or (isinstance(cur, str) and not cur.strip())
                val = cand.get(src)
                if empty and val not in (None, ""):
                    merged[dst] = val
            if not merged.get("title"):
                t = str(cand.get("title") or "").strip()
                if t:
                    merged["title"] = t
            break
    except Exception:
        pass
    return merged


def _resolve_alt_payload(
    alt: dict[str, Any],
    offer_id: str,
    *,
    image_url: str = "",
    search_query: str = "",
) -> dict[str, Any]:
    payload = _hydrate_alt_from_image_search(
        alt,
        offer_id,
        image_url=image_url,
        search_query=search_query,
    )
    title = str(payload.get("title") or "").strip()
    if not title or title == "（无标题）":
        try:
            import newton_cli

            fetched = newton_cli.fetch_offer_title(offer_id)
            if fetched:
                payload["title"] = fetched.strip()
        except Exception:
            pass
    return payload


def _stock_status(inventory: Optional[int]) -> str:
    if inventory is None:
        return "unknown"
    if inventory <= 0:
        return "out"
    if inventory < 10:
        return "low"
    return "in_stock"


def _build_product_b_fields(
    alt: dict[str, Any],
    *,
    offer_id: str,
    linked_lines: list[str],
    from_product_id: str,
    existing: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """把图搜备选写成完整商品中心条目字段。"""
    now = _now_iso()
    title = str(alt.get("title") or "").strip()
    if not title or title == "（无标题）":
        if existing:
            title = str(existing.get("product_name") or "").strip()
    if not title:
        title = "（无标题）"

    unit = _as_float(alt.get("unit_price"))
    if unit is None and existing:
        unit = _as_float(existing.get("original_unit_price")) or 0.0
    unit = unit or 0.0

    shipping = _as_float(alt.get("shipping"))
    if shipping is None and existing:
        shipping = existing.get("original_shipping")

    tiers = _build_tier_prices(unit, shipping if isinstance(shipping, (int, float)) else None)
    base = tiers[0]
    sold = _as_int(alt.get("sold_count"))
    if sold is None and existing:
        sold = _as_int(existing.get("sold_count")) or 0
    sold = sold or 0

    min_qty = _as_int(alt.get("min_order_qty"))
    inventory = _as_int(alt.get("inventory"))
    yx = _as_float(alt.get("yx_index"))
    cate = str(alt.get("cate_id") or "").strip() or None
    shop = str(alt.get("shop_name") or "").strip() or (existing or {}).get("shop_name") or "—"
    detail = (
        str(alt.get("detail_url") or "").strip()
        or (existing or {}).get("source_url")
        or f"https://detail.1688.com/offer/{offer_id}.html"
    )
    image = str(alt.get("image_url") or "").strip() or (existing or {}).get("image_url") or ""
    sku_id = str(alt.get("sku_id") or "").strip() or None
    sku_title = str(alt.get("sku_title") or "").strip() or None

    lines = list((existing or {}).get("linked_ord_lines") or [])
    for ln in linked_lines:
        if ln and ln not in lines:
            lines.append(ln)

    product_skus = None
    if sku_id or sku_title:
        product_skus = [
            {
                "sku_id": sku_id or offer_id,
                "inventory": inventory if inventory is not None else 0,
                "price": unit,
                "attributes": (
                    [{"name": "规格", "value": sku_title}] if sku_title else []
                ),
            }
        ]

    supplier_metrics = None
    if yx is not None:
        supplier_metrics = {
            "composite_score": yx,
            "logistics_score": yx,
            "service_score": None,
        }

    has_title = bool(title and title != "（无标题）")
    fields: dict[str, Any] = {
        "source": "order",
        "in_store": bool((existing or {}).get("in_store", False)),
        "linked_ord_lines": lines,
        "platform_category_hint": cate
        or str(alt.get("industry_name") or "").strip()
        or (existing or {}).get("platform_category_hint"),
        "category": (existing or {}).get("category") or "待映射",
        "category_status": "pending" if has_title else "failed",
        "tier_prices": tiers if unit > 0 else (existing or {}).get("tier_prices") or tiers,
        "source_url": detail,
        "sold_count": sold,
        "product_name": title,
        "image_url": image,
        "original_unit_price": unit if unit > 0 else (existing or {}).get("original_unit_price") or 0,
        "original_shipping": shipping
        if shipping is not None
        else (existing or {}).get("original_shipping"),
        "tangbuy_unit_price": base["tangbuy_unit_price"]
        if unit > 0
        else (existing or {}).get("tangbuy_unit_price") or 0,
        "tangbuy_shipping": base["tangbuy_shipping"]
        if shipping is not None
        else (existing or {}).get("tangbuy_shipping"),
        "shipping_source": (existing or {}).get("shipping_source") or "unknown",
        "shop_name": shop,
        "source_product_id": offer_id,
        "replaced_from_product_id": from_product_id,
        "switch_supplier_at": now,
        "replaced_by_product_id": None,
        "replaced_by_offer_id": None,
        "min_order_qty": min_qty if min_qty is not None else (existing or {}).get("min_order_qty"),
        "inventory_total": inventory
        if inventory is not None
        else (existing or {}).get("inventory_total"),
        "stock_status": _stock_status(inventory)
        if inventory is not None
        else (existing or {}).get("stock_status") or "unknown",
        "product_skus": product_skus or (existing or {}).get("product_skus"),
        "supplier_metrics": supplier_metrics or (existing or {}).get("supplier_metrics"),
        "detail_source": "newton",
        "enrichment_status": "partial" if has_title else "pending",
        "alt_supplier_scan_status": "idle",
        "alternative_suppliers": [],
    }
    # 新商品清空旧 HS，强制按新标题映射
    if not existing or str((existing or {}).get("source_product_id") or "") != offer_id:
        fields["hs_mapping"] = None
        fields["mapping_record"] = None
        fields["mapping_confidence"] = None
        fields["category"] = "待映射"
        fields["category_status"] = "pending" if has_title else "failed"
    elif has_title and (existing or {}).get("category_status") == "failed":
        fields["category_status"] = "pending"
    return fields


def _upsert_product_b_from_alt(
    items: list[dict[str, Any]],
    alt: dict[str, Any],
    *,
    linked_lines: list[str],
    from_product_id: str,
    source_image_url: str = "",
    source_search_query: str = "",
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    offer_id = str(alt.get("offer_id") or "").strip()
    if not offer_id:
        raise SwitchSupplierError("备选缺少 offer_id")

    hydrated = _resolve_alt_payload(
        alt,
        offer_id,
        image_url=source_image_url,
        search_query=source_search_query,
    )

    existing = next(
        (p for p in items if str(p.get("source_product_id") or "") == offer_id),
        None,
    )
    fields = _build_product_b_fields(
        hydrated,
        offer_id=offer_id,
        linked_lines=linked_lines,
        from_product_id=from_product_id,
        existing=existing,
    )

    if existing:
        idx = next(
            i
            for i, p in enumerate(items)
            if p.get("tangbuy_product_id") == existing.get("tangbuy_product_id")
        )
        merged = {**existing, **fields}
        items[idx] = merged
        return merged, items

    record = {
        "tangbuy_product_id": _new_product_id(),
        "created_at": _now_iso(),
        **fields,
    }
    items.insert(0, record)
    return record, items


def switch_supplier(
    *,
    product_id: Optional[str] = None,
    ord_line_no: Optional[str] = None,
    offer_id: str,
    operator: Optional[str] = None,
    signal_type: Optional[str] = None,
    note: Optional[str] = None,
) -> dict[str, Any]:
    """用备选 offer 完整替换原商品：B 作为订单新货源入库。"""
    offer = (offer_id or "").strip()
    if not offer:
        raise SwitchSupplierError("缺少 offer_id")

    product_a = _pick_product_a(product_id=product_id, ord_line_no=ord_line_no)
    a_id = str(product_a.get("tangbuy_product_id") or "")
    old_offer = str(product_a.get("source_product_id") or "").strip()
    if offer == old_offer:
        raise SwitchSupplierError("备选与当前货源相同，无需替换")

    alt = _find_alt(product_a, offer)
    if not alt:
        existing_b = find_by_source_product_id(offer)
        if not existing_b:
            raise SwitchSupplierError("备选不在当前商品候选中", code="alt_not_found")
        alt = {
            "offer_id": offer,
            "title": existing_b.get("product_name"),
            "shop_name": existing_b.get("shop_name"),
            "detail_url": existing_b.get("source_url"),
            "image_url": existing_b.get("image_url"),
            "unit_price": existing_b.get("original_unit_price"),
            "shipping": existing_b.get("original_shipping"),
            "sold_count": existing_b.get("sold_count"),
            "yx_index": (existing_b.get("supplier_metrics") or {}).get("composite_score"),
            "min_order_qty": existing_b.get("min_order_qty"),
            "inventory": existing_b.get("inventory_total"),
            "cate_id": existing_b.get("platform_category_hint"),
            "sku_id": ((existing_b.get("product_skus") or [{}])[0] or {}).get("sku_id"),
        }

    linked = list(product_a.get("linked_ord_lines") or [])
    if ord_line_no and ord_line_no not in linked:
        linked.append(ord_line_no)
    if not linked and ord_line_no:
        linked = [ord_line_no]
    if not linked:
        raise SwitchSupplierError("原商品无关联子单，无法换供到订单")

    items = load_products()
    a_idx = next(
        (i for i, p in enumerate(items) if p.get("tangbuy_product_id") == a_id),
        None,
    )
    if a_idx is None:
        raise SwitchSupplierError("原商品不存在", code="not_found")

    product_b, items = _upsert_product_b_from_alt(
        items,
        alt,
        linked_lines=linked,
        from_product_id=a_id,
        source_image_url=str(product_a.get("image_url") or ""),
        source_search_query=str(product_a.get("alt_search_query") or ""),
    )
    b_id = str(product_b.get("tangbuy_product_id") or "")

    a_idx = next(i for i, p in enumerate(items) if p.get("tangbuy_product_id") == a_id)
    items[a_idx] = {
        **items[a_idx],
        "linked_ord_lines": [],
        "replaced_by_product_id": b_id,
        "replaced_by_offer_id": offer,
        "supplier_replaced_at": _now_iso(),
        "in_store": False,
    }

    b_idx = next(i for i, p in enumerate(items) if p.get("tangbuy_product_id") == b_id)
    items[b_idx] = {
        **items[b_idx],
        "replaced_by_product_id": None,
        "replaced_by_offer_id": None,
    }
    save_products(items)

    now = _now_iso()
    sku_id = None
    skus = product_b.get("product_skus") or []
    if skus and isinstance(skus[0], dict):
        sku_id = skus[0].get("sku_id")

    for line in linked:
        disposition_store.merge_override(
            line,
            {
                "splr_item_id": offer,
                "item_url": product_b.get("source_url"),
                "item_img": product_b.get("image_url"),
                "item_nm": product_b.get("product_name"),
                "splr_shop_nm": product_b.get("shop_name"),
                "pur_prc": product_b.get("original_unit_price"),
                "sku_id": sku_id,
                "min_order_qty": product_b.get("min_order_qty"),
                "tangbuy_product_id": b_id,
                "supplier_switched_at": now,
                "supplier_switched_from": old_offer,
                "supplier_switched_from_product_id": a_id,
            },
        )
        disposition_store.append_audit(
            {
                "ord_line_no": line,
                "action_key": "change_seller",
                "action_label": "换供",
                "signal_type": signal_type,
                "operator": operator,
                "override_reason": note,
                "from_product_id": a_id,
                "to_product_id": b_id,
                "from_offer_id": old_offer,
                "to_offer_id": offer,
                "to_product_name": product_b.get("product_name"),
                "to_unit_price": product_b.get("original_unit_price"),
                "at": now,
            }
        )

    # 同步：类目映射（有标题必须跑）；异步：继续补全
    to_product = get_product_by_id(b_id)
    name = str((to_product or {}).get("product_name") or "").strip()
    if to_product and name and name != "（无标题）":
        try:
            mapped = map_product_category_by_id(b_id)
            if mapped:
                to_product = mapped
        except Exception:
            pass
    try:
        from app.services.products.product_jobs import schedule_product_pipeline

        schedule_product_pipeline(b_id)
    except Exception:
        pass

    return {
        "ok": True,
        "from_product": get_product_by_id(a_id),
        "to_product": get_product_by_id(b_id) or to_product,
        "linked_ord_lines": linked,
        "from_offer_id": old_offer,
        "to_offer_id": offer,
    }

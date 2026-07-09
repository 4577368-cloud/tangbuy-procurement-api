"""从真实订单子单 upsert 商品池，并触发品类映射。"""

from __future__ import annotations

from typing import Any

from app.config.business_config import get_price_markup
from app.config.store import get_business_config
from app.services.orders import service as order_service
from app.services.products.service import _build_tier_prices, _new_product_id, _now_iso, map_product_category_by_id
from app.services.products.store import (
    find_product_for_ord_line,
    find_reusable_hs_mapping,
    load_products,
    save_products,
)


def _num(v: Any, fallback: float = 0.0) -> float:
    try:
        n = float(v)
        return n if n == n else fallback  # NaN guard
    except (TypeError, ValueError):
        return fallback


def _merge_ord_line(product: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    ord_line = str(row.get("ord_line_no") or "").strip()
    lines = list(product.get("linked_ord_lines") or [])
    if ord_line and ord_line not in lines:
        lines.append(ord_line)

    name = str(row.get("item_nm") or row.get("item_nm_cn") or "").strip()
    image = str(row.get("item_img") or "").strip()
    url = str(row.get("item_url") or "").strip()
    shop = str(row.get("splr_shop_nm") or "").strip()
    platform_cat = str(row.get("lvl1_ctgy_nm") or "").strip()
    unit = _num(row.get("pur_prc"))
    post_raw = row.get("post_fee")
    shipping = None if post_raw is None else _num(post_raw)

    merged: dict[str, Any] = {
        **product,
        "linked_ord_lines": lines,
        "source": product.get("source") or "order",
    }
    if platform_cat:
        merged["platform_category_hint"] = platform_cat

    if name and (not merged.get("product_name") or merged.get("product_name") == "（无标题）"):
        merged["product_name"] = name
    if image and not merged.get("image_url"):
        merged["image_url"] = image
    if url and not merged.get("source_url"):
        merged["source_url"] = url
    if shop and (not merged.get("shop_name") or merged.get("shop_name") == "—"):
        merged["shop_name"] = shop
    splr = str(row.get("splr_item_id") or "").strip()
    if splr and not merged.get("source_product_id"):
        merged["source_product_id"] = splr

    markup = get_price_markup()
    if unit > 0 and not merged.get("original_unit_price"):
        merged["original_unit_price"] = unit
        merged["tangbuy_unit_price"] = round(unit * markup, 2)
    if shipping is not None and merged.get("original_shipping") is None:
        merged["original_shipping"] = shipping
        merged["tangbuy_shipping"] = round(shipping * markup, 2)

    return merged


def _new_product_from_ord_line(row: dict[str, Any], *, new_id_fn) -> dict[str, Any]:
    item_id = str(row.get("item_id") or "").strip()
    splr_id = str(row.get("splr_item_id") or "").strip()
    tangbuy_id = item_id or new_id_fn()
    unit = _num(row.get("pur_prc"))
    post_raw = row.get("post_fee")
    shipping = None if post_raw is None else _num(post_raw)
    tiers = _build_tier_prices(unit, shipping)
    base = tiers[0]
    platform_cat = str(row.get("lvl1_ctgy_nm") or "").strip()
    ord_line = str(row.get("ord_line_no") or "").strip()
    name = str(row.get("item_nm") or row.get("item_nm_cn") or "").strip() or "（无标题）"

    return {
        "tangbuy_product_id": tangbuy_id,
        "source": "order",
        "in_store": False,
        "linked_ord_lines": [ord_line] if ord_line else [],
        "platform_category_hint": platform_cat or None,
        "category": "待映射",
        "category_status": "pending",
        "tier_prices": tiers,
        "created_at": _now_iso(),
        "source_url": str(row.get("item_url") or ""),
        "sold_count": 0,
        "product_name": name,
        "image_url": str(row.get("item_img") or ""),
        "original_unit_price": unit,
        "original_shipping": shipping,
        "tangbuy_unit_price": base["tangbuy_unit_price"],
        "tangbuy_shipping": base["tangbuy_shipping"],
        "shipping_source": "unknown",
        "shop_name": str(row.get("splr_shop_nm") or "") or "—",
        "source_product_id": splr_id,
    }


def upsert_product_from_ord_line(row: dict[str, Any], *, new_id_fn) -> tuple[dict[str, Any], bool]:
    """返回 (商品, 是否新建)。"""
    ord_line = str(row.get("ord_line_no") or "").strip()
    if not ord_line:
        raise ValueError("缺少 ord_line_no")

    items = load_products()
    existing = find_product_for_ord_line(row)
    if existing:
        idx = next(
            i for i, p in enumerate(items) if p.get("tangbuy_product_id") == existing.get("tangbuy_product_id")
        )
        updated = _merge_ord_line(items[idx], row)
        items[idx] = updated
        save_products(items)
        return updated, False

    created = _new_product_from_ord_line(row, new_id_fn=new_id_fn)
    items.insert(0, created)
    save_products(items)
    return created, True


def sync_products_from_orders(
    *,
    queue: str = "pending_procurement",
    page: int = 1,
    page_size: int = 200,
    auto_map: bool = True,
) -> dict[str, Any]:
    order_res = order_service.list_ord_lines(queue=queue, page=page, page_size=page_size)
    lines = order_res.get("items") or []
    if order_res.get("error"):
        return {
            "ok": False,
            "error": order_res["error"],
            "stats": {"order_lines_scanned": 0},
            "products": load_products(),
        }

    cfg = get_business_config()
    auto_map_enabled = auto_map and bool(cfg.get("rules", {}).get("auto_category_mapping", True))

    created = 0
    updated = 0
    mapped = 0
    mapped_reused = 0
    map_failed = 0
    map_skipped = 0
    seen_for_map: set[str] = set()

    for row in lines:
        if not isinstance(row, dict):
            continue
        try:
            product, is_new = upsert_product_from_ord_line(row, new_id_fn=_new_product_id)
        except ValueError:
            continue
        if is_new:
            created += 1
        else:
            updated += 1

        if is_new:
            from app.services.products.product_jobs import schedule_product_pipeline

            schedule_product_pipeline(str(product.get("tangbuy_product_id") or ""))

        if not auto_map_enabled:
            map_skipped += 1
            continue
        if product.get("source") != "order":
            map_skipped += 1
            continue

        pid = str(product.get("tangbuy_product_id") or "")
        if not pid:
            continue

        # 同一批订单里同 SKU 只跑一次 Agent，后续子单走复用
        sku_key = str(product.get("source_product_id") or "").strip() or pid
        if sku_key in seen_for_map:
            reused = find_reusable_hs_mapping(product, exclude_id=pid)
            if reused:
                result = map_product_category_by_id(pid)
                if result and result.get("category_status") not in ("failed", "pending", "mapping"):
                    mapped_reused += 1
                else:
                    map_failed += 1
            else:
                map_failed += 1
            continue
        seen_for_map.add(sku_key)

        had_reuse = bool(find_reusable_hs_mapping(product, exclude_id=pid))
        result = map_product_category_by_id(pid)
        if result and result.get("category_status") not in ("failed", "pending", "mapping"):
            if had_reuse:
                mapped_reused += 1
            else:
                mapped += 1
        else:
            map_failed += 1

    return {
        "ok": True,
        "stats": {
            "order_lines_scanned": len(lines),
            "created": created,
            "updated": updated,
            "mapped": mapped,
            "mapped_reused": mapped_reused,
            "map_failed": map_failed,
            "map_skipped": map_skipped,
            "queue": queue,
        },
        "products": load_products(),
    }

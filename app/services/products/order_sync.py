"""从真实订单子单 upsert 商品池，并触发品类映射。"""

from __future__ import annotations

from typing import Any

from app.config.business_config import get_price_markup
from app.config.store import get_business_config
from app.services.category_mapping.admin_sync import prefetch_admin_goods_cache
from app.services.orders import service as order_service
from app.services.products.service import _build_tier_prices, _new_product_id, _now_iso, map_product_category_by_id
from app.services.products.store import (
    find_product_for_ord_line,
    find_reusable_hs_mapping,
    load_products,
    save_products,
    _protect_resolved_mapping,
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
        merged["admin_ord_category_hint"] = platform_cat
        existing_hint = str(merged.get("platform_category_hint") or "").strip()
        if not existing_hint:
            merged["platform_category_hint"] = platform_cat
        elif name:
            from app.services.category_mapping.mapping_quality import hint_conflicts_title

            if not hint_conflicts_title(name, platform_cat):
                merged["platform_category_hint"] = platform_cat

    if name and (not merged.get("product_name") or merged.get("product_name") == "（无标题）"):
        merged["product_name"] = name
    if image and not merged.get("image_url"):
        merged["image_url"] = image
    if url and not merged.get("source_url"):
        merged["source_url"] = url
    tang_url = str(row.get("tang_item_url") or "").strip()
    if tang_url and not merged.get("tang_item_url"):
        merged["tang_item_url"] = tang_url
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

    return _protect_resolved_mapping(product, merged)


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
        "tang_item_url": str(row.get("tang_item_url") or "") or None,
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
    all_pages: bool = False,
    max_pages: int = 100,
) -> dict[str, Any]:
    if all_pages:
        return _sync_products_all_pages(
            queue=queue,
            page_size=page_size,
            auto_map=auto_map,
            max_pages=max_pages,
        )

    return _sync_products_single_page(
        queue=queue,
        page=page,
        page_size=page_size,
        auto_map=auto_map,
    )


def _admin_writeback_stats(result: Optional[dict[str, Any]]) -> tuple[str, bool, bool]:
    """返回 (map_bucket, writeback_ok, writeback_failed)。"""
    if not result or result.get("category_status") in ("failed", "pending", "mapping"):
        return "failed", False, False
    mr = result.get("mapping_record") if isinstance(result.get("mapping_record"), dict) else {}
    if mr.get("match_method") == "admin_existing":
        wb = mr.get("admin_writeback") if isinstance(mr.get("admin_writeback"), dict) else {}
        ok = wb.get("status") == "ok"
        fail = wb.get("status") == "failed"
        return "admin_adopted", ok, fail
    wb = mr.get("admin_writeback") if isinstance(mr.get("admin_writeback"), dict) else {}
    ok = wb.get("status") == "ok"
    fail = wb.get("status") == "failed"
    return "mapped", ok, fail


def _sync_products_single_page(
    *,
    queue: str,
    page: int,
    page_size: int,
    auto_map: bool,
    seen_for_map: set[str] | None = None,
    admin_cache: dict[str, dict[str, Any]] | None = None,
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
    admin_adopted = 0
    admin_writeback_ok = 0
    admin_writeback_failed = 0
    seen = seen_for_map if seen_for_map is not None else set()
    goods_cache = admin_cache if admin_cache is not None else {}

    if auto_map_enabled:
        goods_ids = [
            str(r.get("splr_item_id") or "").strip()
            for r in lines
            if isinstance(r, dict) and str(r.get("splr_item_id") or "").strip()
        ]
        prefetch_admin_goods_cache(goods_ids, goods_cache)

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
        if not is_new:
            map_skipped += 1
            continue

        pid = str(product.get("tangbuy_product_id") or "")
        if not pid:
            continue

        # 同一批订单里同 SKU 只跑一次 Agent，后续子单走复用
        sku_key = str(product.get("source_product_id") or "").strip() or pid
        if sku_key in seen:
            reused = find_reusable_hs_mapping(product, exclude_id=pid)
            if reused:
                result = map_product_category_by_id(pid, ord_row=row, admin_cache=goods_cache)
                bucket, wb_ok, wb_fail = _admin_writeback_stats(result)
                if bucket == "failed":
                    map_failed += 1
                else:
                    mapped_reused += 1
                    if bucket == "admin_adopted":
                        admin_adopted += 1
                    if wb_ok:
                        admin_writeback_ok += 1
                    if wb_fail:
                        admin_writeback_failed += 1
            else:
                map_failed += 1
            continue
        seen.add(sku_key)

        had_reuse = bool(find_reusable_hs_mapping(product, exclude_id=pid))
        result = map_product_category_by_id(pid, ord_row=row, admin_cache=goods_cache)
        bucket, wb_ok, wb_fail = _admin_writeback_stats(result)
        if bucket == "failed":
            map_failed += 1
        else:
            if bucket == "admin_adopted":
                admin_adopted += 1
            elif had_reuse:
                mapped_reused += 1
            else:
                mapped += 1
            if wb_ok:
                admin_writeback_ok += 1
            if wb_fail:
                admin_writeback_failed += 1

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
            "admin_adopted": admin_adopted,
            "admin_writeback_ok": admin_writeback_ok,
            "admin_writeback_failed": admin_writeback_failed,
            "queue": queue,
        },
        "products": load_products(),
    }


def _sync_products_all_pages(
    *,
    queue: str,
    page_size: int,
    auto_map: bool,
    max_pages: int,
) -> dict[str, Any]:
    totals = {
        "order_lines_scanned": 0,
        "created": 0,
        "updated": 0,
        "mapped": 0,
        "mapped_reused": 0,
        "map_failed": 0,
        "map_skipped": 0,
        "admin_adopted": 0,
        "admin_writeback_ok": 0,
        "admin_writeback_failed": 0,
        "pages_scanned": 0,
        "queue": queue,
    }
    seen_for_map: set[str] = set()
    admin_cache: dict[str, dict[str, Any]] = {}
    last_error: str | None = None

    for page in range(1, max(1, max_pages) + 1):
        batch = _sync_products_single_page(
            queue=queue,
            page=page,
            page_size=page_size,
            auto_map=auto_map,
            seen_for_map=seen_for_map,
            admin_cache=admin_cache,
        )
        if not batch.get("ok"):
            last_error = str(batch.get("error") or "sync failed")
            break

        stats = batch.get("stats") or {}
        lines_n = int(stats.get("order_lines_scanned") or 0)
        totals["pages_scanned"] += 1
        totals["order_lines_scanned"] += lines_n
        for key in (
            "created",
            "updated",
            "mapped",
            "mapped_reused",
            "map_failed",
            "map_skipped",
            "admin_adopted",
            "admin_writeback_ok",
            "admin_writeback_failed",
        ):
            totals[key] += int(stats.get(key) or 0)

        if lines_n < page_size:
            break

    ok = last_error is None
    return {
        "ok": ok,
        **({"error": last_error} if last_error else {}),
        "stats": totals,
        "products": load_products(),
    }

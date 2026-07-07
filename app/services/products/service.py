"""商品中心服务（对齐 product-store.ts）。"""

from __future__ import annotations

import random
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from app.integrations.skill_cli import run_category_suggest
from app.services.products.store import (
    confirm_product_mapping,
    find_by_source_product_id,
    load_products,
    save_products,
    update_product,
)

MARKUP = 1.2


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _new_product_id() -> str:
    return str(int(time.time() * 1000)) + str(random.randint(10000, 99999))


def _parse_price(raw: Any) -> float:
    if isinstance(raw, (int, float)):
        return float(raw)
    if not raw:
        return 0.0
    digits = "".join(c for c in str(raw) if c.isdigit() or c == ".")
    try:
        return float(digits) if digits else 0.0
    except ValueError:
        return 0.0


def _apply_shipping_markup(shipping: Optional[float]) -> Optional[float]:
    if shipping is None:
        return None
    return round(shipping * MARKUP, 2)


def _build_tier_prices(unit: float, shipping: Optional[float]) -> list[dict[str, Any]]:
    tangbuy_unit = round(unit * MARKUP, 2)
    tangbuy_ship = _apply_shipping_markup(shipping)
    return [
        {
            "min_qty": 1,
            "max_qty": None,
            "original_unit_price": unit,
            "original_shipping": shipping,
            "tangbuy_unit_price": tangbuy_unit,
            "tangbuy_shipping": tangbuy_ship,
        }
    ]


def list_products() -> list[dict[str, Any]]:
    items = load_products()
    return sorted(items, key=lambda p: p.get("created_at", ""), reverse=True)


def get_product_stats() -> dict[str, int]:
    return {"total": len(load_products())}


def get_product_by_id(pid: str) -> Optional[dict[str, Any]]:
    return next((p for p in load_products() if p.get("tangbuy_product_id") == pid), None)


def add_product_from_find_item(
    product: dict[str, Any],
    *,
    shipping: Optional[float] = None,
    category: Optional[str] = None,
) -> dict[str, Any]:
    source_id = (product.get("product_id") or "").strip()
    if source_id:
        existing = find_by_source_product_id(source_id)
        if existing:
            return {"item": existing, "created": False}

    unit = _parse_price(product.get("price"))
    has_manual = shipping is not None
    original_shipping = shipping if has_manual else None
    tiers = _build_tier_prices(unit, original_shipping)
    base = tiers[0]
    record = {
        "tangbuy_product_id": _new_product_id(),
        "category": (category or "").strip() or "待映射",
        "category_status": "pending",
        "tier_prices": tiers,
        "created_at": _now_iso(),
        "source_url": product.get("detail_url") or "",
        "sold_count": int(product.get("sold_count") or 0),
        "product_name": product.get("title") or "（无标题）",
        "image_url": product.get("image_url") or "",
        "original_unit_price": unit,
        "original_shipping": original_shipping,
        "tangbuy_unit_price": base["tangbuy_unit_price"],
        "tangbuy_shipping": base["tangbuy_shipping"],
        "shipping_source": "manual" if has_manual else "unknown",
        "shop_name": product.get("supplier") or "—",
        "source_product_id": source_id,
    }
    items = load_products()
    items.insert(0, record)
    save_products(items)
    return {"item": record, "created": True}


def set_product_shipping(
    pid: str,
    shipping: Optional[float],
    *,
    to_city: Optional[str] = None,
    source: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    def updater(p: dict[str, Any]) -> dict[str, Any]:
        normalized = None if shipping is None else round(max(0, shipping), 2)
        tiers = _build_tier_prices(float(p.get("original_unit_price") or 0), normalized)
        return {
            **p,
            "tier_prices": tiers,
            "original_shipping": normalized,
            "tangbuy_shipping": _apply_shipping_markup(normalized),
            "shipping_source": source or ("unknown" if normalized is None else "manual"),
            **({"shipping_to_city": to_city} if to_city else {}),
            **({"shipping_quoted_at": _now_iso()} if normalized is not None else {}),
        }

    return update_product(pid, updater)


def map_product_category_by_id(pid: str) -> Optional[dict[str, Any]]:
    product = get_product_by_id(pid)
    if not product:
        return None
    update_product(pid, lambda p: {**p, "category_status": "mapping"})
    result = run_category_suggest(
        product.get("product_name", ""),
        goods_id=product.get("source_product_id"),
        image_url=product.get("image_url"),
    )
    if not result.get("success"):
        return update_product(pid, lambda p: {**p, "category_status": "failed"})
    hs = {
        "category_id": result.get("category_id", 0),
        "category_cn_name": result.get("category_cn_name", ""),
        "category_en_name": result.get("category_en_name", ""),
        "hs_code": result.get("hs_code", ""),
        "declare_cn_name": result.get("declare_cn_name", ""),
        "declare_en_name": result.get("declare_en_name", ""),
        "tariff": result.get("tariff"),
    }
    return update_product(pid, lambda p: confirm_product_mapping(p, hs))


def apply_mapping_record(product: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    hs = record.get("suggested_hs") or {}
    return confirm_product_mapping(product, hs if isinstance(hs, dict) else {})

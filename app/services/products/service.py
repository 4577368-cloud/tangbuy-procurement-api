"""商品中心服务（对齐 product-store.ts）。"""

from __future__ import annotations

import random
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from app.integrations.skill_cli import run_category_suggest, unwrap_category_suggest_result
from app.services.category_mapping.suggest import run_category_mapping_suggest
from app.config.business_config import get_price_markup
from app.services.products.store import (
    confirm_product_mapping,
    find_by_source_product_id,
    find_reusable_hs_mapping,
    get_product_by_id as store_get_product_by_id,
    is_valid_hs_mapping,
    load_products,
    mapping_hint_for_product,
    save_products,
    update_product,
)


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


def _apply_shipping_markup(shipping: Optional[float], markup: Optional[float] = None) -> Optional[float]:
    if shipping is None:
        return None
    rate = get_price_markup() if markup is None else markup
    return round(shipping * rate, 2)


def _build_tier_prices(
    unit: float,
    shipping: Optional[float],
    *,
    markup: Optional[float] = None,
) -> list[dict[str, Any]]:
    rate = get_price_markup() if markup is None else markup
    tangbuy_unit = round(unit * rate, 2)
    tangbuy_ship = _apply_shipping_markup(shipping, rate)
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
    return store_get_product_by_id(pid)


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
            from app.services.products.find_cache import mark_find_cache_promoted

            mark_find_cache_promoted(source_id)
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
        "source": "find",
        "in_store": True,
    }
    items = load_products()
    items.insert(0, record)
    save_products(items)
    from app.services.products.find_cache import mark_find_cache_promoted

    if source_id:
        mark_find_cache_promoted(source_id)
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


def _normalize_category_suggest_result(result: dict[str, Any]) -> dict[str, Any]:
    return unwrap_category_suggest_result(result)


def _hs_from_suggest_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "category_id": payload.get("category_id", 0),
        "category_cn_name": payload.get("category_cn_name", ""),
        "category_en_name": payload.get("category_en_name", ""),
        "hs_code": payload.get("hs_code", ""),
        "declare_cn_name": payload.get("declare_cn_name", ""),
        "declare_en_name": payload.get("declare_en_name", ""),
        "tariff": payload.get("tariff"),
    }


_MAPPING_META_KEYS = (
    "match_method",
    "match_detail",
    "decision",
    "history_hit",
    "agent_confidence",
    "semantic_candidates",
    "signal_scores",
    "matched_keywords",
    "vision_summary",
    "title_image_agreement_keywords",
    "vision_keywords",
    "skip_fusion",
    "match_candidates",
    "source_title_original",
    "checks",
    "auto_resolved",
    "mapped_at",
    "suggested_category_id",
)


def _merge_suggest_into_mapping(mapping: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    next_map = dict(mapping)
    for key in _MAPPING_META_KEYS:
        if key in payload and payload[key] is not None:
            next_map[key] = payload[key]
    hs = _hs_from_suggest_payload(payload)
    if is_valid_hs_mapping(hs):
        next_map["suggested_hs"] = hs
        next_map["suggested_category_path"] = (
            f"{hs.get('category_cn_name', '')} / {hs.get('declare_cn_name', '')} · HS {hs.get('hs_code', '')}"
        )
        next_map["suggested_category_id"] = str(hs.get("category_id", ""))
    if payload.get("confidence") is not None and next_map.get("agent_confidence") is None:
        next_map["agent_confidence"] = payload["confidence"]
    return next_map


def save_mapping_draft(product: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    """写入 Agent 映射结果（含图片理解），不自动确认商品品类。"""
    mapping = _merge_suggest_into_mapping(dict(product.get("mapping_record") or {}), record)
    mapping["review_status"] = record.get("review_status") or "pending"
    product["mapping_record"] = mapping
    conf = record.get("agent_confidence")
    if conf is None:
        conf = record.get("confidence")
    if conf is not None:
        product["mapping_confidence"] = conf
    if record.get("mapped_at"):
        product["mapping_mapped_at"] = record["mapped_at"]
    product["category_status"] = "needs_review"
    hs = mapping.get("suggested_hs")
    if record.get("auto_resolved") and is_valid_hs_mapping(hs):
        product = confirm_product_mapping(product, hs)
        product["mapping_record"] = _merge_suggest_into_mapping(
            dict(product.get("mapping_record") or {}),
            record,
        )
    return product


def save_mapping_draft_from_payload(product: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    now = _now_iso()
    record = dict(payload)
    record.setdefault("mapped_at", now)
    record.setdefault("review_status", "pending")
    decision = str(payload.get("decision") or "")
    conf = float(payload.get("confidence") or 0)
    record["auto_resolved"] = decision in ("semantic_agreement", "history_hit") and conf >= 0.85
    return save_mapping_draft(product, record)


def map_product_category_by_id(
    pid: str,
    *,
    ord_row: Optional[dict[str, Any]] = None,
    admin_cache: Optional[dict[str, dict[str, Any]]] = None,
) -> Optional[dict[str, Any]]:
    product = store_get_product_by_id(pid)
    if not product:
        return None

    if product.get("category_status") == "mapping" and not is_valid_hs_mapping(product.get("hs_mapping")):
        update_product(pid, lambda p: {**p, "category_status": "pending"})
        product = store_get_product_by_id(pid) or product

    reused = find_reusable_hs_mapping(product, exclude_id=pid)
    if reused:
        hs, detail = reused
        return update_product(
            pid,
            lambda p: confirm_product_mapping(
                {
                    **p,
                    "mapping_record": {
                        **(p.get("mapping_record") or {}),
                        "match_detail": detail,
                        "match_method": "local_item_mapped",
                    },
                },
                hs,
            ),
        )

    from app.services.category_mapping.admin_sync import try_adopt_admin_category

    adopted = try_adopt_admin_category(
        product,
        ord_row=ord_row,
        admin_cache=admin_cache,
    )
    if adopted:
        return update_product(pid, lambda _: adopted)

    update_product(pid, lambda p: {**p, "category_status": "mapping"})
    try:
        payload = run_category_mapping_suggest(
            product.get("product_name", ""),
            hint=mapping_hint_for_product(product),
            goods_id=product.get("source_product_id"),
            image_url=product.get("image_url"),
        )
        if not payload.get("success"):
            return update_product(pid, lambda p: {**p, "category_status": "failed"})
        hs = _hs_from_suggest_payload(payload)
        if not is_valid_hs_mapping(hs):
            return update_product(pid, lambda p: {**p, "category_status": "failed"})
        return update_product(pid, lambda p: save_mapping_draft_from_payload(p, payload))
    except Exception:
        return update_product(pid, lambda p: {**p, "category_status": "failed"})


def apply_mapping_record(product: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    hs = record.get("suggested_hs") or record.get("corrected_hs") or {}
    if not isinstance(hs, dict) or not is_valid_hs_mapping(hs):
        raise ValueError("映射结果无效，请重新运行品类映射")

    review = str(record.get("review_status") or "")
    if review == "corrected":
        resolution = "manual_correct"
    elif review in ("confirmed", "pending"):
        resolution = "manual_confirm"
    else:
        resolution = "auto"
    updated = confirm_product_mapping(product, hs, resolution=resolution)
    mapping = _merge_suggest_into_mapping(dict(updated.get("mapping_record") or {}), record)
    category_path = record.get("suggested_category_path")
    if category_path:
        mapping["suggested_category_path"] = category_path
    mapping["suggested_hs"] = hs

    if record.get("review_status"):
        mapping["review_status"] = record["review_status"]
    if record.get("auto_resolved") is False:
        updated["category_status"] = "needs_review"
    if record.get("agent_confidence") is not None:
        updated["mapping_confidence"] = record["agent_confidence"]

    updated["mapping_record"] = mapping
    return updated

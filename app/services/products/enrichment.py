"""商品详情补全（Tangbuy Portal itemGet，A Admin ↔ B Portal）。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from app.config.business_config import get_price_markup
from app.core.config import get_settings
from app.integrations.tangbuy_portal.client import TangbuyPortalError, item_get
from app.services.products.store import get_product_by_id, update_product


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _parse_shop_item_id_from_url(url: str) -> Optional[str]:
    if "tangbuy.cc/product" not in url:
        return None
    try:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        data_source = (qs.get("dataSource") or [""])[0]
        item_id = (qs.get("id") or [""])[0].strip()
        if data_source.upper() == "SHOP" and item_id.isdigit():
            return item_id
    except (ValueError, TypeError):
        return None
    return None


def _build_1688_detail_url(product: dict[str, Any]) -> Optional[str]:
    source_url = str(product.get("source_url") or "").strip()
    if "detail.1688.com" in source_url:
        return source_url.split("?")[0] if source_url else source_url
    offer_id = str(product.get("source_product_id") or "").strip()
    if offer_id.isdigit():
        return f"https://detail.1688.com/offer/{offer_id}.html"
    return None


def tangbuy_shop_item_id(product: dict[str, Any]) -> Optional[str]:
    """Admin A 的 tangGoodsId → 宽表 item_id，入库后为 tangbuy_product_id。"""
    from_url = _parse_shop_item_id_from_url(str(product.get("source_url") or ""))
    if from_url:
        return from_url
    tid = str(product.get("tangbuy_product_id") or "").strip()
    if tid.isdigit() and len(tid) >= 10:
        return tid
    return None


def build_shop_item_get_url(item_id: str) -> str:
    return f"https://shop.tangbuy.cc/product?dataSource=SHOP&id={item_id}"


def should_portal_enrich(product: dict[str, Any]) -> bool:
    """
    线路 1：订单货源且有 Tangbuy item_id → 调 B。
    线路 2：1688 选品加入大店 → 不调 B（仅 scan）。
    """
    if product.get("source") == "find":
        return False
    if product.get("source") == "order":
        return tangbuy_shop_item_id(product) is not None
    return tangbuy_shop_item_id(product) is not None


def resolve_item_get_urls(product: dict[str, Any]) -> list[str]:
    """itemGet 候选 URL（按命中率排序）。"""
    if not should_portal_enrich(product):
        return []

    urls: list[str] = []
    seen: set[str] = set()

    def add(url: Optional[str]) -> None:
        u = (url or "").strip()
        if u and u not in seen:
            seen.add(u)
            urls.append(u)

    tang_url = str(product.get("tang_item_url") or "").strip()
    source_url = str(product.get("source_url") or "").strip()
    for u in (tang_url, source_url):
        if "tangbuy.cc/product" in u:
            add(u)

    # 订单货源：1688 详情页在 Portal 上最稳
    add(_build_1688_detail_url(product))

    item_id = tangbuy_shop_item_id(product)
    if item_id:
        if len(item_id) <= 14:
            add(f"https://www.tangbuy.cc/product?dataSource=PREFERRED&id={item_id}")
        add(build_shop_item_get_url(item_id))

    return urls


def resolve_item_get_url(product: dict[str, Any]) -> Optional[str]:
    urls = resolve_item_get_urls(product)
    return urls[0] if urls else None


def mark_pending_match(product_id: str, *, reason: Optional[str] = None) -> Optional[dict[str, Any]]:
    def merge(p: dict[str, Any]) -> dict[str, Any]:
        next_row: dict[str, Any] = {
            **p,
            "enrichment_status": "pending_match",
            "detail_enriched_at": _now_iso(),
        }
        if reason:
            next_row["enrichment_error"] = reason
        else:
            next_row.pop("enrichment_error", None)
        return next_row

    return update_product(product_id, merge)


def _num(v: Any, fallback: float = 0.0) -> float:
    try:
        n = float(v)
        return n if n == n else fallback
    except (TypeError, ValueError):
        return fallback


def _map_tier_prices(raw_tiers: list[dict[str, Any]], *, markup: float) -> list[dict[str, Any]]:
    tiers: list[dict[str, Any]] = []
    for row in raw_tiers:
        if not isinstance(row, dict):
            continue
        unit = _num(row.get("supplierUnitPrice") or row.get("procurementFinalUnitPrice"))
        if unit <= 0:
            continue
        post_raw = row.get("procurementPostFee")
        shipping = None if post_raw is None else _num(post_raw)
        max_q = row.get("maxQuantity")
        tiers.append(
            {
                "min_qty": int(row.get("minQuantity") or 1),
                "max_qty": int(max_q) if max_q is not None else None,
                "original_unit_price": unit,
                "original_shipping": shipping,
                "tangbuy_unit_price": round(unit * markup, 2),
                "tangbuy_shipping": round(shipping * markup, 2) if shipping is not None else None,
            }
        )
    return tiers


def _stock_status(inventory: int, min_num: int) -> str:
    if inventory <= 0:
        return "out"
    if inventory < max(min_num * 10, 10):
        return "low"
    return "in_stock"


def _map_skus(raw_skus: list[dict[str, Any]]) -> list[dict[str, Any]]:
    skus: list[dict[str, Any]] = []
    for sku in raw_skus:
        if not isinstance(sku, dict):
            continue
        attrs = []
        for a in sku.get("skuAttributes") or []:
            if not isinstance(a, dict):
                continue
            attrs.append(
                {
                    "name": a.get("attrName") or a.get("attrNameTrans"),
                    "value": a.get("attrValue") or a.get("attrValueTrans"),
                }
            )
        skus.append(
            {
                "sku_id": str(sku.get("skuId") or ""),
                "inventory": int(sku.get("inventory") or 0),
                "price": _num(sku.get("price")),
                "attributes": attrs,
            }
        )
    return skus


def map_item_to_product_patch(item: dict[str, Any], *, default_city: str) -> dict[str, Any]:
    shop = item.get("shopInfo") if isinstance(item.get("shopInfo"), dict) else {}
    markup = get_price_markup()
    raw_tiers = item.get("tieredPriceConfigList") or []
    if not raw_tiers:
        skus = item.get("productSkus") or []
        if skus and isinstance(skus[0], dict):
            raw_tiers = skus[0].get("tieredPriceConfigList") or []

    tiers = _map_tier_prices(raw_tiers if isinstance(raw_tiers, list) else [], markup=markup)
    base_tier = tiers[0] if tiers else None

    inventory = int(item.get("inventory") or 0)
    min_num = int(item.get("minNum") or 1)
    post_fee = item.get("postFee")
    shipping = base_tier["original_shipping"] if base_tier else (
        None if post_fee is None else _num(post_fee)
    )

    images = item.get("productImageList") or []
    image_url = images[0] if images else None

    spec_params = []
    for row in item.get("specParamList") or []:
        if isinstance(row, dict) and row.get("name"):
            spec_params.append({"name": row["name"], "value": row.get("value")})

    patch: dict[str, Any] = {
        "detail_enriched_at": _now_iso(),
        "detail_source": "tangbuy",
        "enrichment_status": "matched",
        "shipping_to_city": default_city,
        "inventory_total": inventory,
        "stock_status": _stock_status(inventory, min_num),
        "min_order_qty": min_num,
        "product_skus": _map_skus(item.get("productSkus") or []),
        "spec_params": spec_params,
        "supplier_metrics": {
            "logistics_score": shop.get("shopLogisticsScore"),
            "service_score": shop.get("serviceAttitudeScore"),
            "composite_score": shop.get("shopCompositeScore"),
        },
    }

    name = str(item.get("itemName") or "").strip()
    if name:
        patch["product_name"] = name
    if image_url:
        patch["image_url"] = image_url
    shop_name = str(shop.get("shopName") or "").strip()
    if shop_name:
        patch["shop_name"] = shop_name
    detail_url = str(item.get("detailUrl") or "").strip()
    if detail_url:
        patch["source_url"] = detail_url

    if tiers:
        patch["tier_prices"] = tiers
        patch["original_unit_price"] = base_tier["original_unit_price"]
        patch["tangbuy_unit_price"] = base_tier["tangbuy_unit_price"]
        if shipping is not None:
            patch["original_shipping"] = shipping
            patch["tangbuy_shipping"] = base_tier.get("tangbuy_shipping")
            patch["shipping_source"] = "api"
            patch["shipping_quoted_at"] = patch["detail_enriched_at"]
    elif shipping is not None:
        patch["original_shipping"] = shipping
        patch["tangbuy_shipping"] = round(shipping * markup, 2)
        patch["shipping_source"] = "api"
        patch["shipping_quoted_at"] = patch["detail_enriched_at"]

    sold = item.get("soldOut")
    if sold is not None:
        try:
            patch["sold_count"] = int(sold)
        except (TypeError, ValueError):
            pass

    cat_id = item.get("categoryId")
    if cat_id:
        patch["platform_category_hint"] = str(cat_id)

    return patch


def enrich_product_by_id(product_id: str) -> dict[str, Any]:
    product = get_product_by_id(product_id)
    if not product:
        return {"ok": False, "error": "商品不存在", "product_id": product_id}

    settings = get_settings()

    if not should_portal_enrich(product):
        updated = mark_pending_match(product_id)
        return {
            "ok": True,
            "skipped": True,
            "product_id": product_id,
            "product": updated,
            "enrichment_status": "pending_match",
            "message": "1688 选品线路：跳过 Portal 详情，待匹配",
        }

    page_urls = resolve_item_get_urls(product)
    if not page_urls:
        updated = mark_pending_match(product_id, reason="无可用 itemGet URL")
        return {
            "ok": False,
            "product_id": product_id,
            "product": updated,
            "enrichment_status": "pending_match",
            "error": "无商品链接，无法拉取运费",
        }

    update_product(product_id, lambda p: {**p, "enrichment_status": "running"})

    item: Optional[dict[str, Any]] = None
    page_url = page_urls[0]
    last_err: Optional[TangbuyPortalError] = None
    for candidate_url in page_urls:
        try:
            item = item_get(product_page_url=candidate_url)
            page_url = candidate_url
            break
        except TangbuyPortalError as exc:
            last_err = exc
    if item is None:
        err_msg = str(last_err) if last_err else "itemGet 失败"
        updated = mark_pending_match(product_id, reason=err_msg)
        return {
            "ok": False,
            "error": err_msg,
            "product_id": product_id,
            "product": updated,
            "enrichment_status": "pending_match",
        }

    a_item_id = tangbuy_shop_item_id(product)
    b_item_id = str(item.get("itemId") or "").strip()
    splr_id = str(product.get("source_product_id") or "").strip()
    via_1688 = "1688.com" in page_url

    if via_1688:
        if splr_id and b_item_id and splr_id != b_item_id:
            updated = mark_pending_match(
                product_id,
                reason=f"1688 offer 不一致：{splr_id} vs {b_item_id}",
            )
            return {
                "ok": False,
                "product_id": product_id,
                "product": updated,
                "enrichment_status": "pending_match",
                "error": "1688 商品 ID 不匹配",
            }
    elif a_item_id and b_item_id and a_item_id != b_item_id:
        updated = mark_pending_match(
            product_id,
            reason=f"A/B item_id 不一致：{a_item_id} vs {b_item_id}",
        )
        return {
            "ok": False,
            "product_id": product_id,
            "product": updated,
            "enrichment_status": "pending_match",
            "error": "A/B 商品 ID 不匹配",
        }

    patch = map_item_to_product_patch(item, default_city=settings.tangbuy_default_shipping_city)
    if b_item_id and not via_1688:
        patch["tangbuy_product_id"] = b_item_id

    def merge(p: dict[str, Any]) -> dict[str, Any]:
        merged = {**p, **patch}
        if not patch.get("tier_prices") and p.get("tier_prices"):
            merged["tier_prices"] = p["tier_prices"]
        if patch.get("original_shipping") is None and p.get("original_shipping") is not None:
            merged["original_shipping"] = p["original_shipping"]
            merged["tangbuy_shipping"] = p.get("tangbuy_shipping")
            merged["shipping_source"] = p.get("shipping_source")
        merged.pop("enrichment_error", None)
        return merged

    updated = update_product(product_id, merge)
    return {
        "ok": True,
        "product_id": product_id,
        "product": updated,
        "item_get_url": page_url,
        "enrichment_status": "matched",
    }

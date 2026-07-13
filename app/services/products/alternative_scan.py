"""主图图搜备选供应商（最多 3 个）。

流程：LLM/规则清洗短 search_query → find_product(imageUrl + query) → 排除硬过滤 → 复合分取 Top3。
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from typing import Any, Optional

from app.config.business_config import get_price_markup
from app.core.config import get_settings
from app.core.paths import PROJECT_ROOT
from app.services.products.alt_search_query import (
    AltSearchQueryPlan,
    build_alt_search_query_plan,
    candidate_title_ok,
    plan_to_store_fields,
)
from app.services.products.store import get_product_by_id, update_product

_SCRIPTS = PROJECT_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import newton_cli  # noqa: E402

MAX_ALTERNATIVES = 3
SIMILARITY_SWITCH_THRESHOLD = 85.0
COMPOSITE_SWITCH_MARGIN = 5.0
# 刷新备选：候选相似度低于此值则放弃，触发下一轮图搜
MIN_REFRESH_SIMILARITY = 55.0
MAX_SEARCH_ROUNDS = 3
SEARCH_PAGE_SIZE = 20
MAX_BLOCKED_IDS = 60


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _similarity_pct(score: Any) -> float:
    try:
        val = float(score or 0)
    except (TypeError, ValueError):
        return 0.0
    if val <= 1:
        return round(val * 100, 1)
    return round(min(val, 100), 1)


def _product_total_cost(product: dict[str, Any]) -> tuple[Optional[float], bool]:
    unit = product.get("original_unit_price")
    ship = product.get("original_shipping")
    try:
        unit_v = float(unit) if unit is not None else None
    except (TypeError, ValueError):
        unit_v = None
    if unit_v is None or unit_v <= 0:
        return None, True
    ship_v = None
    ship_unknown = ship is None
    if ship is not None:
        try:
            ship_v = float(ship)
        except (TypeError, ValueError):
            ship_unknown = True
    return unit_v + (ship_v or 0), ship_unknown


def _candidate_unit_price(candidate: dict[str, Any]) -> Optional[float]:
    price = candidate.get("price")
    if price in (None, ""):
        return None
    try:
        return float(price)
    except (TypeError, ValueError):
        return None


def _composite_score(
    *,
    similarity_pct: float,
    candidate_cost: Optional[float],
    current_cost: Optional[float],
    yx_index: Any,
    sold_count: int,
) -> float:
    if candidate_cost is None or current_cost is None or current_cost <= 0:
        cost_score = 40.0
    else:
        ratio = candidate_cost / current_cost
        cost_score = max(0.0, min(100.0, (2.0 - ratio) * 50.0))

    try:
        yx = float(yx_index or 0)
    except (TypeError, ValueError):
        yx = 0.0
    fulfil_score = min(100.0, yx * 20.0) if yx > 0 else 50.0
    sold_score = min(100.0, sold_count / 50.0)

    return round(
        similarity_pct * 0.35 + cost_score * 0.35 + fulfil_score * 0.20 + sold_score * 0.10,
        1,
    )


def _current_composite(product: dict[str, Any]) -> float:
    metrics = product.get("supplier_metrics") or {}
    try:
        yx = float(metrics.get("composite_score") or metrics.get("logistics_score") or 0)
    except (TypeError, ValueError):
        yx = 0.0
    fulfil = min(100.0, yx * 20.0) if yx > 0 else 50.0
    sold = int(product.get("sold_count") or 0)
    sold_score = min(100.0, sold / 50.0)
    return round(50.0 * 0.35 + 50.0 * 0.35 + fulfil * 0.20 + sold_score * 0.10, 1)


def _current_unit_price(product: dict[str, Any]) -> Optional[float]:
    """加价前单价（不含运费），备选对比基准。"""
    try:
        unit = float(product.get("original_unit_price") or 0)
    except (TypeError, ValueError):
        unit = 0.0
    if unit > 0:
        return unit
    try:
        tb = float(product.get("tangbuy_unit_price") or 0)
    except (TypeError, ValueError):
        tb = 0.0
    if tb > 0:
        return round(tb / get_price_markup(), 2)
    return None


def _candidate_title(candidate: dict[str, Any]) -> str:
    """图搜原始 title 一定有；兼容落地前后字段名。"""
    for key in ("title", "subject", "offerTitle", "itemTitle", "skuTitle", "product_name"):
        raw = candidate.get(key)
        if raw is None:
            continue
        text = str(raw).strip()
        if text and text != "（无标题）":
            return text
    return ""


def _build_alternative(
    candidate: dict[str, Any],
    *,
    current: dict[str, Any],
    current_cost: Optional[float],
    current_composite: float,
    default_city: str,
) -> dict[str, Any]:
    sim_pct = _similarity_pct(candidate.get("similarity_score") or candidate.get("score"))
    unit = _candidate_unit_price(candidate)
    if unit is None and candidate.get("unit_price") is not None:
        try:
            unit = float(candidate.get("unit_price"))
        except (TypeError, ValueError):
            unit = None
    current_unit = _current_unit_price(current)
    # 成本维只用加价前单价：备选图搜无运费，避免与「当前价+运」不对称
    compare_current = current_unit if current_unit is not None else current_cost
    try:
        yx_raw = candidate.get("yx_index") or candidate.get("yxIndex")
        yx_val = float(yx_raw) if yx_raw not in (None, "") else None
    except (TypeError, ValueError):
        yx_val = None
    composite = _composite_score(
        similarity_pct=sim_pct,
        candidate_cost=unit,
        current_cost=compare_current,
        yx_index=yx_val,
        sold_count=int(candidate.get("sold_count") or candidate.get("soldOut") or 0),
    )

    labels: list[str] = []
    if unit is not None and compare_current is not None and unit < compare_current * 0.98:
        labels.append("价格更低")
    sold = int(candidate.get("sold_count") or candidate.get("soldOut") or 0)
    if sold >= 100:
        labels.append("销量较高")
    if (
        sim_pct >= SIMILARITY_SWITCH_THRESHOLD
        and composite >= current_composite + COMPOSITE_SWITCH_MARGIN
    ):
        labels.append("建议换供")

    offer_id = str(
        candidate.get("product_id") or candidate.get("offer_id") or candidate.get("itemId") or ""
    ).strip()
    title = _candidate_title(candidate)
    shop = str(
        candidate.get("supplier") or candidate.get("shop_name") or candidate.get("company") or "—"
    ).strip() or "—"
    detail = str(
        candidate.get("detail_url") or candidate.get("detailUrl") or ""
    ).strip() or (f"https://detail.1688.com/offer/{offer_id}.html" if offer_id else "")
    image = str(candidate.get("image_url") or candidate.get("imageUrl") or "")

    try:
        min_qty = candidate.get("min_order_qty")
        min_order_qty = int(min_qty) if min_qty not in (None, "") else None
    except (TypeError, ValueError):
        min_order_qty = None
    try:
        inv = candidate.get("inventory")
        inventory = int(inv) if inv not in (None, "") else None
    except (TypeError, ValueError):
        inventory = None

    cate = candidate.get("cate_id") or candidate.get("platform_category_hint")
    stock_status = None
    if inventory is not None:
        if inventory <= 0:
            stock_status = "out"
        elif inventory < 10:
            stock_status = "low"
        else:
            stock_status = "in_stock"

    return {
        "offer_id": offer_id,
        "title": title or None,
        "shop_name": shop,
        "detail_url": detail,
        "image_url": image,
        "unit_price": unit,
        "shipping": None,
        "total_cost": unit,
        "shipping_to_city": default_city,
        "similarity_pct": sim_pct,
        "composite_score": composite,
        "yx_index": yx_val,
        "sold_count": sold,
        "sku_id": str(candidate.get("sku_id") or "").strip() or None,
        "sku_title": str(candidate.get("sku_title") or "").strip() or None,
        "cate_id": str(cate).strip() if cate not in (None, "") else None,
        "industry_name": str(candidate.get("industry_name") or "").strip() or None,
        "min_order_qty": min_order_qty,
        "inventory": inventory,
        "stock_status": stock_status,
        "selling_points": candidate.get("selling_points")
        if isinstance(candidate.get("selling_points"), list)
        else None,
        "service_tags": candidate.get("service_tags")
        if isinstance(candidate.get("service_tags"), list)
        else None,
        "offer_tags": candidate.get("offer_tags")
        if isinstance(candidate.get("offer_tags"), list)
        else None,
        "labels": labels,
        "ranked_at": _now_iso(),
    }


def _collect_offer_ids(alts: Any) -> list[str]:
    out: list[str] = []
    if not isinstance(alts, list):
        return out
    for a in alts:
        if not isinstance(a, dict):
            continue
        oid = str(a.get("offer_id") or "").strip()
        if oid and oid not in out:
            out.append(oid)
    return out


def _merge_blocked(existing: Any, extra: list[str]) -> list[str]:
    merged: list[str] = []
    for oid in list(existing or []) + extra:
        s = str(oid or "").strip()
        if s and s not in merged:
            merged.append(s)
    return merged[-MAX_BLOCKED_IDS:]


def _rank_candidates(
    candidates: list[Any],
    *,
    product: dict[str, Any],
    plan: AltSearchQueryPlan,
    blocked: set[str],
    current_offer: str,
    current_cost: Optional[float],
    current_composite: float,
    default_city: str,
    min_similarity: float,
) -> tuple[list[dict[str, Any]], int, int]:
    """返回 (ranked, filtered_hard, skipped_blocked_or_low_sim)。"""
    seen: set[str] = set()
    ranked: list[dict[str, Any]] = []
    filtered_hard = 0
    skipped = 0
    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        oid = str(cand.get("product_id") or "").strip()
        if not oid or oid == current_offer or oid in seen:
            continue
        if oid in blocked:
            skipped += 1
            continue
        title = str(cand.get("title") or "")
        if not candidate_title_ok(title, plan):
            filtered_hard += 1
            continue
        alt = _build_alternative(
            cand,
            current=product,
            current_cost=current_cost,
            current_composite=current_composite,
            default_city=default_city,
        )
        if float(alt.get("similarity_pct") or 0) < min_similarity:
            skipped += 1
            continue
        seen.add(oid)
        ranked.append(alt)
    ranked.sort(key=lambda x: x.get("composite_score", 0), reverse=True)
    return ranked, filtered_hard, skipped


def _extract_candidates(result: dict[str, Any]) -> list[dict[str, Any]]:
    data = result.get("data") or {}
    candidates = data.get("similar_products") or data.get("products") or []
    if not isinstance(candidates, list):
        return []
    return [c for c in candidates if isinstance(c, dict)]


def _fetch_search_candidates(
    image_url: str,
    plan: AltSearchQueryPlan,
    *,
    limit: int,
) -> tuple[list[dict[str, Any]], Optional[str]]:
    """图搜为主；空结果或瞬时失败时降级文搜。"""
    last_error: Optional[str] = None
    for attempt in range(2):
        try:
            result = newton_cli.search_image(
                image_url,
                limit=limit,
                query=plan.search_query or None,
            )
            if not result.get("success"):
                raise RuntimeError(result.get("error") or result.get("markdown") or "图搜失败")
            candidates = _extract_candidates(result)
            if candidates:
                return candidates, None
            last_error = None
            break
        except Exception as exc:
            last_error = str(exc)
            if attempt == 0:
                time.sleep(1.0)
                continue
            break

    query = (plan.search_query or plan.subject or "").strip()
    if not query:
        return [], last_error
    try:
        result = newton_cli.search_text(query, limit=limit)
        if not result.get("success"):
            return [], last_error or str(result.get("error") or "文搜失败")
        return _extract_candidates(result), None
    except Exception as exc:
        return [], last_error or str(exc)


def scan_product_alternatives(
    product_id: str,
    *,
    refresh: bool = False,
) -> dict[str, Any]:
    product = get_product_by_id(product_id)
    if not product:
        return {"ok": False, "error": "商品不存在", "product_id": product_id}

    # 入队时可能已写入 refresh 标记
    do_refresh = refresh or bool(product.get("alt_supplier_scan_refresh"))

    image_url = str(product.get("image_url") or "").strip()
    if not image_url:
        update_product(
            product_id,
            lambda p: {
                **p,
                "alt_supplier_scan_status": "failed",
                "alt_supplier_scanned_at": _now_iso(),
                "alt_supplier_scan_error": "缺少主图",
                "alt_supplier_scan_refresh": False,
            },
        )
        return {"ok": False, "error": "缺少主图", "product_id": product_id}

    if not newton_cli._ak_ready():
        update_product(
            product_id,
            lambda p: {
                **p,
                "alt_supplier_scan_status": "failed",
                "alt_supplier_scanned_at": _now_iso(),
                "alt_supplier_scan_error": "1688 AK 未配置",
                "alt_supplier_scan_refresh": False,
            },
        )
        return {"ok": False, "error": "1688 AK 未配置", "product_id": product_id}

    settings = get_settings()
    update_product(product_id, lambda p: {**p, "alt_supplier_scan_status": "running"})

    prev_round = _collect_offer_ids(product.get("alternative_suppliers"))
    blocked_list = list(product.get("alt_supplier_blocked_ids") or [])
    if do_refresh and prev_round:
        blocked_list = _merge_blocked(blocked_list, prev_round)
    blocked = set(blocked_list)

    plan: AltSearchQueryPlan = build_alt_search_query_plan(product)
    query_fields = plan_to_store_fields(plan)

    current_offer = str(product.get("source_product_id") or "").strip()
    current_cost, _ = _product_total_cost(product)
    current_composite = _current_composite(product)
    min_sim = MIN_REFRESH_SIMILARITY if do_refresh else 0.0

    top: list[dict[str, Any]] = []
    total_scanned = 0
    total_filtered = 0
    total_skipped = 0
    rounds = 0
    last_error: Optional[str] = None

    # 刷新：屏蔽上一轮后若不足 3 个达标候选，多轮图搜再取
    max_rounds = MAX_SEARCH_ROUNDS if do_refresh else 1
    for round_i in range(max_rounds):
        rounds = round_i + 1
        try:
            # 后续轮适当加大 limit，尽量拿到未被屏蔽的新 offer
            limit = SEARCH_PAGE_SIZE + (round_i * 10)
            candidates, fetch_error = _fetch_search_candidates(
                image_url,
                plan,
                limit=limit,
            )
            if fetch_error and not candidates:
                raise RuntimeError(fetch_error)
        except Exception as exc:
            last_error = str(exc)
            if rounds == 1 and not top:
                update_product(
                    product_id,
                    lambda p: {
                        **p,
                        **query_fields,
                        "alt_supplier_scan_status": "failed",
                        "alt_supplier_scanned_at": _now_iso(),
                        "alt_supplier_scan_error": last_error,
                        "alt_supplier_scan_refresh": False,
                    },
                )
                return {"ok": False, "error": last_error, "product_id": product_id}
            break

        total_scanned += len(candidates)
        ranked, filtered_hard, skipped = _rank_candidates(
            candidates,
            product=product,
            plan=plan,
            blocked=blocked,
            current_offer=current_offer,
            current_cost=current_cost,
            current_composite=current_composite,
            default_city=settings.tangbuy_default_shipping_city,
            min_similarity=min_sim,
        )
        total_filtered += filtered_hard
        total_skipped += skipped

        # 合并本轮尚未入选的新候选（按分）
        have_ids = {str(a.get("offer_id") or "") for a in top}
        for alt in ranked:
            oid = str(alt.get("offer_id") or "")
            if not oid or oid in have_ids:
                continue
            top.append(alt)
            have_ids.add(oid)
            if len(top) >= MAX_ALTERNATIVES:
                break

        top.sort(key=lambda x: x.get("composite_score", 0), reverse=True)
        top = top[:MAX_ALTERNATIVES]
        if len(top) >= MAX_ALTERNATIVES:
            break
        # 不足 3 个 → 下一轮图搜；已入选的也暂屏蔽，避免重复占位
        blocked.update(have_ids)

    # 刷新时若严格阈值凑不足 3 个，用本轮已屏蔽池外的较低相似度结果补齐（不再无限轮）
    if do_refresh and len(top) < MAX_ALTERNATIVES and rounds >= 1:
        try:
            candidates, _ = _fetch_search_candidates(
                image_url,
                plan,
                limit=SEARCH_PAGE_SIZE + 20,
            )
            if candidates:
                total_scanned += len(candidates)
                soft, filtered_hard, skipped = _rank_candidates(
                    candidates,
                    product=product,
                    plan=plan,
                    blocked=blocked | {str(a.get("offer_id") or "") for a in top},
                    current_offer=current_offer,
                    current_cost=current_cost,
                    current_composite=current_composite,
                    default_city=settings.tangbuy_default_shipping_city,
                    min_similarity=0.0,
                )
                total_filtered += filtered_hard
                total_skipped += skipped
                have_ids = {str(a.get("offer_id") or "") for a in top}
                for alt in soft:
                    oid = str(alt.get("offer_id") or "")
                    if not oid or oid in have_ids:
                        continue
                    top.append(alt)
                    have_ids.add(oid)
                    if len(top) >= MAX_ALTERNATIVES:
                        break
                rounds += 1
        except Exception:
            pass

    top.sort(key=lambda x: x.get("composite_score", 0), reverse=True)
    top = top[:MAX_ALTERNATIVES]
    # 本批展示位进屏蔽池，供下次刷新排除
    next_blocked = _merge_blocked(blocked_list, _collect_offer_ids(top))

    def merge(p: dict[str, Any]) -> dict[str, Any]:
        return {
            **p,
            **query_fields,
            "alternative_suppliers": top,
            "alt_supplier_blocked_ids": next_blocked,
            "alt_supplier_scan_status": "done",
            "alt_supplier_scanned_at": _now_iso(),
            "alt_supplier_scan_refresh": False,
            "shipping_to_city": p.get("shipping_to_city") or settings.tangbuy_default_shipping_city,
            **({"alt_supplier_scan_error": None} if top else {}),
        }

    updated = update_product(product_id, merge)
    return {
        "ok": True,
        "product_id": product_id,
        "product": updated,
        "candidates_scanned": total_scanned,
        "candidates_filtered": total_filtered,
        "candidates_skipped": total_skipped,
        "alternatives": len(top),
        "search_query": plan.search_query,
        "query_source": plan.source,
        "refresh": do_refresh,
        "rounds": rounds,
        "blocked_count": len(next_blocked),
    }

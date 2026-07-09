"""换供对比：当前货源 + 最多 3 个备选，LLM 挑最推荐。"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from app.config.business_config import get_price_markup
from app.core.config import get_settings
from app.services.products.store import get_product_by_id

COMPARE_SYSTEM_PROMPT = """你是跨境采购「换货源」决策助手。
根据当前货源与备选的价格、销量、相似度、标题，选出最值得采购的一个。
原则：
1. 优先外观/标题足够像（similarity_pct 高），再看加价前单价更低、销量更健康。
2. 明显偏类/标题不一致的不要推荐，即使更便宜。
3. 若当前已是综合最优，recommended_id 必须为 "current"。
4. 只输出一行 JSON，不要解释。
模板：
{"recommended_id":"current"|"offerId","reason":"简短中文","confidence":0.0-1.0}
"""


def _unit(product: dict[str, Any]) -> Optional[float]:
    try:
        u = float(product.get("original_unit_price") or 0)
    except (TypeError, ValueError):
        u = 0.0
    if u > 0:
        return u
    try:
        tb = float(product.get("tangbuy_unit_price") or 0)
    except (TypeError, ValueError):
        tb = 0.0
    if tb > 0:
        return round(tb / get_price_markup(), 2)
    return None


def _alt_unit(alt: dict[str, Any]) -> Optional[float]:
    try:
        u = float(alt.get("unit_price")) if alt.get("unit_price") is not None else None
    except (TypeError, ValueError):
        return None
    return u if u is not None and u > 0 else None


def _candidate_rows(product: dict[str, Any]) -> list[dict[str, Any]]:
    current_unit = _unit(product)
    metrics = product.get("supplier_metrics") or {}
    rows: list[dict[str, Any]] = [
        {
            "id": "current",
            "role": "current",
            "title": str(product.get("product_name") or ""),
            "shop_name": str(product.get("shop_name") or "—"),
            "image_url": str(product.get("image_url") or ""),
            "unit_price": current_unit,
            "sold_count": int(product.get("sold_count") or 0),
            "similarity_pct": 100.0,
            "yx_index": metrics.get("composite_score") or metrics.get("logistics_score"),
            "offer_id": str(product.get("source_product_id") or ""),
            "detail_url": str(product.get("source_url") or ""),
            "can_switch": False,
        }
    ]
    for alt in (product.get("alternative_suppliers") or [])[:3]:
        if not isinstance(alt, dict):
            continue
        oid = str(alt.get("offer_id") or "").strip()
        if not oid:
            continue
        rows.append(
            {
                "id": oid,
                "role": "alternative",
                "title": str(alt.get("title") or ""),
                "shop_name": str(alt.get("shop_name") or "—"),
                "image_url": str(alt.get("image_url") or ""),
                "unit_price": _alt_unit(alt),
                "sold_count": int(alt.get("sold_count") or 0),
                "similarity_pct": float(alt.get("similarity_pct") or 0),
                "yx_index": alt.get("yx_index"),
                "offer_id": oid,
                "detail_url": str(alt.get("detail_url") or ""),
                "can_switch": True,
            }
        )
    return rows


def _rules_recommend(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """无 LLM：相似≥80 里取价低；否则综合分最高。"""
    def score(r: dict[str, Any]) -> float:
        sim = float(r.get("similarity_pct") or 0)
        price = r.get("unit_price")
        sold = int(r.get("sold_count") or 0)
        price_score = 50.0
        cur = next((x.get("unit_price") for x in rows if x["id"] == "current"), None)
        if price is not None and cur and cur > 0:
            price_score = max(0.0, min(100.0, (2.0 - float(price) / float(cur)) * 50.0))
        sold_score = min(100.0, sold / 50.0)
        return sim * 0.45 + price_score * 0.4 + sold_score * 0.15

    pool = [r for r in rows if float(r.get("similarity_pct") or 0) >= 80] or rows
    best = max(pool, key=score)
    return {
        "recommended_id": best["id"],
        "reason": "规则综合：相似度/价格/销量",
        "confidence": 0.55,
        "source": "rules",
    }


def _llm_recommend(rows: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    settings = get_settings()
    if not settings.llm_configured:
        return None
    from app.services.agent.llm import chat_completion

    payload = [
        {
            "id": r["id"],
            "role": r["role"],
            "title": r["title"][:80],
            "unit_price": r["unit_price"],
            "sold_count": r["sold_count"],
            "similarity_pct": r["similarity_pct"],
            "image_url": r["image_url"][:120] if r.get("image_url") else "",
        }
        for r in rows
    ]
    try:
        resp = chat_completion(
            [
                {"role": "system", "content": COMPARE_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps({"candidates": payload}, ensure_ascii=False)},
            ]
        )
        raw = (resp.content or "").strip()
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            return None
        data = json.loads(m.group(0))
        rid = str(data.get("recommended_id") or "").strip()
        ids = {r["id"] for r in rows}
        if rid not in ids:
            return None
        conf = float(data.get("confidence") or 0.7)
        return {
            "recommended_id": rid,
            "reason": str(data.get("reason") or "LLM 推荐")[:80],
            "confidence": min(0.95, max(0.4, conf)),
            "source": "llm",
        }
    except Exception:
        return None


def compare_product_alternatives(product_id: str) -> dict[str, Any]:
    product = get_product_by_id(product_id)
    if not product:
        return {"ok": False, "error": "商品不存在", "product_id": product_id}

    rows = _candidate_rows(product)
    if len(rows) < 2:
        return {
            "ok": False,
            "error": "暂无备选可对比",
            "product_id": product_id,
            "candidates": rows,
        }

    pick = _llm_recommend(rows) or _rules_recommend(rows)
    current_unit = rows[0].get("unit_price")
    for r in rows:
        pu = r.get("unit_price")
        if pu is None or current_unit in (None, 0):
            r["price_diff_pct"] = None
        else:
            r["price_diff_pct"] = round((float(pu) / float(current_unit) - 1) * 100, 1)
        r["recommended"] = r["id"] == pick["recommended_id"]

    return {
        "ok": True,
        "product_id": product_id,
        "candidates": rows,
        "recommendation": pick,
    }

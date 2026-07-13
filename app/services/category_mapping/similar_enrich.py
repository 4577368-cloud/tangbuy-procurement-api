"""为 suggest 结果附加相近类目与权威税则候选（P0）。"""

from __future__ import annotations

import re
from typing import Any

# 标题倒排噪声：参与税则检索易误命中无关编码
_AUTH_STOP_TOKENS = frozenset(
    {
        "其他",
        "制品",
        "用品",
        "配件",
        "材料",
        "产品",
        "商品",
        "女士",
        "男士",
        "新款",
        "时尚",
        "跨境",
        "外贸",
        "批发",
        "定制",
        "现货",
        "包邮",
        "爆款",
        "热销",
        "专用",
        "通用",
        "套装",
        "系列",
        "精品",
        "高端",
        "优质",
        "多功能",
        "便携式",
        "家用",
        "商用",
        "户外",
        "室内",
        "男女",
        "成人",
        "儿童",
        "the",
        "and",
        "for",
        "with",
    }
)

_AUTH_NOISE_RE = re.compile(
    r"(批发|代发|跨境|外贸|爆款|热销|一件代发|工厂|现货|包邮|直销|新款|潮款|"
    r"帅气|百搭|时尚|精品|特价|促销|厂家|源头|实力|网红|同款|直播|专供|定制|"
    r"wholesale|cross[\s-]?border|hot\s?sale|factory|OEM|ODM|dropshipping)",
    re.I,
)


def _cid(entry: dict[str, Any]) -> int:
    try:
        return int(entry.get("category_id") or entry.get("cid") or 0)
    except (TypeError, ValueError):
        return 0


def _hs_mapping_from_entry(entry: dict[str, Any]) -> dict[str, Any]:
    cid = _cid(entry)
    return {
        "category_id": cid,
        "category_cn_name": str(entry.get("category_cn_name") or entry.get("cn_name") or "").strip(),
        "category_en_name": str(entry.get("category_en_name") or entry.get("en_name") or "").strip(),
        "hs_code": str(entry.get("hs_code") or "").strip(),
        "declare_cn_name": str(entry.get("declare_cn_name") or entry.get("dec_cn_name") or "").strip(),
        "declare_en_name": str(entry.get("declare_en_name") or entry.get("dec_en_name") or "").strip(),
        "tariff": entry.get("tariff"),
    }


def _append_similar(
    out: list[dict[str, Any]],
    seen: set[int],
    entry: dict[str, Any],
    *,
    source: str,
    score: float,
    reason: str = "",
) -> None:
    cid = _cid(entry)
    if cid <= 0 or cid in seen:
        return
    hs = _hs_mapping_from_entry(entry)
    if not hs["category_cn_name"] and not hs["hs_code"]:
        return
    seen.add(cid)
    out.append(
        {
            **hs,
            "score": round(float(score), 3),
            "source": source,
            "reason": reason,
        }
    )


def _meaningful_tokens(text: str) -> set[str]:
    from app.services.category_mapping.hs_authoritative import tokenize_for_search

    return {
        t
        for t in tokenize_for_search(text)
        if len(t) >= 2 and t not in _AUTH_STOP_TOKENS and not t.isdigit()
    }


def _auth_search_query(result: dict[str, Any], title: str, hint: str) -> str:
    """税则检索用语义主体词，不用整段营销标题。"""
    parts: list[str] = []
    for cand in result.get("semantic_candidates") or []:
        if not isinstance(cand, dict):
            continue
        for key in ("label", "category_cn_name", "declare_cn_name"):
            val = str(cand.get(key) or "").strip()
            if val and val not in parts:
                parts.append(val)
        if len(parts) >= 2:
            break
    if parts:
        return " ".join(parts[:3])
    if result.get("success"):
        for key in ("category_cn_name", "declare_cn_name", "label"):
            val = str(result.get(key) or "").strip()
            if val:
                parts.append(val)
        if parts:
            return " ".join(parts[:2])
    cleaned = _AUTH_NOISE_RE.sub(" ", title or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if hint and hint.strip():
        return f"{hint.strip()} {cleaned}".strip()[:48]
    return cleaned[:48]


def _filter_auth_hits(
    hits: list[dict[str, Any]],
    *,
    query: str,
    primary_hs: str = "",
) -> list[dict[str, Any]]:
    """剔除与推荐类目语义无关的税则编码。"""
    if not hits:
        return []
    context = _meaningful_tokens(query)
    primary_prefix = re.sub(r"\D", "", primary_hs)[:4] if primary_hs else ""
    filtered: list[dict[str, Any]] = []
    for hit in hits:
        score = float(hit.get("score") or 0)
        if score >= 1e5:
            filtered.append(hit)
            continue
        blob = " ".join(
            [
                str(hit.get("declare_cn_name") or ""),
                " ".join(hit.get("names") or []),
            ]
        )
        hit_tokens = _meaningful_tokens(blob)
        overlap = context & hit_tokens
        long_overlap = [t for t in overlap if len(t) >= 3]
        hs = re.sub(r"\D", "", str(hit.get("hs_code") or ""))
        same_chapter = bool(primary_prefix and hs.startswith(primary_prefix))
        if long_overlap or (len(overlap) >= 2 and score >= 2):
            filtered.append(hit)
        elif same_chapter and overlap:
            filtered.append(hit)
    return filtered


def enrich_suggest_response(
    result: dict[str, Any],
    *,
    title: str = "",
    hint: str = "",
    limit: int = 8,
) -> dict[str, Any]:
    """在 mapper 结果上附加 similar / authoritative_near，供复核页四栏展示。"""
    if not isinstance(result, dict):
        return result

    out = dict(result)
    primary_cid = _cid(out) if out.get("success") else 0
    seen: set[int] = set()
    if primary_cid > 0:
        seen.add(primary_cid)

    similar: list[dict[str, Any]] = []

    for idx, cand in enumerate(out.get("semantic_candidates") or []):
        if not isinstance(cand, dict):
            continue
        label = str(cand.get("label") or cand.get("category_cn_name") or "").strip()
        score = float(cand.get("confidence") or cand.get("score") or max(0.35, 0.85 - idx * 0.08))
        _append_similar(
            similar,
            seen,
            cand,
            source="semantic",
            score=score,
            reason=f"语义相近：{label}" if label else "语义相近",
        )

    for idx, cand in enumerate(out.get("candidates") or []):
        if not isinstance(cand, dict):
            continue
        label = str(cand.get("category_cn_name") or "").strip()
        score = float(cand.get("score") or max(0.3, 0.7 - idx * 0.06))
        _append_similar(
            similar,
            seen,
            cand,
            source="catalog",
            score=score,
            reason=f"目录匹配：{label}" if label else "目录关键词",
        )

    query = (title or out.get("title_zh") or "").strip()
    if not query and hint:
        query = hint.strip()

    if len(similar) < limit and query:
        try:
            from app.services.category_mapping.catalog_search import search_hs_catalog

            for hit in search_hs_catalog(query, limit):
                if not isinstance(hit, dict):
                    continue
                label = str(hit.get("category_cn_name") or "").strip()
                score = float(hit.get("score") or 0.25)
                _append_similar(
                    similar,
                    seen,
                    hit,
                    source="search",
                    score=score,
                    reason=f"标题检索：{label}" if label else "标题检索",
                )
                if len(similar) >= limit:
                    break
        except Exception:
            pass

    out["similar"] = similar[:limit]

    auth_near: list[dict[str, Any]] = []
    auth_query = _auth_search_query(out, query, hint or "")
    primary_hs = str(out.get("hs_code") or "").strip() if out.get("success") else ""
    if auth_query:
        try:
            from app.services.category_mapping import hs_authoritative

            if hs_authoritative.is_ready():
                raw_hits = hs_authoritative.search(auth_query, limit * 2)
                for hit in _filter_auth_hits(
                    raw_hits,
                    query=auth_query,
                    primary_hs=primary_hs,
                )[:limit]:
                    if not isinstance(hit, dict):
                        continue
                    code = str(hit.get("hs_code") or "").strip()
                    if not code:
                        continue
                    names = hit.get("names") if isinstance(hit.get("names"), list) else []
                    auth_near.append(
                        {
                            "hs_code": code,
                            "declare_cn_name": str(hit.get("declare_cn_name") or "").strip(),
                            "declare_en_name": str(hit.get("declare_en_name") or "").strip(),
                            "score": round(float(hit.get("score") or 0), 3),
                            "names": [str(n) for n in names if n][:6],
                            "control_mark": hit.get("control_mark"),
                        }
                    )
        except Exception:
            pass

    out["authoritative_near"] = auth_near
    return out

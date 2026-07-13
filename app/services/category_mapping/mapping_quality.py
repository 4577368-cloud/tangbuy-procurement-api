"""品类映射质量门禁 — 标题/识图与 HS 是否一致，供复用缓存与自动放行决策。"""

from __future__ import annotations

import re
from typing import Any, Optional

# 申报意图同义词群（与 Web semantic-intent.ts 保持语义对齐）
INTENT_GROUPS: tuple[tuple[str, ...], ...] = (
    ("运动裤", "长裤", "裤子", "休闲裤", "卫裤", "男裤", "女裤", "直筒裤"),
    ("卫衣", "帽衫", "长袖卫衣", "套头卫衣"),
    ("T恤", "短袖T恤", "短袖", "上衣"),
    ("连衣裙", "长裙", "半身裙", "裙子"),
    ("双肩背包", "双肩包", "背包", "书包", "休闲背包"),
    ("单肩包", "斜挎包", "斜跨包", "胸包", "腰包", "手提包", "挎包"),
    ("运动鞋", "跑鞋", "休闲鞋", "板鞋", "球鞋"),
    ("凉鞋", "凉拖", "拖鞋", "沙滩鞋"),
    ("帽子", "渔夫帽", "遮阳帽", "棒球帽"),
    ("耳机", "蓝牙耳机", "无线耳机"),
    ("袜子", "长袜", "短袜", "连裤袜"),
)

# (标题侧模式, 类目侧模式) — 命中即视为跨类冲突
OBVIOUS_CONFLICTS: tuple[tuple[str, str], ...] = (
    (r"裤|长裤|运动裤|休闲裤", r"卫衣|毛衣|针织上衣|帽衫"),
    (r"卫衣|帽衫|毛衣", r"裤|长裤|运动裤|凉鞋|鞋"),
    (r"包|胸包|斜挎|背包|挎包", r"玩具|益智|积木|玩偶"),
    (r"玩具|益智|积木", r"包|裤|衣|鞋|帽"),
    (r"鞋|靴|凉鞋", r"耳机|音箱|路由器|玩具"),
    (r"耳机|蓝牙", r"裤|裙|卫衣|玩具"),
    # 石器/石材 vs 电子网络设备 — 双向冲突
    (r"路由器|网卡|交换机|网关|光纤|集线器|modem|中继器", r"石槽|石磨|石器|青石板|老石|石雕|石盆|石刻|石制品|石条|石材|石灯笼"),
    (r"石槽|石磨|石器|青石板|老石|石雕|石盆|石刻|石制品|石条|石材|石灯笼", r"路由器|网卡|交换机|网关|光纤|集线器|modem|中继器"),
    # 石器/石材 vs 服装鞋帽 — 跨域冲突
    (r"石槽|石磨|石器|青石板|老石|石雕|石盆|石刻|石制品|石条", r"卫衣|毛衣|裤|裙|袜|鞋|帽|包|背心|内衣"),
    (r"连衣裙|半身裙|裙子", r"裤|运动裤|男裤"),
)


def _blob(*parts: Any) -> str:
    return " ".join(str(p or "").strip() for p in parts if p).lower()


def _intent_group(term: str) -> Optional[str]:
    t = term.lower()
    for i, group in enumerate(INTENT_GROUPS):
        for g in group:
            gl = g.lower()
            if t == gl or gl in t or t in gl:
                return f"g{i}"
    return None


def _find_intent_terms(text: str) -> list[str]:
    lower = text.lower()
    hits: list[str] = []
    seen: set[str] = set()
    for group in INTENT_GROUPS:
        for term in sorted(group, key=len, reverse=True):
            if term.lower() not in lower:
                continue
            gid = _intent_group(term)
            if gid and gid not in seen:
                seen.add(gid)
                hits.append(term)
            break
    return hits


def has_obvious_conflict(title_blob: str, category_blob: str) -> bool:
    for title_pat, cat_pat in OBVIOUS_CONFLICTS:
        if re.search(title_pat, title_blob, re.I) and re.search(cat_pat, category_blob, re.I):
            return True
    return False


def mapping_aligns_with_title(
    title: str,
    hs: dict[str, Any],
    *,
    vision_keywords: Optional[list[str]] = None,
) -> tuple[bool, str, float]:
    """返回 (是否通过, 说明, 0~1 分数)。"""
    title_blob = _blob(title, *(vision_keywords or []))
    cat_blob = _blob(
        hs.get("category_cn_name"),
        hs.get("declare_cn_name"),
        hs.get("category_en_name"),
        hs.get("declare_en_name"),
    )
    if not title_blob.strip():
        return False, "缺少商品标题", 0.0
    if not cat_blob.strip():
        return False, "缺少报关品类字段", 0.0

    if has_obvious_conflict(title_blob, cat_blob):
        return False, f"标题与类目存在明显跨类冲突（{hs.get('category_cn_name') or '—'}）", 0.05

    title_intents = _find_intent_terms(title_blob)
    cat_intents = _find_intent_terms(cat_blob)

    if title_intents and cat_intents:
        title_groups = {_intent_group(t) for t in title_intents}
        cat_groups = {_intent_group(t) for t in cat_intents}
        title_groups.discard(None)
        cat_groups.discard(None)
        if title_groups and cat_groups and title_groups.isdisjoint(cat_groups):
            return (
                False,
                f"标题意图「{'/'.join(title_intents[:2])}」与类目「{hs.get('category_cn_name') or '—'}」不一致",
                0.12,
            )
        return True, f"标题意图与类目一致（{'/'.join(title_intents[:2])}）", 0.88

    # 子串互含（具体词 ≥2 字）
    for term in sorted(set(re.findall(r"[\u4e00-\u9fff]{2,8}", title_blob)), key=len, reverse=True):
        if term in cat_blob or any(term in c or c in term for c in re.findall(r"[\u4e00-\u9fff]{2,8}", cat_blob)):
            return True, f"标题词「{term}」与类目相关", 0.72

    if any(kw in cat_blob for kw in (vision_keywords or []) if kw and len(kw) >= 2):
        return True, "识图关键词与类目一致", 0.8

    return False, f"标题与类目「{hs.get('category_cn_name') or '—'}」相关性不足", 0.18


def hint_conflicts_title(title: str, hint: str) -> bool:
    """平台/Admin 类目提示与标题意图明显冲突时，不应作为映射参照。"""
    if not hint or not title:
        return False
    ok, _, _ = mapping_aligns_with_title(title, {"category_cn_name": hint, "declare_cn_name": hint})
    return not ok


def assess_mapping_quality(
    title: str,
    hs: dict[str, Any],
    *,
    vision_keywords: Optional[list[str]] = None,
    match_method: Optional[str] = None,
    confidence: float = 0.0,
    matched_keywords: Optional[list[str]] = None,
) -> dict[str, Any]:
    ok, detail, score = mapping_aligns_with_title(title, hs, vision_keywords=vision_keywords)
    method = (match_method or "").strip()
    # 本地缓存 / 历史命中也必须过标题门禁，否则只能人工确认
    if method in ("local_item_mapped", "history_goods_id") and not ok:
        auto_pass = False
    elif method in ("platform_reference", "platform_fallback", "admin_existing"):
        auto_pass = False
    else:
        auto_pass = ok and confidence >= 0.72 and score >= 0.55

    # 关键词匹配类方法：校验 matched_keywords 是否真的出现在类目名中
    # 防止「标题语义词「石槽」与类目「路由器」一致」这种跨类误匹配被自动放行
    if auto_pass and method.startswith("keyword") and matched_keywords:
        cat_blob = _blob(
            hs.get("category_cn_name"),
            hs.get("declare_cn_name"),
            hs.get("category_en_name"),
            hs.get("declare_en_name"),
        )
        kw_hit = any(
            kw and len(kw) >= 2 and kw in cat_blob
            for kw in matched_keywords
        )
        if not kw_hit:
            auto_pass = False
            detail = f"匹配关键词与类目名不一致（关键词: {','.join(matched_keywords[:3])}，类目: {hs.get('category_cn_name') or '—'}）"

    return {
        "passed": ok,
        "detail": detail,
        "score": round(score, 3),
        "auto_pass_eligible": auto_pass,
    }

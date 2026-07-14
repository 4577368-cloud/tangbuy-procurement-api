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
    ("猫窝", "狗窝", "宠物窝", "宠物床", "猫屋", "狗屋"),
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


def _chinese_terms(text: str, *, min_len: int = 2) -> list[str]:
    return re.findall(rf"[\u4e00-\u9fff]{{{min_len},8}}", text or "")


def hs_code_usable(hs: dict[str, Any]) -> bool:
    code = str(hs.get("hs_code") or "").strip().lower()
    if not code or code in ("nan", "none", "null", "-"):
        return False
    digits = re.sub(r"\D", "", code)
    return len(digits) >= 8


def catalog_leaf_incoherent(hs: dict[str, Any]) -> bool:
    """类目名与申报名像两种完全不同商品（如「鱼」vs「宠物用品」）。"""
    cn = str(hs.get("category_cn_name") or "").strip()
    dec = str(hs.get("declare_cn_name") or "").strip()
    if not cn or not dec:
        return False
    if cn.lower() in ("nan",) or dec.lower() in ("nan",):
        return False
    if cn == dec or cn in dec or dec in cn:
        return False
    cn_terms = set(_chinese_terms(cn))
    dec_terms = set(_chinese_terms(dec))
    if cn_terms & dec_terms:
        return False
    # 短叶节点名与长申报名无共同词、互不包含 → 脏绑
    if len(cn) <= 3 and len(dec) >= 3 and not any(t in dec for t in cn_terms):
        return True
    return False


def title_vision_agreement_terms(
    title: str,
    vision_keywords: Optional[list[str]] = None,
) -> list[str]:
    """标题 ∩ 识图关键词（子串互含），长词优先。"""
    title_l = (title or "").strip().lower()
    if not title_l or not vision_keywords:
        return []
    hits: list[str] = []
    seen: set[str] = set()
    for raw in vision_keywords:
        kw = str(raw or "").strip()
        if len(kw) < 2:
            continue
        kl = kw.lower()
        if kl in title_l or any(t in kl for t in _chinese_terms(title_l) if len(t) >= 2 and t in kl):
            if kl not in seen:
                seen.add(kl)
                hits.append(kw)
            continue
        # 关键词片段落在标题
        for part in _chinese_terms(kw):
            if part in title_l and part not in seen:
                seen.add(part)
                hits.append(part)
    hits.sort(key=len, reverse=True)
    return hits


def title_vision_intent_conflict(
    title: str,
    vision_keywords: Optional[list[str]] = None,
) -> bool:
    """标题与识图落入不同意图组 → 需人工，不能图片单方面赢。"""
    if not vision_keywords:
        return False
    title_intents = _find_intent_terms(title or "")
    vision_intents = _find_intent_terms(_blob(*(vision_keywords or [])))
    if not title_intents or not vision_intents:
        return False
    tg = {_intent_group(t) for t in title_intents}
    vg = {_intent_group(t) for t in vision_intents}
    tg.discard(None)
    vg.discard(None)
    return bool(tg and vg and tg.isdisjoint(vg))


def _category_name_blob(hs: dict[str, Any]) -> str:
    return _blob(hs.get("category_cn_name"), hs.get("category_en_name"))


def _declare_blob(hs: dict[str, Any]) -> str:
    return _blob(hs.get("declare_cn_name"), hs.get("declare_en_name"))


def hs_aligns_with_agreement_terms(
    hs: dict[str, Any],
    agreement_terms: list[str],
) -> bool:
    """印证词须落在类目名上；仅命中申报名不够（防「鱼/宠物用品」）。"""
    if not agreement_terms:
        return False
    cat = _category_name_blob(hs)
    if not cat.strip():
        return False
    for term in agreement_terms:
        t = term.strip().lower()
        if len(t) >= 2 and t in cat:
            return True
        for part in _chinese_terms(term):
            if part in cat:
                return True
    return False


def mapping_aligns_with_title(
    title: str,
    hs: dict[str, Any],
    *,
    vision_keywords: Optional[list[str]] = None,
) -> tuple[bool, str, float]:
    """返回 (是否通过, 说明, 0~1 分数)。"""
    title_only = _blob(title)
    title_blob = _blob(title, *(vision_keywords or []))
    cat_name_blob = _category_name_blob(hs)
    declare_blob = _declare_blob(hs)
    cat_blob = _blob(cat_name_blob, declare_blob)
    if not title_blob.strip():
        return False, "缺少商品标题", 0.0
    if not cat_blob.strip():
        return False, "缺少报关品类字段", 0.0

    if catalog_leaf_incoherent(hs):
        return (
            False,
            f"类目名「{hs.get('category_cn_name') or '—'}」与申报名「{hs.get('declare_cn_name') or '—'}」不一致",
            0.08,
        )

    if has_obvious_conflict(title_blob, cat_blob):
        return False, f"标题与类目存在明显跨类冲突（{hs.get('category_cn_name') or '—'}）", 0.05

    # 有识图时：优先标题∩识图印证，且印证词须落在类目名（非仅申报名）
    agreement = title_vision_agreement_terms(title, vision_keywords)
    if vision_keywords:
        if title_vision_intent_conflict(title, vision_keywords):
            return False, "标题与识图意图冲突，需人工确认", 0.1
        if agreement and hs_aligns_with_agreement_terms(hs, agreement):
            return True, f"标题与识图印证「{'/'.join(agreement[:3])}」对齐类目名", 0.92
        if agreement and any(t.lower() in declare_blob for t in agreement if len(t) >= 2):
            # 仅申报名命中、类目名无关 → 不通过
            return (
                False,
                f"印证词「{'/'.join(agreement[:2])}」仅命中申报名，类目名「{hs.get('category_cn_name') or '—'}」不匹配",
                0.2,
            )

    title_intents = _find_intent_terms(title_only or title_blob)
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

    # 子串互含：优先类目名，其次整包（无识图时保留原行为）
    scan_blob = cat_name_blob if cat_name_blob.strip() else cat_blob
    for term in sorted(set(_chinese_terms(title_only or title_blob)), key=len, reverse=True):
        if term in scan_blob or any(term in c or c in term for c in _chinese_terms(scan_blob)):
            return True, f"标题词「{term}」与类目相关", 0.72

    if vision_keywords and not agreement:
        return False, "标题与识图关键词未印证", 0.22

    if any(kw in cat_blob for kw in (vision_keywords or []) if kw and len(kw) >= 2):
        # 无标题印证时，识图词只能辅助说明，不足以单独过关
        return False, "仅识图命中类目、缺少标题印证", 0.35

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

    if auto_pass and not hs_code_usable(hs):
        auto_pass = False
        detail = f"类目「{hs.get('category_cn_name') or '—'}」无有效 HS，不能自动放行"

    if auto_pass and catalog_leaf_incoherent(hs):
        auto_pass = False
        detail = (
            f"类目名「{hs.get('category_cn_name') or '—'}」与申报名"
            f"「{hs.get('declare_cn_name') or '—'}」不一致，不能自动放行"
        )

    # 有识图时：必须标题∩识图有印证，且印证对齐类目名
    if auto_pass and vision_keywords:
        agreement = title_vision_agreement_terms(title, vision_keywords)
        if not agreement:
            auto_pass = False
            detail = "标题与识图关键词未印证，不能自动放行"
        elif not hs_aligns_with_agreement_terms(hs, agreement):
            auto_pass = False
            detail = (
                f"印证词「{'/'.join(agreement[:3])}」未对齐类目名"
                f"「{hs.get('category_cn_name') or '—'}」"
            )
        if title_vision_intent_conflict(title, vision_keywords):
            auto_pass = False
            detail = "标题与识图意图冲突，不能自动放行"

    # 关键词匹配类方法：校验 matched_keywords 是否真的出现在类目名中
    # 防止「标题语义词「石槽」与类目「路由器」一致」这种跨类误匹配被自动放行
    if auto_pass and method.startswith("keyword") and matched_keywords:
        cat_blob = _category_name_blob(hs) or _blob(
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
        "agreement_terms": title_vision_agreement_terms(title, vision_keywords) if vision_keywords else [],
    }

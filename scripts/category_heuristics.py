#!/usr/bin/env python3
"""品类映射启发式：从类目库自动归纳 + 人工反馈持续学习。

原则（泛化宠物/哺乳/文胸等 case）：
1. 申报名碰撞：同一 dec_cn_name 挂多个叶子类目 → 该词作「泛父类」，不能单独定类。
2. 类目名锚点：cn_name 相对 dec 多出的词（如 哺乳、宠物）→ 标题/识图无证据则降权。
3. 人工纠错：错选类目上的锚点词未在标题出现 → 沉淀为 learned penalty。
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "category"
HEURISTICS_FILE = DATA / "mapping-heuristics.json"
FEEDBACK_FILE = DATA / "feedback.jsonl"

# 人工种子（类目库扫不到的常识）
MANUAL_GENERIC_PARENT_TERMS = {
    "女装", "男装", "童装", "服装", "女士", "男士", "中老年", "亲子", "精品",
    "内衣", "内衣物",
}

MANUAL_DOMAIN_MARKERS: list[dict] = [
    {
        "domain": "宠物",
        "markers": ["宠物", "猫", "狗", "犬", "宠"],
        "human_context": ["女士", "男士", "男款", "女款", "学生", "户外", "旅游", "通勤", "商务"],
    },
    {
        "domain": "婴儿",
        "markers": ["婴儿", "宝宝", "幼儿", "婴童"],
        "human_context": ["女士", "男士", "成人", "学生", "户外"],
    },
    {
        "domain": "儿童",
        "markers": ["儿童", "童装", "童款"],
        "human_context": ["女士", "男士", "成人"],
    },
    {
        "domain": "哺乳",
        "markers": ["哺乳", "喂奶", "喂奶衣", "产后", "孕哺", "哺乳期", "月子", "产检"],
        "human_context": [],
    },
    {
        "domain": "孕哺",
        "markers": ["孕妇", "产妇", "孕款", "孕妈"],
        "human_context": [
            "袜", "长袜", "短袜", "连裤袜", "鞋", "凉鞋", "拖鞋", "靴",
            "衣", "服", "裙", "裤", "帽", "包", "被", "毯", "枕",
        ],
    },
    {
        "domain": "牲畜",
        "markers": ["羊羔", "羔羊", "小羊", "种羊", "活羊", "山羊", "绵羊"],
        "human_context": [
            "袜", "绒", "毛", "衣", "服", "鞋", "包", "帽", "被", "毯", "围巾", "手套",
        ],
    },
]

# 锚点词过泛时不参与「必须有标题证据」
ANCHOR_STOPWORDS = {
    "用品", "配件", "其他", "通用", "系列", "款式", "精品", "定制", "专用", "套装",
    "男女", "成人", "儿童", "时尚", "经典", "新款",
}


def cn_specialty_tokens(cn: str, dec: str = "") -> list[str]:
    """类目中文名相对申报名多出来的语义锚点（如 哺乳吊带 / 内衣 → 哺乳、吊带）。"""
    cn = (cn or "").strip()
    dec = (dec or "").strip()
    if not cn:
        return []

    out: list[str] = []
    dec_set = set(re.findall(r"[\u4e00-\u9fff]{2,6}", dec))
    cn_set = set(re.findall(r"[\u4e00-\u9fff]{2,6}", cn))

    for part in re.split(r"[/／>｜|]", cn):
        part = part.strip()
        if len(part) >= 2 and part not in dec_set and part not in ANCHOR_STOPWORDS:
            out.append(part)

    for tok in cn_set - dec_set:
        if tok in ANCHOR_STOPWORDS or tok in MANUAL_GENERIC_PARENT_TERMS:
            continue
        if tok not in out:
            out.append(tok)

    return list(dict.fromkeys(out))[:8]


def build_heuristics_from_catalog(catalog_list: list[dict]) -> dict:
    """从 HS 类目表归纳申报名碰撞、泛父类词、每类目锚点。"""
    dec_to_cids: dict[str, set[int]] = defaultdict(set)
    dec_to_cn: dict[str, set[str]] = defaultdict(set)
    specialty_by_cid: dict[str, list[str]] = {}

    for entry in catalog_list:
        cid = entry.get("cid")
        cn = str(entry.get("cn_name") or "").strip()
        dec = str(entry.get("dec_cn_name") or "").strip()
        if not cid or not dec:
            continue
        dec_to_cids[dec].add(int(cid))
        if cn:
            dec_to_cn[dec].add(cn)
        specialty_by_cid[str(cid)] = cn_specialty_tokens(cn, dec)

    declare_collision: dict[str, dict] = {}
    auto_generic: set[str] = set(MANUAL_GENERIC_PARENT_TERMS)

    for dec, cids in dec_to_cids.items():
        distinct_cn = dec_to_cn.get(dec, set())
        if len(cids) >= 5 and len(distinct_cn) >= 4 and len(dec) >= 2:
            declare_collision[dec] = {
                "cid_count": len(cids),
                "cn_count": len(distinct_cn),
            }
            auto_generic.add(dec)

    return {
        "declare_collision_terms": declare_collision,
        "generic_parent_terms": sorted(auto_generic),
        "specialty_by_cid": specialty_by_cid,
        "version": 1,
    }


@lru_cache(maxsize=1)
def load_mapping_heuristics() -> dict:
    if HEURISTICS_FILE.exists():
        try:
            return json.loads(HEURISTICS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    catalog_path = DATA / "catalog.json"
    if not catalog_path.exists():
        return build_heuristics_from_catalog([])
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    return build_heuristics_from_catalog(catalog.get("list") or [])


def generic_parent_terms() -> set[str]:
    h = load_mapping_heuristics()
    return set(h.get("generic_parent_terms") or []) | MANUAL_GENERIC_PARENT_TERMS


def declare_collision_terms() -> set[str]:
    h = load_mapping_heuristics()
    return set((h.get("declare_collision_terms") or {}).keys())


def specialty_for_cid(cid: int | str) -> list[str]:
    h = load_mapping_heuristics()
    return list((h.get("specialty_by_cid") or {}).get(str(cid)) or [])


def _title_blob(title: str, vision_keywords: list[str] | None = None) -> str:
    parts = [title or ""]
    if vision_keywords:
        parts.extend(vision_keywords)
    return " ".join(parts)


def load_learned_anchor_penalties(catalog_by_cid: dict | None = None) -> dict[str, float]:
    """从 feedback.jsonl 学习：错选类目上的锚点词未在标题出现 → 对该锚点加强惩罚。"""
    penalties: dict[str, float] = {}
    if not FEEDBACK_FILE.exists():
        return penalties

    for line in FEEDBACK_FILE.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue

        orig_id = str(row.get("original_category_id") or "")
        corrected_id = str(row.get("corrected_category_id") or "")
        if not orig_id:
            continue
        if corrected_id and corrected_id == orig_id and not row.get("rejected"):
            continue

        title = str(row.get("source_title") or "")
        hint = str(row.get("source_category_hint") or "")
        blob = _title_blob(title, row.get("vision_keywords") or [])
        kws = row.get("matched_keywords") or []
        for kw in kws:
            if kw:
                blob += f" {kw}"
        try:
            from category_mapper import dominant_product_signals

            for term, _ in dominant_product_signals(title, hint)[:4]:
                blob += f" {term}"
        except Exception:
            pass

        wrong_anchors = row.get("wrong_specialty_tokens")
        if not wrong_anchors and catalog_by_cid and orig_id in catalog_by_cid:
            orig = catalog_by_cid[orig_id]
            wrong_anchors = cn_specialty_tokens(
                str(orig.get("cn_name") or ""),
                str(orig.get("dec_cn_name") or ""),
            )
        elif not wrong_anchors:
            wrong_anchors = specialty_for_cid(orig_id)

        for anchor in wrong_anchors or []:
            if len(anchor) < 2 or anchor in blob:
                continue
            penalties[anchor] = min(penalties.get(anchor, 0.22), 0.12)

    return penalties


def expand_evidence_terms(title: str, vision_keywords: list[str] | None = None) -> list[str]:
    """标题/识图里出现的领域词展开（如 哺乳期 → 哺乳），补进语义词池。"""
    blob = _title_blob(title, vision_keywords)
    found: list[str] = []
    for rule in MANUAL_DOMAIN_MARKERS:
        for m in rule["markers"]:
            if m in blob and m not in found:
                found.append(m)
        if rule["domain"] in blob and rule["domain"] not in found:
            found.append(rule["domain"])
    return found


def domain_alignment_bonus(
    title: str,
    vision_keywords: list[str] | None,
    cn_name: str,
    dec_cn_name: str = "",
) -> float:
    """标题/识图已体现某领域时，类目名也对齐该领域 → 加分。"""
    blob = _title_blob(title, vision_keywords)
    bonus = 0.0
    cat = f"{cn_name} {dec_cn_name}"
    for rule in MANUAL_DOMAIN_MARKERS:
        if not any(m in blob for m in rule["markers"]) and rule["domain"] not in blob:
            continue
        if rule["domain"] in cat or any(m in cat for m in rule["markers"]):
            bonus += 0.28
    return bonus


def seed_domain_multiplier(title: str, cn_name: str, dec_cn_name: str = "") -> float:
    blob = f"{cn_name} {dec_cn_name}"
    title_l = title or ""
    for rule in MANUAL_DOMAIN_MARKERS:
        domain = rule["domain"]
        if domain not in blob:
            continue
        if any(m in title_l for m in rule["markers"]):
            return 1.0
        if any(m in title_l for m in rule["human_context"]):
            return 0.1
        return 0.2
    return 1.0


def evidence_multiplier(
    title: str,
    cn_name: str,
    dec_cn_name: str = "",
    vision_keywords: list[str] | None = None,
    learned_anchors: dict[str, float] | None = None,
) -> float:
    """标题/识图对类目锚点与领域词的证据强度 0.1~1.0。"""
    blob = _title_blob(title, vision_keywords)
    mult = 1.0

    mult = min(mult, seed_domain_multiplier(title, cn_name, dec_cn_name))

    anchors = cn_specialty_tokens(cn_name, dec_cn_name)
    missing = [a for a in anchors if a not in blob]
    if anchors and len(missing) == len(anchors):
        mult = min(mult, 0.18)
    elif missing and len(missing) >= 2:
        mult = min(mult, 0.35)

    learned = learned_anchors or {}
    for anchor in anchors:
        if anchor in learned and anchor not in blob:
            mult = min(mult, learned[anchor])

    return mult


def declare_only_penalty(term: str, cn: str, dec: str) -> float:
    """语义词仅命中申报名、未命中类目名，且该申报名为碰撞词 → 额外降权。"""
    if term not in declare_collision_terms():
        return 1.0
    cn_has = term in (cn or "")
    dec_has = term in (dec or "")
    if dec_has and not cn_has:
        return 0.55
    return 1.0


def label_fit_adjustment(term: str, cn: str, dec: str) -> float:
    """类目名贴合度乘子：申报名蹭词、锚点无证据等。"""
    if not term:
        return 1.0
    mult = 1.0
    if term == cn or term == dec:
        return 1.0
    cn_has = term in (cn or "")
    dec_only = (term in (dec or "")) and not cn_has
    if dec_only:
        mult *= declare_only_penalty(term, cn, dec)
    return mult

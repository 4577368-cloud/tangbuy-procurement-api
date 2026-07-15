#!/usr/bin/env python3
"""
品类映射 Agent CLI — 基于 HS 类目表 + 历史商品映射做建议。

用法:
  python3 category_mapper.py suggest --title "无线蓝牙耳机" [--hint "耳机"] [--goods-id "815526992410"] [--image-url "..."]
  python3 category_mapper.py lookup --cid 50006121
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path

from category_heuristics import (
    domain_alignment_bonus,
    evidence_multiplier,
    expand_evidence_terms,
    generic_parent_terms,
    label_fit_adjustment,
    load_learned_anchor_penalties,
    lookup_history_conventions_for_text,
    seed_domain_multiplier,
    term_catalog_extension_mismatch,
    term_catalog_extension_penalty,
)
from pending_conventions import (
    lookup_goods_id_soft,
    lookup_pending_conventions_for_text,
)

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "category"

# 属性/工艺/装饰/版型/营销词：出现在标题里但不是「海关申报品类」本身。
# 命中类目表也不应作为主品类候选（例如「刺绣」会命中织绣类目，但商品其实是连衣裙）。
ATTRIBUTE_TERMS = {
    "刺绣", "绣花", "印花", "镶钻", "钉珠", "烫钻", "水钻", "玻璃钻", "亮片", "珠片",
    "流苏", "蕾丝", "网纱", "雪纺", "拼接", "拼色", "不对称", "波点", "条纹", "格子",
    "碎花", "褶皱", "镂空", "勾花", "钩花", "打底", "两件", "套装",
    "宽松", "修身", "显瘦", "收腰", "高腰", "气质", "优雅", "复古", "时尚", "百搭",
    "新款", "爆款", "热卖", "潮款", "欧美", "跨境", "外贸", "中东", "阿拉", "穆斯林",
    "长袖", "短袖", "中袖", "无袖", "七分", "高领", "圆领", "立领", "翻领", "方领",
    "黑色", "白色", "红色", "蓝色", "绿色", "灰色", "粉色", "米色", "卡其", "驼色",
}

# 场景/用途修饰：标题常见但不是申报品类（如「户外运动胸包」里的户外运动）
SCENE_DESCRIPTOR_TERMS = {
    "户外运动", "休闲运动", "运动休闲", "休闲旅游", "健身运动", "户外休闲",
    "居家", "室内", "室外", "旅行", "旅游", "运动",
}

# 地域/风格修饰：不是商品品类（如「非洲辫假发」里的非洲 → 非洲鼓）
GEOGRAPHIC_REGION_TERMS = {
    "非洲", "欧洲", "亚洲", "美洲", "大洋洲", "北欧", "南美", "北美", "东亚", "西欧",
    "东南亚", "中东", "日式", "韩式", "欧美", "法式", "英式", "美式", "中式", "港风",
    "非洲风", "波西米亚",
}

# 主题/系列修饰：标题里常作氛围词，会误撞同名词类目（如「海洋系列吊坠」→ 海洋·教育书）
THEME_SERIES_TERMS = {
    "海洋", "星空", "宇宙", "梦幻", "童话", "森系", "田园", "复古风", "民族风",
    "卡通", "动漫", "宫廷", "欧式", "北欧风", "热带", "沙滩", "海边", "雪地",
    "圣诞", "节日", "节日系", "海洋系", "星空系",
}

# 包袋类商品名词（长词优先匹配，避免「肩胸包斜跨包」整段误识别）
BAG_NOUN_PATTERNS = (
    "双肩背包", "单肩背包", "斜挎包", "斜跨包", "胸包", "腰包", "手提包",
    "单肩包", "双肩包", "背包", "挎包", "手包", "拎包", "休闲包", "运动包", "配件包",
)

# 标题词 → 类目检索别名（如「胸包」叶子类目缺 HS，回退到「斜挎包」等同 HS 叶子）
TERM_CATALOG_ALIASES: dict[str, list[str]] = {
    "胸包": ["胸包", "斜挎包", "斜跨包", "单肩包", "腰包", "挎包"],
    "斜跨包": ["斜挎包", "斜跨包", "胸包", "单肩包"],
    "斜挎包": ["斜挎包", "斜跨包", "胸包"],
    # 石器类：标题常见词在类目库中无直接对应，回退到有 HS 编码的石器类目
    "石槽": ["石雕", "仿古石器", "石器", "石制品"],
    "石磨": ["石雕", "仿古石器", "石器", "石制品"],
    "石磨盘": ["石雕", "仿古石器", "石器", "石制品"],
    "老石器": ["仿古石器", "石雕", "石器", "石制品"],
    "青石板": ["石雕", "大理石制品", "仿古石器", "石制品"],
    "石盆": ["石雕", "仿古石器", "石器", "石制品"],
    "石臼": ["石雕", "仿古石器", "石器", "石制品"],
    "石灯笼": ["石雕", "仿古石器", "石器", "石制品"],
    "石条": ["石雕", "大理石制品", "仿古石器", "石制品"],
    "石缸": ["石雕", "仿古石器", "石器", "石制品"],
    "石钵": ["石雕", "仿古石器", "石器", "石制品"],
    "石桌": ["大理石餐桌", "石雕", "仿古石器", "石制品"],
    "石凳": ["大理石餐桌", "石雕", "仿古石器", "石制品"],
    # 假发类：避免「发套」误撞沙发垫类目
    "发套": ["假发", "发套", "假发套", "美发", "接发"],
    "假发": ["假发", "发套", "美发", "接发", "假发套"],
}

# 泛父类词：见 category_heuristics（类目库自动归纳 + 人工种子）

# 领域修饰种子：category_heuristics.MANUAL_DOMAIN_MARKERS；运行时用锚点证据 + 反馈学习泛化

# 标题噪声：营销/材质/场景堆砌词，不参与抽品类语义词
TITLE_SEMANTIC_NOISE = {
    "男士",
    "女士",
    "男款",
    "女款",
    "新款",
    "潮款",
    "帅气",
    "百搭",
    "时尚",
    "夏季",
    "冬天",
    "春天",
    "秋天",
    "黑色",
    "白色",
    "红色",
    "蓝色",
    "绿色",
    "超软",
    "防滑",
    "户外",
    "两穿",
    "夹趾",
    "设计",
    "橡胶",
    "织物",
    "夏天",
    "潮款帅",
    "款帅气",
    "士凉鞋",
    "士夏季",
    "几何",
    "图案",
    "牛津布",
    "批发",
    "量大",
    "跨境",
    "欧美",
    "旅游",
    "新款",
    "加厚",
    "纯色",
    "休闲",
    "潮流",
    "经典",
    "简约",
    "大容量",
    # 非商品词/营销词，不参与品类语义
    "测试",
    "形象",
    "模特",
    "代言",
    "广告",
    "展示",
    "样品",
    "样衣",
    "参考",
    "图片",
    # 人群/场景修饰，不是申报品类本身
    "孕妇",
    "产妇",
    "孕妈",
    "婴童",
    "成人",
    "学生",
    # 系列/版型修饰，不是申报品类
    "系列",
    "混装",
    "工厂",
    "直销",
}

# 人群/受众词：出现在标题里但不是商品品类（如「孕妇款长袜」里的孕妇）
AUDIENCE_DESCRIPTOR_TERMS = {
    "孕妇", "产妇", "孕妈", "哺乳", "月子", "孕", "男士", "女士", "男款", "女款",
    "成人", "儿童", "婴童", "学生", "中老年", "青少年",
}

# 动物词：单独出现不是商品（羊绒袜里的「羊绒/羊羔绒」另当别论）
ANIMAL_DESCRIPTOR_TERMS = {
    "羊羔", "羔羊", "小羊", "种羊", "活羊", "山羊", "绵羊", "牛犊", "仔猪", "雏鸡",
}

# 饰品/配件主商品词（避免「项链手链挂件」整段被当成单个主词）
JEWELRY_PRODUCT_TERMS = {
    "项链", "手链", "脚链", "挂件", "吊坠", "饰品", "饰品配件", "配饰",
    "耳环", "耳钉", "耳坠", "手镯", "戒指", "胸针", "吊饰", "珠串",
}

# 英文标题 → 中文商品词干（用于频次统计）
EN_PRODUCT_NOUN_MAP = {
    "socks": "袜",
    "sock": "袜",
    "stockings": "袜",
    "stocking": "袜",
    "pantyhose": "袜",
    "tights": "袜",
}


def domain_conflict_multiplier(title: str, cn_name: str, dec_cn_name: str = "", vision_keywords: list[str] | None = None) -> float:
    """兼容旧名：委托给启发式模块（锚点证据 + 领域种子 + 反馈学习）。"""
    catalog, _, _, _ = load_data()
    learned = load_learned_anchor_penalties(catalog.get("by_cid"))
    return evidence_multiplier(title, cn_name, dec_cn_name, vision_keywords, learned)


def clamp_conf(x: float) -> float:
    """置信度一律归一到 [0, 0.98]，避免出现 >100% 的展示与写库。"""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    return round(max(0.0, min(0.98, v)), 3)


def score_to_confidence(score: float) -> float:
    """把无上界的内部排序分压成 0-1 置信度（兜底，主路径用 dimensions）。"""
    return clamp_conf(max(0.0, float(score)) / 1.3)


# 候选品类置信度的参与维度（加权和 = 展示/决策用 confidence）
DIMENSION_WEIGHTS = {
    "term_match": 0.14,
    "term_frequency": 0.12,
    "label_fit": 0.20,
    "catalog_fit": 0.18,
    "term_specificity": 0.12,
    "vision": 0.12,
    "hint": 0.08,
    "specificity": 0.06,
    "feedback": 0.04,
}

DIMENSION_COMPARE_KEYS = (
    "term_match",
    "term_frequency",
    "label_fit",
    "catalog_fit",
    "term_specificity",
    "vision",
    "hint",
    "specificity",
    "feedback",
)


def category_label_fit(term: str, cn: str, dec: str, title: str = "") -> float:
    """语义词与类目名的贴合：完全命中具体类目 > 泛词蹭进长路径 > 申报名碰撞降权。"""
    if not term:
        return 0.0
    base = 0.15
    if term == cn or term == dec:
        base = 1.0
    elif cn.startswith(term) or (dec and dec.startswith(term)):
        if term_catalog_extension_mismatch(term, cn, dec, title):
            base = 0.24
        else:
            base = 0.92
    elif term in cn or (dec and term in dec):
        host = cn if term in cn else dec
        extra = max(0, len(host) - len(term))
        base = max(0.3, 0.88 - extra * 0.045)
    return round(base * label_fit_adjustment(term, cn, dec), 3)


def term_frequency_score(term: str, title: str, hint: str = "") -> float:
    """标题中商品词出现次数越多，置信度越高（人群/动物修饰词不适用）。"""
    if term in AUDIENCE_DESCRIPTOR_TERMS or term in ANIMAL_DESCRIPTOR_TERMS:
        return 0.2
    blob = f"{title or ''} {hint or ''}"
    count = blob.count(term)
    for en, zh in EN_PRODUCT_NOUN_MAP.items():
        if zh == term or term in zh or zh in term:
            count += len(re.findall(rf"\b{en}\b", (title or "").lower()))
    if count <= 0:
        return 0.0
    return round(min(1.0, 0.45 + 0.18 * min(count - 1, 4)), 3)


def dominant_product_signals(title: str, hint: str = "") -> list[tuple[str, int]]:
    """从标题+平台类目提取主商品词及频次。"""
    counts: dict[str, int] = {}
    blob = title or ""
    for noun in extract_product_nouns(blob):
        counts[noun] = counts.get(noun, 0) + blob.count(noun)
    for noun in sorted(JEWELRY_PRODUCT_TERMS, key=len, reverse=True):
        if noun in blob:
            counts[noun] = counts.get(noun, 0) + blob.count(noun)
    for en, zh in EN_PRODUCT_NOUN_MAP.items():
        n = len(re.findall(rf"\b{en}\b", blob.lower()))
        if n:
            counts[zh] = counts.get(zh, 0) + n
    for term in re.findall(r"[\u4e00-\u9fff]{2,6}", blob):
        if term in TITLE_SEMANTIC_NOISE or term in AUDIENCE_DESCRIPTOR_TERMS:
            continue
        if term in SCENE_DESCRIPTOR_TERMS:
            continue
        if term in ANIMAL_DESCRIPTOR_TERMS and "绒" not in blob[max(0, blob.find(term) - 1) : blob.find(term) + len(term) + 1]:
            continue
        if any(term in noun or noun in term for noun in counts):
            continue
        if term.endswith(("袜", "鞋", "靴", "帽", "包", "裙", "裤", "衣", "服", "被", "毯", "枕", "巾")):
            counts[term] = counts.get(term, 0) + blob.count(term)
        elif term in ("袜", "鞋", "帽", "包"):
            counts[term] = counts.get(term, 0) + blob.count(term)
    if hint:
        for term in re.findall(r"[\u4e00-\u9fff]{2,6}", hint):
            if term not in TITLE_SEMANTIC_NOISE:
                counts[term] = counts.get(term, 0) + 3
    return sorted(counts.items(), key=lambda x: (-x[1], -len(x[0])))


def term_specificity_bonus(term: str, all_terms: list[str]) -> float:
    """同标题存在更长的具体语义词时，短泛词降权（如 双肩包 存在则 背包 降权）。"""
    if not term or not all_terms:
        return 1.0
    longer = [t for t in all_terms if t != term and len(t) > len(term) and term in t]
    if longer:
        return 0.5
    if any(len(t) > len(term) for t in all_terms if t != term):
        return 0.72
    return 1.0


def term_position_bonus(term: str, title: str) -> float:
    """标题越靠前出现，越可能是主商品词（中文电商标题惯例）。"""
    if not term or not title:
        return 1.0
    pos = title.find(term)
    if pos < 0:
        return 1.0
    # 前 6 个字符为黄金位置；越靠后递减
    if pos == 0:
        return 1.12
    if pos <= 3:
        return 1.08
    if pos <= 6:
        return 1.04
    return 1.0


def compute_candidate_dimensions(
    term: str,
    title: str,
    title_tokens: set[str],
    hint: str,
    vision_keywords: list[str],
    entry: dict,
    boost: float,
    kind: str,
    all_terms: list[str] | None = None,
) -> dict[str, float]:
    cn = entry.get("cn_name", "")
    dec = entry.get("dec_cn_name", "")
    catalog_fit = min(1.0, score_catalog_entry(title, title_tokens, hint, entry))
    domain = domain_conflict_multiplier(title, cn, dec, vision_keywords)
    catalog_fit = min(1.0, catalog_fit * domain)
    spec = {"specific": 1.0, "generic": 0.35, "attribute": 0.15}.get(kind, 0.5)
    # 制服/演出领域：即使语义词被归为泛词，只要类目属于该领域且标题有强信号，就按具体品类对待
    uniform_bonus = uniform_domain_bonus(title, cn, dec)
    if uniform_bonus > 1.0 and kind == "generic":
        spec = min(1.0, spec + 0.35)

    term_in_title = term in (title or "")
    term_in_cat = term in cn or term in dec
    freq = term_frequency_score(term, title, hint)
    return {
        "term_match": round(1.0 if term_in_title else (0.65 if term_in_cat else 0.0), 3),
        "term_frequency": freq,
        "label_fit": round(
            category_label_fit(term, cn, dec, title) * term_position_bonus(term, title), 3
        ),
        "catalog_fit": round(min(1.0, catalog_fit * uniform_bonus), 3),
        "term_specificity": round(term_specificity_bonus(term, all_terms or []), 3),
        "vision": round(1.0 if term in vision_keywords else 0.0, 3),
        "hint": round(1.0 if hint and term == hint else 0.0, 3),
        "specificity": round(spec, 3),
        "feedback": round(clamp_conf(max(0.0, boost) / 0.4), 3),
    }


def uniform_domain_bonus(title: str, cn_name: str, dec_cn_name: str = "") -> float:
    """标题含制服/职业装/演出等强领域信号时，对齐该类目的候选加分；
    西装/正装等词与制服场景冲突时降权。
    """
    if not title:
        return 1.0
    cat = f"{cn_name} {dec_cn_name}"
    uniform_indicators = {"制服", "工作服", "演出服", "舞台服装", "职业套装", "校服", "商务服", "工装"}
    occupation_indicators = {"海员", "演出", "保安", "护士", "医生", "厨师", "警察", "消防员", "空乘"}
    title_uniform = any(s in title for s in uniform_indicators | occupation_indicators)
    if not title_uniform:
        return 1.0

    cat_is_uniform = any(s in cat for s in uniform_indicators)
    cat_is_suit_only = ("西装" in cat or "西服" in cat or "正装" in cat) and not cat_is_uniform

    if cat_is_uniform:
        return 1.35
    if cat_is_suit_only:
        return 0.55
    return 1.0


def confidence_from_dimensions(dims: dict[str, float]) -> float:
    total = sum(float(dims.get(k, 0.0)) * w for k, w in DIMENSION_WEIGHTS.items())
    return clamp_conf(total)


def count_dimension_wins(top_dims: dict[str, float], second_dims: dict[str, float]) -> int:
    wins = 0
    for key in DIMENSION_COMPARE_KEYS:
        if float(top_dims.get(key, 0.0)) - float(second_dims.get(key, 0.0)) >= 0.08:
            wins += 1
    return wins


def compute_separation(top: dict, second: dict) -> dict:
    td = top.get("dimensions") or {}
    sd = second.get("dimensions") or {}
    return {
        "dim_wins": count_dimension_wins(td, sd),
        "score_gap": round(float(top.get("score", 0.0)) - float(second.get("score", 0.0)), 3),
        "conf_gap": round(
            float(top.get("confidence") or score_to_confidence(top.get("score", 0.0)))
            - float(second.get("confidence") or score_to_confidence(second.get("score", 0.0))),
            3,
        ),
    }


def classify_term(term: str) -> str:
    """term -> 'attribute' | 'generic' | 'specific'"""
    if term in ATTRIBUTE_TERMS:
        return "attribute"
    if term in SCENE_DESCRIPTOR_TERMS:
        return "attribute"
    if term in GEOGRAPHIC_REGION_TERMS:
        return "attribute"
    if term in THEME_SERIES_TERMS:
        return "attribute"
    if term in AUDIENCE_DESCRIPTOR_TERMS or term in ANIMAL_DESCRIPTOR_TERMS:
        return "attribute"
    if term in generic_parent_terms():
        return "generic"
    return "specific"


def tokenize(text: str) -> set[str]:
    text = (text or "").lower()
    tokens: set[str] = set()
    tokens.update(re.findall(r"[a-z]{2,}", text))
    han = re.sub(r"[^\u4e00-\u9fff]", "", text)
    for i in range(max(0, len(han) - 1)):
        tokens.add(han[i : i + 2])
    tokens.update(re.findall(r"[\u4e00-\u9fff]{2,6}", text))
    return {t for t in tokens if len(t) >= 2}


@lru_cache(maxsize=1)
def load_data():
    catalog = json.loads((DATA / "catalog.json").read_text(encoding="utf-8"))
    history = json.loads((DATA / "history.json").read_text(encoding="utf-8"))
    goods_id_index = json.loads((DATA / "goods-id-index.json").read_text(encoding="utf-8"))
    token_index = json.loads((DATA / "token-index.json").read_text(encoding="utf-8"))
    return catalog, history, goods_id_index, token_index


def lookup_cid(cid: int | str) -> dict | None:
    catalog, _, _, _ = load_data()
    return catalog["by_cid"].get(str(cid))


def valid_hs(code) -> bool:
    s = str(code or "").strip().lower().replace(".0", "")
    return bool(s and s != "nan" and re.match(r"^\d{8,12}$", s))


def enrich_category_fields(cat: dict, catalog: dict) -> dict:
    """部分类目行 hs_code 为空，尝试从同申报品名的其他行补齐。"""
    if valid_hs(cat.get("hs_code")) and cat.get("dec_cn_name", "").lower() != "nan":
        return cat
    dec = cat.get("dec_cn_name", "")
    if dec and str(dec).lower() != "nan":
        for entry in catalog["list"]:
            if entry.get("dec_cn_name") == dec and valid_hs(entry.get("hs_code")):
                return {
                    **cat,
                    "hs_code": entry["hs_code"],
                    "dec_cn_name": entry.get("dec_cn_name") or cat.get("dec_cn_name"),
                    "dec_en_name": entry.get("dec_en_name") or cat.get("dec_en_name"),
                }
    for entry in catalog["list"]:
        if entry.get("cn_name") == cat.get("cn_name") and valid_hs(entry.get("hs_code")):
            return {**cat, "hs_code": entry["hs_code"]}
    return cat


def pick_scored_with_hs(scored: list[tuple[float, dict]]) -> tuple[float, dict] | None:
    if not scored:
        return None
    for item in scored:
        if valid_hs(item[1].get("hs_code")):
            return item
    return scored[0]


ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "category"
FEEDBACK_FILE = DATA / "feedback.jsonl"


def load_feedback_boost() -> dict[str, float]:
    """人工复核/审计纠错沉淀：`关键词:category_id` -> 加权。

    正样本：纠正后的类目在该判别词上加权（下次更倾向选它）。
    负样本：被拒绝的原类目在该判别词上降权（下次不再误判到它）。
    这就是 agent 的持续自我训练来源。
    """
    boosts: dict[str, float] = {}
    if not FEEDBACK_FILE.exists():
        return boosts
    for line in FEEDBACK_FILE.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            cid = str(row.get("corrected_category_id", "") or "")
            orig = str(row.get("original_category_id", "") or "")
            rejected = bool(row.get("rejected"))
            confirmed = bool(row.get("confirmed"))
            kws = row.get("matched_keywords") or []
            title = str(row.get("source_title", "") or "")
            hint = str(row.get("source_category_hint", "") or "")

            if rejected and orig:
                for kw in kws:
                    if kw:
                        boosts[f"{kw}:{orig}"] = boosts.get(f"{kw}:{orig}", 0) - 0.35
                for term, _ in dominant_product_signals(title, hint)[:3]:
                    boosts[f"{term}:{orig}"] = boosts.get(f"{term}:{orig}", 0) - 0.28
                continue

            for kw in kws:
                if not kw:
                    continue
                if cid and not rejected:
                    weight = 0.15 if confirmed and orig == cid else 0.28
                    boosts[f"{kw}:{cid}"] = boosts.get(f"{kw}:{cid}", 0) + weight
                if orig and orig != cid:
                    key = f"{kw}:{orig}"
                    boosts[key] = boosts.get(key, 0) - 0.32

            if title and cid and not rejected:
                boosts[f"{title[:20]}:{cid}"] = boosts.get(f"{title[:20]}:{cid}", 0) + 0.1

            if cid and not rejected:
                for term, _ in dominant_product_signals(title, hint)[:3]:
                    boosts[f"{term}:{cid}"] = boosts.get(f"{term}:{cid}", 0) + 0.22
        except Exception:
            pass
    return boosts


def score_hint_match(hint: str, entry: dict) -> float:
    if not hint:
        return 0.0
    cn = entry.get("cn_name", "")
    dec = entry.get("dec_cn_name", "")
    hint_l = hint.lower()
    if hint in cn or hint in dec:
        return 0.95
    if hint_l in cn.lower() or hint_l in dec.lower():
        return 0.75
    ht = tokenize(hint)
    ct = tokenize(" ".join([cn, dec]))
    if not ht or not ct:
        return 0.0
    return len(ht & ct) / len(ht)


def score_title_match(title: str, entry: dict) -> float:
    tokens = tokenize(title)
    return score_catalog_entry(title, tokens, "", entry)


def extract_matched_keywords(title: str, hint: str, cat: dict) -> list[str]:
    found: list[str] = []
    cn, dec = cat.get("cn_name", ""), cat.get("dec_cn_name", "")
    en = (cat.get("en_name") or "").lower()
    for seg in re.findall(r"[\u4e00-\u9fff]{2,6}", title):
        if seg in cn or seg in dec or (hint and seg in hint):
            found.append(seg)
    for w in re.findall(r"[a-z]{2,}", title.lower()):
        if w in en:
            found.append(w)
    if hint and (hint in cn or hint in dec or hint in title):
        found.append(hint)
    for part in re.split(r"[/\s>]+", cn):
        if len(part) >= 2 and part in title:
            found.append(part)
    return list(dict.fromkeys(found))[:10]


def fuse_signals(title_s: float, hint_s: float, history_s: float, image_s: float = 0.0) -> dict:
    fused = min(
        0.98,
        title_s * 0.35 + hint_s * 0.2 + history_s * 0.25 + image_s * 0.2,
    )
    return {
        "title": round(title_s, 3),
        "platform_category": round(hint_s, 3),
        "history": round(history_s, 3),
        "image": round(image_s, 3),
        "fused": round(fused, 3),
    }


def pack_result(
    cid: int,
    confidence: float,
    match_method: str,
    match_detail: str,
    candidates: list[dict] | None = None,
    *,
    title: str = "",
    hint: str = "",
    signal_scores: dict | None = None,
    matched_keywords: list[str] | None = None,
    decision: str = "manual_suggested",
    history_hit: bool = False,
    skip_fusion: bool = False,
    semantic_candidates: list[dict] | None = None,
    title_image_agreement_keywords: list[str] | None = None,
    vision_keywords: list[str] | None = None,
    separation: dict | None = None,
) -> dict:
    catalog, _, _, _ = load_data()
    cat = lookup_cid(cid)
    if not cat:
        return {"success": False, "error": f"未知 category_id: {cid}"}
    cat = enrich_category_fields(cat, catalog)
    keywords = matched_keywords if matched_keywords is not None else extract_matched_keywords(title, hint, cat)
    if vision_keywords:
        keywords = list(dict.fromkeys([*keywords, *vision_keywords]))[:12]

    hint_s = score_hint_match(hint, cat) if hint else 0.0
    title_s = score_title_match(title, cat) if title else 0.0
    image_s = 0.82 if vision_keywords else 0.0

    if skip_fusion or history_hit:
        scores = {
            "title": round(title_s, 3),
            "platform_category": round(hint_s, 3) if hint else 0.0,
            "history": 1.0 if history_hit else 0.0,
            "image": round(image_s, 3),
            "fused": 1.0 if history_hit else round(confidence, 3),
        }
        fused = scores["fused"]
    else:
        scores = signal_scores or fuse_signals(title_s, hint_s, 0.0, image_s)
        fused = round(max(confidence, scores.get("fused", confidence)), 3)
        scores = {**scores, "fused": fused}

    return {
        "success": True,
        "category_id": cat["cid"],
        "category_cn_name": cat["cn_name"],
        "category_en_name": cat["en_name"],
        "hs_code": cat["hs_code"],
        "declare_cn_name": cat["dec_cn_name"],
        "declare_en_name": cat["dec_en_name"],
        "tariff": cat.get("tariff"),
        "confidence": round(fused, 3),
        "match_method": match_method,
        "match_detail": match_detail,
        "candidates": candidates or [],
        "matched_keywords": keywords,
        "signal_scores": scores,
        "decision": decision,
        "history_hit": history_hit,
        "skip_fusion": skip_fusion or history_hit,
        "semantic_candidates": semantic_candidates or [],
        "title_image_agreement_keywords": title_image_agreement_keywords or [],
        "vision_keywords": vision_keywords or [],
        "separation": separation,
    }


@lru_cache(maxsize=1)
def _catalog_semantic_terms() -> set[str]:
    catalog, _, _, _ = load_data()
    terms: set[str] = set()
    for entry in catalog["list"]:
        for field in ("cn_name", "dec_cn_name"):
            text = entry.get(field, "") or ""
            terms.update(re.findall(r"[\u4e00-\u9fff]{2,6}", text))
    return terms


def extract_product_nouns(title: str) -> list[str]:
    """从标题按长度优先抽取包袋等具体商品名词。"""
    title = title or ""
    found: list[str] = []
    for noun in sorted(BAG_NOUN_PATTERNS, key=len, reverse=True):
        if noun in title:
            found.append(noun)
    return list(dict.fromkeys(found))


def extract_title_semantics(title: str) -> list[str]:
    """从标题抽取可能指向不同品类的语义词。"""
    noise = TITLE_SEMANTIC_NOISE
    catalog_terms = _catalog_semantic_terms()
    title = title or ""
    product_nouns = extract_product_nouns(title)
    raw: list[str] = list(product_nouns)
    for n in (4, 3, 2):
        for i in range(max(0, len(title) - n + 1)):
            seg = title[i : i + n]
            if seg in noise or seg not in catalog_terms:
                continue
            if product_nouns and seg in SCENE_DESCRIPTOR_TERMS:
                continue
            if product_nouns and seg in GEOGRAPHIC_REGION_TERMS:
                continue
            if product_nouns and seg in THEME_SERIES_TERMS:
                continue
            raw.append(seg)

    # 英文商品词补入（如 socks → 袜）
    for en, zh in EN_PRODUCT_NOUN_MAP.items():
        if re.search(rf"\b{en}\b", title.lower()) and zh in catalog_terms:
            raw.append(zh)

    unique = sorted(set(raw), key=len, reverse=True)
    found: list[str] = []
    for seg in unique:
        if any(seg != longer and seg in longer for longer in unique if len(longer) > len(seg)):
            continue
        if seg not in found:
            found.append(seg)
    return found[:10]


def _catalog_pick_rank(
    term: str,
    entry: dict,
    learned_anchors: dict[str, float] | None = None,
    title: str = "",
) -> tuple:
    """同分候选：申报名/类目名直贴语义词 > 前缀蹭词 > 领域/锚点证据。"""
    cn = entry.get("cn_name", "") or ""
    dec = entry.get("dec_cn_name", "") or ""
    exact_cn = cn == term
    exact_dec = dec == term
    cn_has = term in cn
    dec_only = (term in dec) and not cn_has
    extension_bad = term_catalog_extension_mismatch(term, cn, dec, title)
    domain_penalty = seed_domain_multiplier("", cn, dec)
    anchor_penalty = evidence_multiplier("", cn, dec, None, learned_anchors)
    return (
        exact_cn or exact_dec,
        exact_dec,
        exact_cn,
        not extension_bad,
        cn_has and not exact_cn,
        not dec_only,
        domain_penalty,
        anchor_penalty,
    )


def _search_terms_for(term: str) -> list[str]:
    aliases = TERM_CATALOG_ALIASES.get(term, [])
    return list(dict.fromkeys([term, *aliases]))


def _best_catalog_for_term(
    term: str,
    title: str,
    title_tokens: set[str],
    hint: str,
    catalog: dict,
    vision_keywords: list[str],
    boosts: dict[str, float] | None = None,
    learned_anchors: dict[str, float] | None = None,
) -> tuple[float, dict] | None:
    boosts = boosts or {}
    learned_anchors = learned_anchors or {}
    best_score = 0.0
    best_entry: dict | None = None
    best_match_term = term
    scored: list[tuple[float, dict, str]] = []
    for match_term in _search_terms_for(term):
        for entry in catalog["list"]:
            cn = entry.get("cn_name", "")
            dec = entry.get("dec_cn_name", "")
            # 语义词必须出现在类目名/申报名中才参与打分，
            # 不能仅因出现在标题中就跨类匹配到不相关类目（如「石槽」→「路由器」）
            if match_term not in cn and match_term not in dec:
                continue
            s = score_catalog_entry(title, title_tokens, hint, entry)
            if match_term in cn or match_term in dec:
                s += 0.45
            if cn == match_term:
                s += 0.2
            if dec == match_term:
                s += 0.28
            if match_term == term:
                s += 0.08
            if match_term in vision_keywords or term in vision_keywords:
                s += 0.35
            s -= term_catalog_extension_penalty(match_term, cn, dec, title)
            s += domain_alignment_bonus(title, vision_keywords, cn, dec)
            # 反馈学习：该判别词对此类目的正/负加权
            s += boosts.get(f"{term}:{entry.get('cid')}", 0.0)
            scored.append((s, entry, match_term))
    if not scored:
        return None
    with_hs = [(s, e, t) for s, e, t in scored if valid_hs(e.get("hs_code"))]
    pool = with_hs if with_hs else scored
    best_score, best_entry, best_match_term = max(
        pool,
        key=lambda row: (
            row[0],
            valid_hs(row[1].get("hs_code")),
            _catalog_pick_rank(row[2], row[1], learned_anchors, title),
        ),
    )
    if best_score < 0.12:
        return None
    return best_score, best_entry


def rank_semantic_candidates(
    title: str,
    hint: str,
    vision_keywords: list[str],
    catalog: dict,
) -> list[dict]:
    title_tokens = tokenize(title + " " + (hint or ""))
    semantics = extract_title_semantics(title)
    evidence = expand_evidence_terms(title, vision_keywords)
    terms = list(dict.fromkeys([*semantics, *evidence, *vision_keywords, *( [hint] if hint else [] )]))
    boosts = load_feedback_boost()
    learned_anchors = load_learned_anchor_penalties(catalog.get("by_cid"))

    raw: list[dict] = []
    for term in terms:
        picked = _best_catalog_for_term(
            term, title, title_tokens, hint, catalog, vision_keywords, boosts, learned_anchors,
        )
        if not picked:
            continue
        score, entry = picked
        entry = enrich_category_fields(entry, catalog)
        sources: list[str] = []
        if term in semantics:
            sources.append("title")
        if term in vision_keywords:
            sources.append("vision")
        if hint and term == hint:
            sources.append("hint")
        kind = classify_term(term)
        boost = boosts.get(f"{term}:{entry.get('cid')}", 0.0)
        dims = compute_candidate_dimensions(
            term, title, title_tokens, hint, vision_keywords, entry, boost, kind, semantics
        )
        conf = confidence_from_dimensions(dims)
        cn_name = entry["cn_name"]
        dec_name = entry.get("dec_cn_name", "")
        reason = ""
        if term_catalog_extension_mismatch(term, cn_name, dec_name, title):
            reason = f"标题词「{term}」与报关类目「{cn_name}」不是同一商品，已降权"
        raw.append(
            {
                "label": term,
                "category_id": entry["cid"],
                "category_cn_name": cn_name,
                "category_en_name": entry.get("en_name", ""),
                "hs_code": entry.get("hs_code", ""),
                "declare_cn_name": entry.get("dec_cn_name", ""),
                "declare_en_name": entry.get("dec_en_name", ""),
                "score": round(score, 3),
                "confidence": conf,
                "dimensions": dims,
                "kind": kind,
                "is_attribute": kind == "attribute",
                "is_generic": kind == "generic",
                "matched_keywords": [term],
                "sources": sources,
                **({"reason": reason} if reason else {}),
            }
        )

    # 无有效 HS 编码的候选显著降权（避免推荐到 junk 叶子类目）
    for item in raw:
        if not valid_hs(item.get("hs_code")):
            dims = dict(item.get("dimensions") or {})
            dims["catalog_fit"] = round(float(dims.get("catalog_fit", 0.0)) * 0.35, 3)
            dims["label_fit"] = round(float(dims.get("label_fit", 0.0)) * 0.55, 3)
            item["dimensions"] = dims
            item["confidence"] = confidence_from_dimensions(dims)
            item["score"] = round(float(item["score"]) * 0.45, 3)
            item["reason"] = "该候选类目缺少有效 HS 编码，已降权"

    raw.sort(key=lambda x: float(x.get("confidence") or 0), reverse=True)
    by_label: dict[str, dict] = {}
    for item in raw:
        label = str(item["label"])
        if label not in by_label or item["score"] > by_label[label]["score"]:
            by_label[label] = item

    # 主商品词（如袜/长袜）频次高时，人群/动物修饰词候选降权
    dominant = dominant_product_signals(title, hint)
    if dominant:
        top_term, top_cnt = dominant[0]
        for item in by_label.values():
            label = str(item.get("label", ""))
            cn = str(item.get("category_cn_name", ""))
            dec = str(item.get("declare_cn_name", ""))
            cat_blob = f"{cn} {dec}"
            if label in AUDIENCE_DESCRIPTOR_TERMS or label in ANIMAL_DESCRIPTOR_TERMS:
                if top_cnt >= 2 and top_term not in cat_blob and top_term not in label:
                    dims = dict(item.get("dimensions") or {})
                    for key in ("term_match", "term_frequency", "label_fit", "catalog_fit"):
                        dims[key] = round(float(dims.get(key, 0.0)) * 0.22, 3)
                    item["dimensions"] = dims
                    item["confidence"] = confidence_from_dimensions(dims)
                    item["score"] = round(float(item["score"]) * 0.25, 3)
                    item["reason"] = f"标题主商品为「{top_term}」（×{top_cnt}），「{label}」为修饰词已降权"
            elif top_cnt >= 2 and top_term not in cat_blob and top_term not in label:
                if not any(t in cat_blob or t in label for t, _ in dominant[:2]):
                    dims = dict(item.get("dimensions") or {})
                    dims["catalog_fit"] = round(float(dims.get("catalog_fit", 0.0)) * 0.45, 3)
                    item["dimensions"] = dims
                    item["confidence"] = confidence_from_dimensions(dims)
                    item["score"] = round(float(item["score"]) * 0.5, 3)
                    if not item.get("reason"):
                        item["reason"] = f"与标题主商品「{top_term}」不一致，已降权"

    bag_nouns = extract_product_nouns(title)
    dominant_terms = [t for t, _ in dominant_product_signals(title, hint)[:4]]
    anchor_nouns = list(dict.fromkeys([*bag_nouns, *dominant_terms]))
    if anchor_nouns:
        for item in by_label.values():
            label = str(item.get("label", ""))
            cn = str(item.get("category_cn_name", ""))
            dec = str(item.get("declare_cn_name", ""))
            cat_blob = f"{cn} {dec}"
            is_anchor_label = label in anchor_nouns or any(
                n in label or label in n for n in anchor_nouns
            )
            is_anchor_cat = any(
                n in cat_blob or any(alias in cat_blob for alias in _search_terms_for(n))
                for n in anchor_nouns
            )
            if is_anchor_label or is_anchor_cat:
                continue
            dims = dict(item.get("dimensions") or {})
            for key in ("term_match", "label_fit", "catalog_fit", "term_frequency"):
                dims[key] = round(float(dims.get(key, 0.0)) * 0.18, 3)
            item["dimensions"] = dims
            item["confidence"] = confidence_from_dimensions(dims)
            item["score"] = round(float(item["score"]) * 0.18, 3)
            if not item.get("reason"):
                item["reason"] = (
                    f"标题主商品为「{'/'.join(anchor_nouns[:2])}」，「{label}」已降权"
                )

    # 地域/风格词一律大幅降权（非商品品类）
    for item in by_label.values():
        label = str(item.get("label", ""))
        if label not in GEOGRAPHIC_REGION_TERMS:
            continue
        dims = dict(item.get("dimensions") or {})
        for key in ("term_match", "term_frequency", "label_fit", "catalog_fit"):
            dims[key] = round(float(dims.get(key, 0.0)) * 0.15, 3)
        item["dimensions"] = dims
        item["confidence"] = confidence_from_dimensions(dims)
        item["score"] = round(float(item["score"]) * 0.15, 3)
        item["reason"] = f"「{label}」为地域/风格修饰，非商品品类，已降权"

    # 主题/系列氛围词降权（如「海洋系列」不是海洋·教育书）
    for item in by_label.values():
        label = str(item.get("label", ""))
        if label not in THEME_SERIES_TERMS:
            continue
        dims = dict(item.get("dimensions") or {})
        for key in ("term_match", "term_frequency", "label_fit", "catalog_fit", "specificity"):
            dims[key] = round(float(dims.get(key, 0.0)) * 0.12, 3)
        item["dimensions"] = dims
        item["confidence"] = confidence_from_dimensions(dims)
        item["score"] = round(float(item["score"]) * 0.12, 3)
        item["reason"] = f"「{label}」为主题/系列修饰，非商品品类，已降权"

    pool = _primary_pool(list(by_label.values()), title)
    ranked = sorted(pool, key=lambda x: float(x.get("confidence") or 0), reverse=True)[:3]
    for item in ranked:
        penalty = domain_conflict_multiplier(
            title,
            str(item.get("category_cn_name", "")),
            str(item.get("declare_cn_name", "")),
            vision_keywords,
        )
        if penalty < 1.0:
            dims = dict(item.get("dimensions") or {})
            dims["catalog_fit"] = round(float(dims.get("catalog_fit", 0.0)) * penalty, 3)
            item["dimensions"] = dims
            item["confidence"] = confidence_from_dimensions(dims)
            item["score"] = round(float(item["score"]) * penalty, 3)
            item["reason"] = f"类目含领域词但标题未体现，已降权（×{penalty}）"
    ranked.sort(key=lambda x: float(x.get("confidence") or 0), reverse=True)
    ranked = _apply_history_conventions(ranked[:3], title, vision_keywords, catalog)
    return _finalize_semantic_candidate_ranks(ranked[:3])


def _merge_convention_hits(
    history_hits: list[dict],
    pending_hits: list[dict],
) -> list[dict]:
    """合并 Excel 历史惯例与在线共识；同 cid 取更强 strength。"""
    strength_rank = {
        "dominant": 4,
        "pending_promoted": 3,
        "support": 2,
        "pending_soft": 1,
    }
    by_cid: dict[str, dict] = {}
    for hit in [*history_hits, *pending_hits]:
        cid = str(hit.get("category_id") or "")
        if not cid:
            continue
        prev = by_cid.get(cid)
        if not prev:
            by_cid[cid] = dict(hit)
            continue
        if strength_rank.get(str(hit.get("strength")), 0) > strength_rank.get(
            str(prev.get("strength")), 0
        ):
            merged = {**prev, **hit}
            by_cid[cid] = merged
        else:
            # 保留更高票数 / share
            if int(hit.get("count") or 0) > int(prev.get("count") or 0):
                by_cid[cid] = {**prev, **{k: hit[k] for k in ("count", "share", "summary") if k in hit}}
    return sorted(
        by_cid.values(),
        key=lambda x: (
            strength_rank.get(str(x.get("strength")), 0),
            float(x.get("share") or 0) * int(x.get("count") or 0),
        ),
        reverse=True,
    )[:8]


def _apply_history_conventions(
    ranked: list[dict],
    title: str,
    vision_keywords: list[str] | None,
    catalog: dict,
) -> list[dict]:
    """历史订单惯例 + 在线共识 soft-boost：注入常见 cid，并在接近时按惯例决胜。"""
    hits = _merge_convention_hits(
        lookup_history_conventions_for_text(title, vision_keywords),
        lookup_pending_conventions_for_text(title, vision_keywords),
    )
    if not hits:
        return ranked

    by_cid = catalog.get("by_cid") or {}
    pool = {str(c.get("category_id")): dict(c) for c in ranked}

    for hit in hits:
        cid = str(hit.get("category_id") or "")
        if not cid:
            continue
        entry = by_cid.get(cid) or (by_cid.get(int(cid)) if cid.isdigit() else None) or {}
        share = float(hit.get("share") or 0)
        count = int(hit.get("count") or 0)
        strength = str(hit.get("strength") or "")
        # 惯例加分：Excel dominant 最强；在线 promoted 次之；soft 更弱
        boost = 0.18 + share * 0.55
        if strength == "dominant":
            boost += 0.12
        elif strength == "pending_promoted":
            boost += 0.10
        elif strength == "pending_soft":
            boost = 0.10 + share * 0.35
        conf_boost = min(0.92, 0.55 + share * 0.4 + min(count, 40) / 200)
        if strength == "pending_soft":
            conf_boost = min(0.88, conf_boost)
        source_tag = "pending_consensus" if strength.startswith("pending") else "history"

        if cid in pool:
            row = pool[cid]
            row["score"] = round(float(row.get("score") or 0) + boost, 3)
            row["confidence"] = round(
                min(0.96, max(float(row.get("confidence") or 0), conf_boost) + boost * 0.15),
                3,
            )
            row["sources"] = list(dict.fromkeys([*(row.get("sources") or []), source_tag]))
            dims = dict(row.get("dimensions") or {})
            dims["history"] = round(share, 3)
            if strength.startswith("pending"):
                dims["consensus_support"] = count
            row["dimensions"] = dims
            reason = str(hit.get("summary") or "")
            if reason:
                prev = str(row.get("reason") or "")
                row["reason"] = f"{prev}；{reason}" if prev and reason not in prev else (reason or prev)
            row["history_convention"] = hit
            continue

        if not entry and not hit.get("category_cn_name"):
            continue
        cn = str(entry.get("cn_name") or hit.get("category_cn_name") or "")
        dec = str(entry.get("dec_cn_name") or hit.get("declare_cn_name") or "")
        hs = str(entry.get("hs_code") or hit.get("hs_code") or "")
        if not valid_hs(hs) and strength not in ("dominant", "pending_promoted"):
            continue
        pool[cid] = {
            "label": hit.get("term") or cn,
            "category_id": int(cid) if cid.isdigit() else cid,
            "category_cn_name": cn,
            "category_en_name": entry.get("en_name", ""),
            "hs_code": hs,
            "declare_cn_name": dec,
            "declare_en_name": entry.get("dec_en_name", ""),
            "score": round(0.55 + share * 0.5, 3),
            "confidence": round(conf_boost, 3),
            "dimensions": {
                "term_match": 0.8,
                "history": round(share, 3),
                "consensus_support": count if strength.startswith("pending") else 0,
                "label_fit": 0.7,
                "catalog_fit": 0.6 if valid_hs(hs) else 0.2,
                "specificity": 0.7,
                "vision": 0.5 if vision_keywords else 0.0,
            },
            "kind": "specific",
            "is_attribute": False,
            "is_generic": False,
            "matched_keywords": [hit.get("term")] if hit.get("term") else [],
            "sources": [source_tag],
            "reason": str(hit.get("summary") or ("在线共识" if source_tag == "pending_consensus" else "历史订单惯例")),
            "history_convention": hit,
        }

    merged = list(pool.values())
    merged.sort(key=lambda x: float(x.get("confidence") or 0), reverse=True)

    # 接近决胜：top2 分差很小且第二名有更强历史惯例 → 提升惯例侧
    if len(merged) >= 2:
        a, b = merged[0], merged[1]
        gap = float(a.get("confidence") or 0) - float(b.get("confidence") or 0)
        ha = (a.get("history_convention") or {}) if isinstance(a.get("history_convention"), dict) else {}
        hb = (b.get("history_convention") or {}) if isinstance(b.get("history_convention"), dict) else {}
        if gap <= 0.12 and hb.get("strength") == "dominant" and ha.get("strength") != "dominant":
            b["confidence"] = round(float(a.get("confidence") or 0) + 0.02, 3)
            b["reason"] = (
                f"{b.get('reason') or ''}；相近候选按历史惯例优先".strip("；")
            )
            merged.sort(key=lambda x: float(x.get("confidence") or 0), reverse=True)
        elif gap <= 0.08 and float(hb.get("share") or 0) >= float(ha.get("share") or 0) + 0.15:
            if int(hb.get("count") or 0) >= int(ha.get("count") or 0):
                b["confidence"] = round(float(a.get("confidence") or 0) + 0.01, 3)
                b["reason"] = (
                    f"{b.get('reason') or ''}；相近候选按历史频次优先".strip("；")
                )
                merged.sort(key=lambda x: float(x.get("confidence") or 0), reverse=True)

    return merged[:5]


def _finalize_semantic_candidate_ranks(candidates: list[dict]) -> list[dict]:
    """按最终 confidence 重排并写入 rank 1..n（展示与决策一致）。"""
    ordered = sorted(candidates, key=lambda x: float(x.get("confidence") or 0), reverse=True)
    for i, item in enumerate(ordered):
        item["rank"] = i + 1
    return ordered


UNIFORM_DOMAIN_SIGNALS = {
    "制服", "工作服", "演出服", "舞台服装", "职业套装", "校服", "商务服",
    "工装", "工作制服", "海员", "演出", "保安", "护士", "医生", "厨师",
    "警察", "消防员", "空乘",
}


def _primary_pool(cands: list[dict], title: str = "") -> list[dict]:
    """只保留「真正的具体品类」参与竞争：
    - 有具体品类时，剔除属性词与泛父类词；
    - 只有泛父类时，用泛父类；
    - 只有属性词时（商品本身就是该属性品类，如蕾丝面料），才用属性词。

    特殊处理：标题含制服/职业装/演出等强领域信号时，即使存在其它具体品类，
    也保留映射到制服/演出领域的泛词候选（如 工作服 → 工作服/校服/商务服定制），
    避免「西装」等词因出现位置靠前而误胜真正的制服/演出商品。
    """
    non_attr = [c for c in cands if not c.get("is_attribute")]
    if not non_attr:
        return cands
    specifics = [c for c in non_attr if not c.get("is_generic")]
    if not specifics:
        return non_attr

    title_has_uniform_signal = any(s in title for s in UNIFORM_DOMAIN_SIGNALS)
    if title_has_uniform_signal:
        uniform_generics = [
            c for c in non_attr if c.get("is_generic") and _is_uniform_category(c)
        ]
        if uniform_generics:
            # 只取置信度最高的一个制服类泛词，避免泛词过多稀释排序
            best_generic = max(uniform_generics, key=lambda x: float(x.get("confidence") or 0))
            return sorted([*specifics, best_generic], key=lambda x: float(x.get("confidence") or 0), reverse=True)

    return specifics


def _is_uniform_category(cand: dict) -> bool:
    """候选类目是否属于制服/演出/职业装领域。"""
    cn = str(cand.get("category_cn_name", ""))
    dec = str(cand.get("declare_cn_name", ""))
    blob = f"{cn} {dec}"
    uniform_cat_signals = {
        "制服", "工作服", "演出服", "舞台服装", "职业套装", "校服",
        "商务服", "工装", "工作制服", "演出", "舞台装", "cosplay",
    }
    return any(s in blob for s in uniform_cat_signals)


def infer_decision(
    ranked: list[dict],
    vision_keywords: list[str],
    title: str,
) -> str:
    """能决则决：仅在「≥2 个真正的具体品类且分离度低」时才判为多义待选。

    ranked 已经过 _primary_pool 过滤（属性词/泛父类词不参与竞争），
    因此这里的分歧都是「不同的具体品类」之间的真分歧。
    """
    if not ranked:
        return "manual_suggested"
    if len(ranked) == 1:
        return "semantic_agreement"

    top, second = ranked[0], ranked[1]
    ct = float(top.get("confidence") or score_to_confidence(top.get("score", 0)))
    cs = float(second.get("confidence") or score_to_confidence(second.get("score", 0)))
    sep = compute_separation(top, second)

    # 图文一致：视觉关键词落在标题里，或与 top 标签一致 → 直接采用 top
    agreement = [
        kw
        for kw in vision_keywords
        if kw and (kw in title or kw == top.get("label") or kw in str(top.get("category_cn_name", "")))
    ]
    if agreement and ct >= 0.55:
        return "semantic_agreement"

    # 多维领先 → agent 自行决断（不再依赖单一 8% 百分比差）
    if sep["dim_wins"] >= 3:
        return "semantic_agreement"
    if sep["dim_wins"] >= 2 and ct >= 0.68:
        return "semantic_agreement"
    if ct >= 0.78:
        return "semantic_agreement"
    if sep["score_gap"] >= 0.12:
        return "semantic_agreement"
    if sep["conf_gap"] >= 0.09:
        return "semantic_agreement"

    return "ambiguous_semantics"


def agreement_keywords(title: str, vision_keywords: list[str], ranked: list[dict]) -> list[str]:
    if not vision_keywords:
        return []
    out: list[str] = []
    for kw in vision_keywords:
        if kw in title:
            out.append(kw)
    if not out and ranked:
        top_label = ranked[0].get("label", "")
        for kw in vision_keywords:
            if kw in top_label or top_label in kw:
                out.append(kw)
    return list(dict.fromkeys(out))[:6]


def score_catalog_entry(title: str, title_tokens: set[str], hint: str, entry: dict) -> float:
    cn = entry.get("cn_name", "")
    dec = entry.get("dec_cn_name", "")
    blob = " ".join([cn, entry.get("en_name", ""), dec, entry.get("dec_en_name", ""), hint or ""]).lower()
    cat_tokens = tokenize(blob)
    if not title_tokens or not cat_tokens:
        base = 0.0
    else:
        base = len(title_tokens & cat_tokens) / max(len(title_tokens), 1)

    title_l = title.lower()
    hint_l = (hint or "").lower()

    # 平台类目 / 标题关键词加权
    for kw in [hint_l, *re.findall(r"[\u4e00-\u9fff]{2,4}", title_l)]:
        if not kw:
            continue
        if kw in cn.lower() or kw in dec.lower():
            base += 0.25
        if kw in cn or kw in dec:
            base += 0.15

    # 明显冲突降权
    if "耳机" in title_l and "音箱" in cn and "耳机" not in cn:
        base *= 0.2
    if "腰带" in title_l and "女装" in cn and "腰带" not in cn:
        base *= 0.3

    base *= domain_conflict_multiplier(title, cn, dec, None)

    return base


def is_price_only_title(title: str) -> bool:
    t = (title or "").strip()
    if not t:
        return True
    stripped = re.sub(
        r"(价格范围|价格|CNY|RMB|¥|元|包邮|现货|厂家|直销|批发|定制|新款|热卖)",
        "",
        t,
        flags=re.I,
    )
    stripped = re.sub(r"[\d\.\-\s~～]+", "", stripped)
    return len(stripped) < 2


def category_name_matches_hint(cat_name: str, hint: str) -> bool:
    if not hint or not cat_name:
        return False
    h = hint.strip()
    c = cat_name.strip()
    return h in c or c in h or h.lower() in c.lower()


def lookup_best_for_hint(hint: str, catalog: dict) -> dict | None:
    """将平台类目名解析为 catalog 条目（仅作参照/兜底，不参与 Agent 打分）。"""
    h = (hint or "").strip()
    if not h:
        return None
    best: dict | None = None
    best_score = 0.0
    for entry in catalog["list"]:
        s = score_hint_match(h, entry)
        if s > best_score:
            best_score = s
            best = entry
    if best and best_score >= 0.5:
        return enrich_category_fields(best, catalog)
    for entry in catalog["list"]:
        cn = str(entry.get("cn_name") or "")
        if cn == h or (len(h) >= 2 and h in cn):
            return enrich_category_fields(entry, catalog)
    return None


def detect_platform_reference_issue(
    *,
    title: str,
    platform_hint: str,
    vision_keywords: list[str],
    ranked: list[dict],
    picked_cid: int,
) -> tuple[str, str, bool]:
    """返回 (decision, detail, is_platform_fallback)。"""
    if not platform_hint:
        return "", "", False

    picked = lookup_cid(picked_cid) or {}
    picked_cn = str(picked.get("cn_name") or "")
    hint_aligned = category_name_matches_hint(picked_cn, platform_hint) or score_hint_match(
        platform_hint, picked
    ) >= 0.5

    weak_title = is_price_only_title(title)
    has_vision = bool(vision_keywords)
    title_supports_hint = platform_hint in title or any(
        platform_hint in kw or kw in platform_hint for kw in vision_keywords
    )

    if hint_aligned and not title_supports_hint:
        if weak_title and not has_vision:
            return (
                "platform_fallback",
                f"标题/图片无法解析品类，暂沿用平台当前类目「{platform_hint}」",
                True,
            )
        if has_vision and not title_supports_hint:
            vision_blob = " ".join(vision_keywords)
            return (
                "ambiguous_semantics",
                f"识图「{vision_blob[:24]}」与平台类目「{platform_hint}」不一致，需人工纠错",
                False,
            )
        if not weak_title and not has_vision:
            return (
                "platform_fallback",
                f"标题/识图未佐证平台类目「{platform_hint}」，建议人工确认",
                True,
            )

    if not hint_aligned:
        top = ranked[0] if ranked else None
        top_label = str((top or {}).get("label") or picked_cn)
        display_cn = str((top or {}).get("category_cn_name") or picked_cn)
        return (
            "ambiguous_semantics",
            f"Agent 首选「{top_label}→{display_cn}」，与平台类目「{platform_hint}」不一致，需人工纠错",
            False,
        )

    return "", "", False


def suggest(
    title: str,
    hint: str = "",
    goods_id: str = "",
    image_url: str = "",
    vision_keywords: list[str] | None = None,
    *,
    skip_history: bool = False,
    hint_as_reference: bool = False,
    platform_hint: str = "",
) -> dict:
    if not DATA.exists():
        return {
            "success": False,
            "error": "品类数据未构建，请先运行: python3 scripts/build-category-data.py",
        }

    catalog, history, goods_id_index, token_index = load_data()
    title = (title or "").strip()
    if not title:
        return {"success": False, "error": "缺少商品标题 title"}

    vk = vision_keywords or []
    ref_hint = (platform_hint or hint or "").strip()
    scoring_hint = "" if hint_as_reference else (hint or "").strip()

    # 1) 历史 goods_id 精确命中 — 二元门闩，100% 通过（重新映射可跳过）
    gid = re.sub(r"\D", "", goods_id or "")
    if not skip_history and gid and gid in goods_id_index:
        cid = goods_id_index[gid]
        return pack_result(
            cid,
            1.0,
            "history_goods_id",
            f"历史 goods_id={gid} 已映射，直接采用",
            title=title,
            hint=hint,
            decision="history_hit",
            history_hit=True,
            skip_fusion=True,
            vision_keywords=vk,
        )

    # 1b) 在线 soft goods_id（单票不成门闩；support≥3 才强参考）
    soft_gid = lookup_goods_id_soft(gid) if (not skip_history and gid) else None
    soft_gid_boost_cid: int | None = None
    if soft_gid and int(soft_gid.get("support") or 0) >= 3:
        try:
            soft_gid_boost_cid = int(soft_gid.get("category_id"))
        except (TypeError, ValueError):
            soft_gid_boost_cid = None

    title_tokens = tokenize(title + " " + (scoring_hint or ""))
    ranked = rank_semantic_candidates(title, scoring_hint, vk, catalog)
    decision = infer_decision(ranked, vk, title)
    agree_kws = agreement_keywords(title, vk, ranked)

    if ranked:
        top = ranked[0]
        cid = int(top["category_id"])
        hint_s = score_hint_match(scoring_hint, lookup_cid(cid) or {}) if scoring_hint else 0.0
        title_s = min(1.0, float(top["score"]))
        image_s = 0.82 if vk else 0.0
        if agree_kws:
            image_s = max(image_s, 0.88)
            title_s = min(1.0, title_s + 0.12)
        hist_conv = top.get("history_convention") if isinstance(top.get("history_convention"), dict) else {}
        history_s = float(hist_conv.get("share") or 0.0)
        if hist_conv.get("strength") == "dominant":
            history_s = max(history_s, 0.75)
        elif hist_conv.get("strength") == "pending_promoted":
            history_s = max(history_s, 0.65)
        elif hist_conv.get("strength") == "pending_soft":
            history_s = max(history_s, 0.45)

        base_conf = float(top.get("confidence") or score_to_confidence(top.get("score", 0)))
        if soft_gid_boost_cid is not None and int(top.get("category_id") or 0) == soft_gid_boost_cid:
            base_conf = min(0.94, base_conf + 0.08)
            if "pending_consensus" not in (top.get("sources") or []):
                top["sources"] = list(dict.fromkeys([*(top.get("sources") or []), "pending_goods_id"]))

        override_decision, override_detail, is_fallback = detect_platform_reference_issue(
            title=title,
            platform_hint=ref_hint,
            vision_keywords=vk,
            ranked=ranked,
            picked_cid=cid,
        )
        if override_decision:
            decision = override_decision
        if is_fallback:
            base_conf = min(base_conf, 0.45)
        elif override_decision in ("ambiguous_semantics", "platform_fallback"):
            base_conf = min(base_conf, 0.72)

        if decision == "semantic_agreement" and not is_fallback and not override_decision:
            conf = clamp_conf(base_conf + (0.12 if agree_kws else 0.05))
            if agree_kws:
                # 图文一致，给足自动通过的底气
                conf = max(conf, 0.85)
            if history_s >= 0.5:
                conf = max(conf, min(0.9, conf + 0.03))
            detail = override_detail or f"标题语义词「{top['label']}」与类目「{top['category_cn_name']}」一致"
            if hist_conv.get("summary"):
                detail = f"{detail}；{hist_conv['summary']}"
            return pack_result(
                cid,
                conf,
                "keyword_catalog_strong" if conf >= 0.75 else "keyword_catalog",
                detail,
                candidates=ranked,
                title=title,
                hint=scoring_hint or ref_hint,
                signal_scores=fuse_signals(title_s, hint_s, history_s, image_s),
                matched_keywords=list(dict.fromkeys([*agree_kws, top["label"], *vk]))[:10],
                decision=decision,
                semantic_candidates=ranked,
                title_image_agreement_keywords=agree_kws,
                vision_keywords=vk,
            )

        detail = override_detail or (
            f"标题含多个具体品类，请在置信度选项中确认（推荐：{top['label']}）"
            if decision == "ambiguous_semantics"
            else f"标题语义词「{top['label']}」与类目「{top['category_cn_name']}」一致"
        )
        if hist_conv.get("summary"):
            detail = f"{detail}；{hist_conv['summary']}"
        method = "platform_reference" if is_fallback else "keyword_ambiguous"
        return pack_result(
            cid,
            base_conf,
            method,
            detail,
            candidates=ranked,
            title=title,
            hint=scoring_hint or ref_hint,
            signal_scores=fuse_signals(title_s, hint_s, history_s, image_s),
            matched_keywords=list(dict.fromkeys([top["label"], *vk, *extract_title_semantics(title)]))[:10],
            decision=decision,
            semantic_candidates=ranked,
            title_image_agreement_keywords=agree_kws,
            vision_keywords=vk,
            separation=compute_separation(ranked[0], ranked[1]) if len(ranked) >= 2 else None,
        )

    # 2) 历史标题相似（不作为历史门闩，仅作弱参考）
    candidate_idxs: set[int] = set()
    for tok in title_tokens:
        for idx in token_index.get(tok, []):
            candidate_idxs.add(idx)

    best_hist = None
    best_ratio = 0.0
    for idx in candidate_idxs:
        rec = history[idx]
        ratio = SequenceMatcher(None, title, rec["goods_name"]).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_hist = rec

    if best_hist and best_ratio >= 0.72:
        cat = lookup_cid(best_hist["category_id"]) or {}
        title_s = score_title_match(title, cat)
        hint_s = score_hint_match(hint, cat)
        signals = fuse_signals(title_s, hint_s, 0.0, 0.82 if vk else 0.0)
        return pack_result(
            best_hist["category_id"],
            signals["fused"],
            "history_similar",
            f"历史相似商品参考（相似度 {best_ratio:.2f}），建议人工确认",
            candidates=_top_history_candidates(history, candidate_idxs, title, 3),
            title=title,
            hint=hint,
            signal_scores=signals,
            decision="manual_suggested",
            vision_keywords=vk,
        )

    boosts = load_feedback_boost()
    scored: list[tuple[float, dict]] = []
    for entry in catalog["list"]:
        s = score_catalog_entry(title, title_tokens, hint, entry)
        cid_s = str(entry["cid"])
        for kw in title_tokens:
            s += boosts.get(f"{kw}:{cid_s}", 0)
        if hint:
            s += boosts.get(f"{hint}:{cid_s}", 0)
        for kw in vk:
            if kw in entry.get("cn_name", "") or kw in entry.get("dec_cn_name", ""):
                s += 0.3
        if s > 0:
            scored.append((s, entry))
    scored.sort(key=lambda x: x[0], reverse=True)

    if scored and scored[0][0] >= 0.15:
        top = scored[:8]
        picked = pick_scored_with_hs(top)
        if not picked:
            picked = top[0]
        best_score, best = picked
        best = enrich_category_fields(best, catalog)
        title_s = min(1.0, best_score)
        hint_s = score_hint_match(hint, best)
        image_s = 0.82 if vk else 0.0
        signals = fuse_signals(title_s, hint_s, 0.0, image_s)
        sem = rank_semantic_candidates(title, hint, vk, catalog)
        return pack_result(
            best["cid"],
            signals["fused"],
            "keyword_catalog",
            f"关键词匹配类目「{best['cn_name']}」",
            candidates=[
                {
                    "category_id": e["cid"],
                    "category_cn_name": e["cn_name"],
                    "hs_code": e["hs_code"],
                    "score": round(s, 3),
                }
                for s, e in top
            ],
            title=title,
            hint=hint,
            signal_scores=signals,
            decision=infer_decision(sem, vk, title),
            semantic_candidates=sem,
            title_image_agreement_keywords=agree_kws,
            vision_keywords=vk,
        )

    if scored:
        picked = pick_scored_with_hs(scored[:8])
        if not picked:
            picked = scored[0]
        best_score, best = picked
        best = enrich_category_fields(best, catalog)
        signals = fuse_signals(min(1.0, best_score), score_hint_match(hint, best), 0.0, 0.82 if vk else 0.0)
        return pack_result(
            best["cid"],
            max(0.35, signals["fused"]),
            "keyword_weak",
            "弱匹配，建议人工搜索 HS 类目",
            candidates=[
                {
                    "category_id": e["cid"],
                    "category_cn_name": e["cn_name"],
                    "hs_code": e["hs_code"],
                    "score": round(s, 3),
                }
                for s, e in scored[:3]
            ],
            title=title,
            hint=hint,
            signal_scores=signals,
            decision="manual_suggested",
            vision_keywords=vk,
        )

    if hint_as_reference and ref_hint:
        plat = lookup_best_for_hint(ref_hint, catalog)
        if plat:
            picked_hs = pick_scored_with_hs([(1.0, plat)])
            if picked_hs:
                _, plat_entry = picked_hs
            else:
                plat_entry = plat
            detail = f"标题/图片无法解析品类，暂沿用平台当前类目「{ref_hint}」"
            return pack_result(
                plat_entry["cid"],
                0.38,
                "platform_reference",
                detail,
                title=title,
                hint=ref_hint,
                signal_scores={
                    "title": 0.0,
                    "platform_category": 0.0,
                    "history": 0.0,
                    "image": 0.0,
                    "fused": 0.38,
                },
                decision="platform_fallback",
                vision_keywords=vk,
            )

    return {
        "success": False,
        "error": "未能匹配到合适品类",
        "confidence": 0,
        "decision": "manual_suggested",
    }


def _top_history_candidates(history, idxs: set[int], title: str, n: int) -> list[dict]:
    ranked = []
    for idx in idxs:
        rec = history[idx]
        ratio = SequenceMatcher(None, title, rec["goods_name"]).ratio()
        ranked.append((ratio, rec))
    ranked.sort(key=lambda x: x[0], reverse=True)
    out = []
    for ratio, rec in ranked[:n]:
        cat = lookup_cid(rec["category_id"])
        out.append(
            {
                "goods_name": rec["goods_name"][:60],
                "category_id": rec["category_id"],
                "category_cn_name": cat["cn_name"] if cat else "",
                "hs_code": cat["hs_code"] if cat else "",
                "similarity": round(ratio, 3),
            }
        )
    return out


def search_catalog(query: str, limit: int = 12) -> dict:
    if not DATA.exists():
        return {"success": False, "error": "数据未构建"}
    catalog, _, _, _ = load_data()
    q = (query or "").strip()
    if not q:
        return {"success": True, "results": []}
    tokens = tokenize(q)
    scored: list[tuple[float, dict]] = []
    for entry in catalog["list"]:
        s = score_catalog_entry(q, tokens, q, entry)
        blob = " ".join(
            [
                entry.get("cn_name", ""),
                entry.get("en_name", ""),
                entry.get("dec_cn_name", ""),
                entry.get("hs_code", ""),
                str(entry.get("cid", "")),
            ]
        )
        if q in blob or q.lower() in blob.lower():
            s += 0.5
        if s > 0:
            scored.append((s, enrich_category_fields(entry, catalog)))
    scored.sort(key=lambda x: x[0], reverse=True)
    results = []
    for s, e in scored[:limit]:
        results.append(
            {
                "score": round(s, 3),
                "category_id": e["cid"],
                "category_cn_name": e["cn_name"],
                "category_en_name": e["en_name"],
                "hs_code": e["hs_code"],
                "declare_cn_name": e["dec_cn_name"],
                "declare_en_name": e["dec_en_name"],
            }
        )
    return {"success": True, "results": results}


def main() -> int:
    parser = argparse.ArgumentParser(description="品类映射 Agent CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_suggest = sub.add_parser("suggest", help="根据标题/类目/历史建议 HS 映射")
    p_suggest.add_argument("--title", required=True)
    p_suggest.add_argument("--hint", default="")
    p_suggest.add_argument("--platform-hint", default="")
    p_suggest.add_argument("--goods-id", default="")
    p_suggest.add_argument("--image-url", default="")
    p_suggest.add_argument("--vision-keywords", default="")
    p_suggest.add_argument("--skip-history", action="store_true")
    p_suggest.add_argument("--hint-as-reference", action="store_true")

    p_lookup = sub.add_parser("lookup", help="按分类编号查询")
    p_lookup.add_argument("--cid", required=True)

    p_search = sub.add_parser("search", help="联想搜索 HS 类目表")
    p_search.add_argument("--query", required=True)
    p_search.add_argument("--limit", type=int, default=12)

    args = parser.parse_args()

    if args.command == "suggest":
        vk: list[str] = []
        if args.vision_keywords:
            try:
                parsed = json.loads(args.vision_keywords)
                if isinstance(parsed, list):
                    vk = [str(x) for x in parsed if x]
            except json.JSONDecodeError:
                vk = [x.strip() for x in args.vision_keywords.split(",") if x.strip()]
        result = suggest(
            args.title,
            args.hint,
            args.goods_id,
            args.image_url,
            vk,
            skip_history=bool(args.skip_history),
            hint_as_reference=bool(args.hint_as_reference),
            platform_hint=args.platform_hint or args.hint,
        )
    elif args.command == "search":
        result = search_catalog(args.query, args.limit)
    else:
        cat = lookup_cid(args.cid)
        result = {"success": bool(cat), "data": cat} if cat else {"success": False, "error": "not found"}

    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())

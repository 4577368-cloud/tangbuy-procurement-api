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
    {
        "domain": "玩具",
        "markers": ["玩具", "益智", "积木", "玩偶", "娃娃", "模型玩具"],
        "human_context": [
            "包", "胸包", "斜挎", "斜跨", "单肩", "双肩", "腰包", "背包", "挎包", "手包", "拎包",
            "卫衣", "毛衣", "针织", "裤", "裙", "袜", "鞋", "帽", "衣", "服",
        ],
    },
    {
        "domain": "箱包",
        "markers": [
            "胸包", "斜挎包", "斜跨包", "单肩包", "双肩包", "腰包", "背包", "挎包",
            "手包", "拎包", "手提包", "箱包", "休闲包",
        ],
        "human_context": ["玩具", "益智", "卫衣", "毛衣", "针织"],
    },
    {
        "domain": "石器",
        "markers": [
            "石槽", "石磨", "石器", "青石板", "老石", "石雕", "石盆", "石刻",
            "石制品", "石条", "石灯笼", "石臼", "石桌", "石凳", "石缸", "石钵",
        ],
        "human_context": [
            "卫衣", "毛衣", "裤", "裙", "袜", "鞋", "帽", "包", "背心", "内衣",
            "路由器", "网卡", "交换机", "网关", "耳机", "音箱",
        ],
    },
    {
        "domain": "电子网络",
        "markers": [
            "路由器", "网卡", "交换机", "网关", "光纤", "集线器", "中继器",
        ],
        "human_context": [
            "石槽", "石磨", "石器", "青石板", "老石", "石雕", "石盆", "石刻",
            "卫衣", "毛衣", "裤", "裙", "袜", "鞋", "帽",
        ],
    },
]

# 锚点词过泛时不参与「必须有标题证据」
ANCHOR_STOPWORDS = {
    "用品", "配件", "其他", "通用", "系列", "款式", "精品", "定制", "专用", "套装",
    "男女", "成人", "儿童", "时尚", "经典", "新款",
}

# 申报名为书/刊物类时，标题/识图须有书类证据，否则主题词（海洋/艺术…）不可蹭到教育书
BOOK_DECLARE_NAMES = frozenset(
    {"教育书", "教育书籍", "书籍", "图书", "杂志", "教材", "读物", "教辅", "绘本"}
)
BOOK_EVIDENCE_MARKERS = (
    "书", "书籍", "教材", "杂志", "读物", "图书", "教辅", "绘本", "课本", "手册",
    "小说", "习题", "试卷", "辞典", "字典", "百科",
)


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

    dec = (dec_cn_name or "").strip()
    if dec in BOOK_DECLARE_NAMES or any(d in dec for d in ("教育书", "书籍", "图书")):
        if not any(m in blob for m in BOOK_EVIDENCE_MARKERS):
            # 主题词恰好等于 cn_name（如 海洋）不算书类证据
            mult = min(mult, 0.08)

    anchors = cn_specialty_tokens(cn_name, dec_cn_name)
    missing = [a for a in anchors if a not in blob]

    # 标题/识图已直接命中申报名或类目名核心词 → 不应对替代锚点缺失过度惩罚
    title_hits_core = bool(
        (dec_cn_name and len(dec_cn_name) >= 2 and dec_cn_name in blob)
        or (cn_name and len(cn_name) >= 2 and cn_name in blob)
    )
    # 书类申报：仅命中主题 cn 不算 core
    if dec in BOOK_DECLARE_NAMES and not any(m in blob for m in BOOK_EVIDENCE_MARKERS):
        title_hits_core = bool(dec and dec in blob)

    if anchors and len(missing) == len(anchors):
        # 全缺失：若命中核心则放宽到 0.55，否则严厉 0.18
        mult = min(mult, 0.55 if title_hits_core else 0.18)
    elif missing and len(missing) >= 2:
        mult = min(mult, 0.75 if title_hits_core else 0.35)

    learned = learned_anchors or {}
    for anchor in anchors:
        if anchor in learned and anchor not in blob:
            mult = min(mult, learned[anchor])

    return mult


# 语义词作类目名前缀时，后缀若构成另一商品（盒/架/袋等）且标题未体现 → 蹭词
CATALOG_EXTENSION_SUFFIXES = (
    "盒",
    "架",
    "袋",
    "箱",
    "柜",
    "桶",
    "瓶",
    "盖",
    "套",
    "垫",
    "收纳",
    "展示",
    "保养",
    "鉴定",
    "包装",
)


def term_catalog_extension_mismatch(term: str, cn: str, dec: str, title: str = "") -> bool:
    """语义词仅为类目名前缀、后缀构成另一商品且标题未体现时，视为蹭词（首饰≠首饰盒）。"""
    if not term:
        return False
    if cn == term or dec == term:
        return False
    title = title or ""
    for host in (cn, dec):
        if not host or host == term or not host.startswith(term):
            continue
        suffix = host[len(term) :]
        if not suffix:
            continue
        if host in title:
            return False
        if any(s in title for s in CATALOG_EXTENSION_SUFFIXES if s in suffix):
            return False
        if any(suffix.startswith(s) or s in suffix for s in CATALOG_EXTENSION_SUFFIXES):
            return True
    return False


def term_catalog_extension_penalty(term: str, cn: str, dec: str, title: str = "") -> float:
    return 0.55 if term_catalog_extension_mismatch(term, cn, dec, title) else 0.0


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


# ── 历史订单惯例（从历史设置类目沉淀：term → 常用 cid）────────────────

CONVENTIONS_FILE = DATA / "history-conventions.json"

_HISTORY_TERM_STOP = ANCHOR_STOPWORDS | {
    "厂家", "直销", "热卖", "爆款", "跨境", "亚马逊", "工厂", "批发", "包邮",
    "新款", "春季", "夏季", "秋季", "冬季", "四季", "通用", "男女", "女士", "男士",
    "小型", "大型", "小号", "大号", "可拆", "可调", "调节", "泰迪", "比熊",
}

# 商品实体后缀：滑动窗口抽词用（窝/垫/床…）
_HISTORY_PRODUCT_SUFFIX = frozenset(
    "窝垫床包袋盒杯壶碗锅鞋靴袜裤裙帽巾被毯枕罩架柜桶瓶刀剪"
)


def _history_title_terms(goods_name: str, catalog_by_cid: dict | None = None) -> list[str]:
    """从历史标题抽商品实体词，用于惯例对照。

    只保留：
    - 2 字且以商品后缀结尾（猫窝/狗窝/垫子）
    - 3 字且前缀为「宠物/猫砂」等（宠物窝/宠物床）
    避免滑窗抽到「熊宠物垫」「狗床猫窝」等噪声。
    """
    del catalog_by_cid
    text = str(goods_name or "").strip()
    if not text:
        return []
    han = re.sub(r"[^\u4e00-\u9fff]", "", text)
    if not han:
        return []
    found: list[str] = []
    seen: set[str] = set()
    allowed_3_prefix = ("宠物", "猫砂", "狗狗", "猫猫")

    def _add(t: str) -> None:
        if t in seen or t in _HISTORY_TERM_STOP:
            return
        if any(t != longer and t in longer for longer in seen):
            return
        seen.add(t)
        found.append(t)

    for i in range(0, len(han) - 1):
        t = han[i : i + 2]
        if t[-1] in _HISTORY_PRODUCT_SUFFIX:
            _add(t)
    for i in range(0, len(han) - 2):
        t = han[i : i + 3]
        if t[-1] not in _HISTORY_PRODUCT_SUFFIX:
            continue
        if not t.startswith(allowed_3_prefix):
            continue
        _add(t)
        # 3 字「宠物窝」优先时，去掉被覆盖的 2 字「物窝」不会产生；但可去掉更短且被包含的
    # 去掉被更长词包含的短词
    filtered = []
    for t in sorted(found, key=len, reverse=True):
        if any(t != longer and t in longer for longer in filtered):
            continue
        filtered.append(t)
    return filtered[:10]


def build_history_conventions(
    history_records: list[dict],
    catalog_by_cid: dict | None = None,
    *,
    min_count: int = 3,
    max_categories_per_term: int = 5,
) -> dict:
    """从历史订单归纳「标题词 → 实际选用类目」惯例。"""
    by_cid = catalog_by_cid or {}
    term_cid_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    term_samples: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))

    for rec in history_records:
        name = str(rec.get("goods_name") or "").strip()
        try:
            cid = str(int(rec.get("category_id")))
        except (TypeError, ValueError):
            continue
        if not name or not cid:
            continue
        for term in _history_title_terms(name):
            term_cid_counts[term][cid] += 1
            samples = term_samples[term][cid]
            if len(samples) < 3 and name not in samples:
                samples.append(name[:80])

    term_to_categories: dict[str, list[dict]] = {}
    dominant: dict[str, dict] = {}

    for term, cid_counts in term_cid_counts.items():
        total = sum(cid_counts.values())
        if total < min_count:
            continue
        ranked = sorted(cid_counts.items(), key=lambda x: (-x[1], x[0]))
        rows: list[dict] = []
        for cid, count in ranked[:max_categories_per_term]:
            if count < min_count and not rows:
                continue
            if count < max(2, min_count // 2) and rows:
                break
            entry = by_cid.get(cid) or {}
            if not entry and cid.isdigit():
                entry = by_cid.get(int(cid)) or {}
            share = round(count / total, 3)
            row = {
                "category_id": int(cid) if cid.isdigit() else cid,
                "category_cn_name": str((entry or {}).get("cn_name") or ""),
                "declare_cn_name": str((entry or {}).get("dec_cn_name") or ""),
                "hs_code": str((entry or {}).get("hs_code") or ""),
                "count": count,
                "share": share,
                "sample_titles": list(term_samples[term].get(cid) or [])[:2],
            }
            rows.append(row)
        if not rows:
            continue
        term_to_categories[term] = rows
        top = rows[0]
        if top["share"] >= 0.35 and top["count"] >= min_count:
            dominant[term] = {
                "category_id": top["category_id"],
                "category_cn_name": top["category_cn_name"],
                "declare_cn_name": top["declare_cn_name"],
                "hs_code": top["hs_code"],
                "count": top["count"],
                "share": top["share"],
                "support": total,
                "summary": (
                    f"含「{term}」的历史商品 {total} 条中，"
                    f"{top['count']} 条（{int(top['share'] * 100)}%）用「"
                    f"{top['category_cn_name'] or top['category_id']}」"
                ),
            }

    return {
        "version": 1,
        "history_count": len(history_records),
        "term_count": len(term_to_categories),
        "dominant_count": len(dominant),
        "term_to_categories": term_to_categories,
        "dominant": dominant,
    }


@lru_cache(maxsize=1)
def load_history_conventions() -> dict:
    if not CONVENTIONS_FILE.exists():
        return {"term_to_categories": {}, "dominant": {}, "version": 0}
    try:
        data = json.loads(CONVENTIONS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"term_to_categories": {}, "dominant": {}}
    except (OSError, json.JSONDecodeError):
        return {"term_to_categories": {}, "dominant": {}}


def clear_history_conventions_cache() -> None:
    load_history_conventions.cache_clear()


def lookup_history_conventions_for_text(
    title: str,
    vision_keywords: list[str] | None = None,
) -> list[dict]:
    """返回当前标题/识图可命中的历史惯例（按 share*count 排序）。"""
    conv = load_history_conventions()
    dominant = conv.get("dominant") or {}
    term_map = conv.get("term_to_categories") or {}
    blob = _title_blob(title, vision_keywords)
    hits: list[dict] = []
    seen_cid: set[str] = set()

    # 先看 dominant（强惯例）
    for term, dom in dominant.items():
        if term not in blob:
            continue
        cid = str(dom.get("category_id"))
        if cid in seen_cid:
            continue
        seen_cid.add(cid)
        hits.append({**dom, "term": term, "strength": "dominant"})

    # 再补 term_to_categories 前列
    for term, rows in term_map.items():
        if term not in blob:
            continue
        for row in (rows or [])[:2]:
            cid = str(row.get("category_id"))
            if cid in seen_cid:
                continue
            if int(row.get("count") or 0) < 3:
                continue
            seen_cid.add(cid)
            hits.append(
                {
                    **row,
                    "term": term,
                    "strength": "support",
                    "summary": (
                        f"含「{term}」时常见「{row.get('category_cn_name') or cid}」"
                        f"（{row.get('count')} 次，{int(float(row.get('share') or 0) * 100)}%）"
                    ),
                }
            )

    hits.sort(
        key=lambda x: (
            1 if x.get("strength") == "dominant" else 0,
            float(x.get("share") or 0) * int(x.get("count") or 0),
        ),
        reverse=True,
    )
    return hits[:6]

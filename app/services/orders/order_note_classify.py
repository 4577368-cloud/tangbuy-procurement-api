"""用户备注分级：无价值（可忽略）vs 有价值（拦截自动下单 / 进指挥中心）。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from functools import lru_cache
from typing import Any, Literal, Optional

from app.core.config import get_settings
from app.core.paths import data_dir

NoteTier = Literal["none", "low", "high"]
NoteTopic = Literal[
    "spec_change",
    "color_change",
    "size_change",
    "quantity_change",
    "custom_request",
    "price_change",
    "other",
]

_CACHE_PATH = data_dir() / "orders" / "note-classify-cache.json"

# —— 无价值：催发货、客套、好评 ——
_LOW_VALUE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(p, re.I)
    for p in [
        r"请?尽快(发|发货|寄|出|配送)",
        r"加急(发|发货)?",
        r"急用",
        r"早点发",
        r"速发",
        r"快(点)?发",
        r"尽快",
        r"asap",
        r"ship\s*(it\s*)?(as\s*soon\s*as\s*possible|quickly|fast|soon)",
        r"fast\s*shipping",
        r"hurry",
        r"quick\s*delivery",
        r"expédier\s*(vite|rapidement)",
        r"livraison\s*rapide",
        r"envío\s*rápido",
        r"lo\s*antes\s*posible",
        r"thank\s*you",
        r"thanks",
        r"merci",
        r"gracias",
        r"谢谢",
        r"感谢",
        r"辛苦了",
        r"好评",
        r"五星",
        r"麻烦",
        r"please\s*ship",
    ]
]

# —— 有价值：规格/颜色/尺码/数量/定制/改价 ——
_HIGH_VALUE_PATTERNS: list[tuple[NoteTopic, re.Pattern[str]]] = [
    (
        "size_change",
        re.compile(
            r"(换|改|更换|改成|换成|change|replace|switch|instead\s+of|changer|"
            r"cambiar|remplacer).{0,12}(码|尺码|size|taille|talla|tamaño)",
            re.I,
        ),
    ),
    (
        "size_change",
        re.compile(
            r"\b(X{0,3}[SML]|XXL|XXXL|XS|\d{2})\b.{0,8}(换|改|→|->|to|换成|instead)",
            re.I,
        ),
    ),
    (
        "color_change",
        re.compile(
            r"(换|改|更换|改成|换成|change|replace|changer|cambiar).{0,12}"
            r"(颜色|色|color|couleur|color)",
            re.I,
        ),
    ),
    (
        "spec_change",
        re.compile(
            r"(规格|型号|款式|版本|variant|model|modèle|modelo|sku|attribute)",
            re.I,
        ),
    ),
    (
        "quantity_change",
        re.compile(
            r"(数量|件数|qty|quantity|cantidad|quantité).{0,8}(改|换|change|→|->)",
            re.I,
        ),
    ),
    (
        "custom_request",
        re.compile(
            r"(定制|定做|oem|贴牌|logo|包装|印花|刺绣|刻字|custom|personaliz)",
            re.I,
        ),
    ),
    (
        "price_change",
        re.compile(
            r"(改价|降价|补款|差价|价格|price|discount|remise|precio)",
            re.I,
        ),
    ),
]


@dataclass
class NoteClassification:
    tier: NoteTier
    topics: list[str]
    block_procurement: bool
    signal_type: Optional[str]
    reason: str
    confidence: float
    source: Literal["rules", "llm", "empty"]

    def to_public(self) -> dict[str, Any]:
        return asdict(self)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _load_cache() -> dict[str, dict[str, Any]]:
    if not _CACHE_PATH.exists():
        return {}
    try:
        raw = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(cache: dict[str, dict[str, Any]]) -> None:
    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    # 控制体积
    items = list(cache.items())[-500:]
    _CACHE_PATH.write_text(
        json.dumps(dict(items), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _cache_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def _classify_with_rules(text: str) -> Optional[NoteClassification]:
    normalized = _normalize(text)
    if not normalized:
        return NoteClassification(
            tier="none",
            topics=[],
            block_procurement=False,
            signal_type=None,
            reason="无备注",
            confidence=1.0,
            source="rules",
        )

    topics: list[str] = []
    for topic, pattern in _HIGH_VALUE_PATTERNS:
        if pattern.search(normalized):
            topics.append(topic)
    topics = list(dict.fromkeys(topics))

    low_hit = any(p.search(normalized) for p in _LOW_VALUE_PATTERNS)
    high_hit = len(topics) > 0

    if high_hit:
        primary = topics[0]
        signal_type = "SKU_MISMATCH" if primary in ("size_change", "color_change", "spec_change") else "NOTE_REVIEW"
        return NoteClassification(
            tier="high",
            topics=topics,
            block_procurement=True,
            signal_type=signal_type,
            reason=f"备注涉及{'/'.join(topics)}，需人工核对",
            confidence=0.88 if signal_type == "SKU_MISMATCH" else 0.84,
            source="rules",
        )

    if low_hit:
        return NoteClassification(
            tier="low",
            topics=[],
            block_procurement=False,
            signal_type=None,
            reason="催发/客套类备注，可忽略",
            confidence=0.9,
            source="rules",
        )

    return None


def _classify_with_llm(text: str) -> Optional[NoteClassification]:
    settings = get_settings()
    if not settings.llm_configured:
        return None

    key = _cache_key(text)
    cache = _load_cache()
    if key in cache:
        return NoteClassification(**cache[key])

    from app.services.agent.llm import chat_completion

    try:
        resp = chat_completion(
            [
                {
                    "role": "system",
                    "content": (
                        "你是跨境采购订单备注分类器。判断用户备注是否会影响采购规格/颜色/尺码/数量。"
                        "无价值示例：请尽快发货、thank you、ship fast。"
                        "有价值示例：M码换XL、change color to red、remplacer la taille、cambiar talla。"
                        "只输出 JSON："
                        '{"tier":"low|high","topics":["size_change"|"color_change"|"spec_change"|'
                        '"quantity_change"|"custom_request"|"price_change"|"other"],"reason":"简短中文","confidence":0.0-1.0}'
                    ),
                },
                {"role": "user", "content": text},
            ]
        )
        raw = (resp.content or "").strip()
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            return None
        data = json.loads(m.group(0))
        tier = data.get("tier")
        if tier not in ("low", "high"):
            return None
        topics = [t for t in (data.get("topics") or []) if isinstance(t, str)]
        confidence = float(data.get("confidence") or 0.75)
        reason = str(data.get("reason") or "LLM 分类")
        signal_type = None
        block = False
        if tier == "high":
            block = True
            if any(t in topics for t in ("size_change", "color_change", "spec_change")):
                signal_type = "SKU_MISMATCH"
            else:
                signal_type = "NOTE_REVIEW"
        result = NoteClassification(
            tier=tier,
            topics=topics,
            block_procurement=block,
            signal_type=signal_type,
            reason=reason,
            confidence=min(0.95, max(0.5, confidence)),
            source="llm",
        )
        cache[key] = asdict(result)
        _save_cache(cache)
        return result
    except Exception:
        return None


def classify_order_note(
    text: Optional[str],
    *,
    item_attr: Optional[str] = None,
) -> NoteClassification:
    """分级用户备注。待采购场景：high → 拦截；low → 忽略。"""
    note = _normalize(text or "")
    if not note:
        return NoteClassification(
            tier="none",
            topics=[],
            block_procurement=False,
            signal_type=None,
            reason="无备注",
            confidence=1.0,
            source="empty",
        )

    ruled = _classify_with_rules(note)
    if ruled is not None:
        return ruled

    llm = _classify_with_llm(note)
    if llm is not None:
        return llm

    # 无法判定：保守拦截，进指挥中心人工看
    return NoteClassification(
        tier="high",
        topics=["other"],
        block_procurement=True,
        signal_type="NOTE_REVIEW",
        reason="备注未能自动分类，需人工确认",
        confidence=0.62,
        source="rules",
    )


def enrich_row_note_fields(row: dict[str, Any]) -> dict[str, Any]:
    """在宽表行上附加备注分级（供 UI / 规则引擎）。"""
    note = row.get("usr_rmk") or row.get("ord_rmk") or row.get("rmk") or row.get("item_rmk")
    item_attr = row.get("item_attr") or row.get("item_attr_cn")
    cls = classify_order_note(str(note) if note else None, item_attr=str(item_attr) if item_attr else None)
    pub = cls.to_public()
    row["note_tier"] = pub["tier"]
    row["note_topics"] = pub["topics"]
    row["note_block_procurement"] = pub["block_procurement"]
    row["note_signal_type"] = pub["signal_type"]
    row["note_classify_reason"] = pub["reason"]
    row["note_classify_confidence"] = pub["confidence"]
    row["note_classify_source"] = pub["source"]
    return row

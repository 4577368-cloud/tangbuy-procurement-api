"""Admin 下单/接单失败回执归类：规则优先 + LLM 判别 + 关键词兜底。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Literal, Optional

from app.core.config import get_settings
from app.core.paths import data_dir

AdminErrorKey = Literal[
    "ADMIN_STOCK",
    "ADMIN_MOQ",
    "ADMIN_SKU",
    "ADMIN_MARGIN",
    "ADMIN_ERROR",
]

ADMIN_ERROR_LABELS: dict[AdminErrorKey, str] = {
    "ADMIN_STOCK": "疑似缺货",
    "ADMIN_MOQ": "起批量不满足",
    "ADMIN_SKU": "规格不符",
    "ADMIN_MARGIN": "价格/毛利异常",
    "ADMIN_ERROR": "平台下单失败",
}

_VALID_KEYS = frozenset(ADMIN_ERROR_LABELS.keys())
_CACHE_PATH = data_dir() / "orders" / "admin-error-classify-cache.json"

# SKU ID 拉货源失败 / 下架：归疑似缺货，不进规格不符
_STOCK_OFFER_PATTERNS = [
    re.compile(r"sku\s*not\s*match\s*fetch\s*goods", re.I),
    re.compile(r"not\s*match\s*fetch\s*goods", re.I),
    re.compile(r"\bfetch\s+goods\b", re.I),
    re.compile(r"商品不存在|商品已下架|offer\s*(not\s*found|offline|removed)", re.I),
    re.compile(r"(?<!\d)404(?!\d)"),  # 页面 404，避免命中 skuId 中的数字串
    re.compile(r"下架|失效\s*sku|sku\s*失效|sku\s*不存在", re.I),
]

_STOCK_KEYWORDS = ("库存", "缺货", "没货", "无货", "out of stock")
_MOQ_KEYWORDS = ("起订", "起批量", "MOQ", "moq", "最小起")
_MARGIN_KEYWORDS = ("毛利", "价格", "金额", "运费", "差价")
# 真·规格属性对不上（有对账明细或明确属性不匹配）
_SKU_ATTR_PATTERNS = [
    re.compile(r"sku\s*属性信息不匹配", re.I),
    re.compile(r"属性信息不匹配", re.I),
    re.compile(r"需要[^;]{0,24}[:：].{0,40}1688查询的是", re.I),
    re.compile(r"规格不符|规格不匹配|尺码不[对符]|颜色不[对符]", re.I),
]


@dataclass
class AdminErrorClassification:
    key: AdminErrorKey
    label: str
    reason: str
    confidence: float
    source: Literal["rule", "llm", "keyword", "fallback"]


def _cache_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _load_cache() -> dict[str, dict]:
    path = _CACHE_PATH
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_cache(cache: dict[str, dict]) -> None:
    path = _CACHE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=0), encoding="utf-8")


def _result(key: AdminErrorKey, reason: str, *, confidence: float, source: str) -> AdminErrorClassification:
    return AdminErrorClassification(
        key=key,
        label=ADMIN_ERROR_LABELS[key],
        reason=reason,
        confidence=confidence,
        source=source,  # type: ignore[arg-type]
    )


def classify_admin_error_rules(message: str) -> Optional[AdminErrorClassification]:
    msg = str(message or "").strip()
    if not msg:
        return None
    lower = msg.lower()

    for pat in _STOCK_OFFER_PATTERNS:
        if pat.search(msg):
            return _result("ADMIN_STOCK", "货源/SKU 不可用或下架", confidence=0.95, source="rule")

    if any(k in msg for k in _STOCK_KEYWORDS) or "out of stock" in lower:
        return _result("ADMIN_STOCK", "回执含缺货/库存语义", confidence=0.9, source="rule")
    if re.search(r"\bstock\b", lower) and "sku" not in lower:
        return _result("ADMIN_STOCK", "回执含 stock", confidence=0.85, source="rule")

    for pat in _SKU_ATTR_PATTERNS:
        if pat.search(msg):
            return _result("ADMIN_SKU", "规格属性与 1688 不一致", confidence=0.95, source="rule")

    if any(k in msg for k in _MOQ_KEYWORDS):
        return _result("ADMIN_MOQ", "回执含起批量语义", confidence=0.9, source="rule")

    if any(k in msg for k in _MARGIN_KEYWORDS):
        return _result("ADMIN_MARGIN", "回执含价格/毛利语义", confidence=0.85, source="rule")

    if any(k in msg for k in ("操作频繁", "稍后再试", "限流", "rate limit", "timeout", "超时")):
        return _result("ADMIN_ERROR", "平台限流或临时失败", confidence=0.9, source="rule")

    return None


def classify_admin_error_keywords(message: str) -> AdminErrorClassification:
    """无规则/无 LLM 时的关键词兜底；裸 sku 不再一律规格不符。"""
    msg = str(message or "").strip()
    lower = msg.lower()

    if any(k in msg for k in _STOCK_KEYWORDS) or "out of stock" in lower:
        return _result("ADMIN_STOCK", "关键词:缺货", confidence=0.7, source="keyword")
    if any(k in msg for k in _MOQ_KEYWORDS):
        return _result("ADMIN_MOQ", "关键词:起批", confidence=0.7, source="keyword")
    if any(k in msg for k in ("属性", "颜色", "尺码", "规格")) and "sku" in lower:
        return _result("ADMIN_SKU", "关键词:规格属性", confidence=0.65, source="keyword")
    if any(k in msg for k in ("规格", "属性")):
        return _result("ADMIN_SKU", "关键词:规格", confidence=0.6, source="keyword")
    if any(k in msg for k in _MARGIN_KEYWORDS):
        return _result("ADMIN_MARGIN", "关键词:价格", confidence=0.65, source="keyword")
    return _result("ADMIN_ERROR", "未识别，兜底", confidence=0.5, source="fallback")


def _classify_with_llm(message: str) -> Optional[AdminErrorClassification]:
    settings = get_settings()
    if not settings.llm_configured:
        return None

    key = _cache_key(message)
    cache = _load_cache()
    if key in cache:
        try:
            row = cache[key]
            k = row.get("key")
            if k in _VALID_KEYS:
                return AdminErrorClassification(
                    key=k,  # type: ignore[arg-type]
                    label=ADMIN_ERROR_LABELS[k],  # type: ignore[index]
                    reason=str(row.get("reason") or "缓存"),
                    confidence=float(row.get("confidence") or 0.8),
                    source="llm",
                )
        except Exception:
            pass

    from app.services.agent.llm import chat_completion

    try:
        resp = chat_completion(
            [
                {
                    "role": "system",
                    "content": (
                        "你是跨境采购系统的 Admin/1688 下单失败回执分类器。"
                        "根据错误原文，只能归入下列之一：\n"
                        "- ADMIN_STOCK 疑似缺货：库存不足、缺货、商品/SKU 下架、链接失效、"
                        "PI sku not match fetch goods、拉取货源 SKU 失败、商品不存在\n"
                        "- ADMIN_SKU 规格不符：颜色/尺码/属性选错，或「sku属性信息不匹配」且带"
                        "「需要… / 1688查询的是…」对账明细\n"
                        "- ADMIN_MOQ 起批量不满足：起订量/MOQ 不够\n"
                        "- ADMIN_MARGIN 价格/毛利异常：毛利、运费、差价、金额异常\n"
                        "- ADMIN_ERROR 平台下单失败：限流、超时、操作频繁、其它未知\n"
                        "注意：仅含 skuId 且提示 not match fetch goods → ADMIN_STOCK，不是规格不符。\n"
                        '只输出 JSON：{"key":"ADMIN_STOCK|ADMIN_SKU|ADMIN_MOQ|ADMIN_MARGIN|ADMIN_ERROR",'
                        '"reason":"简短中文","confidence":0.0-1.0}'
                    ),
                },
                {"role": "user", "content": message},
            ]
        )
        raw = (resp.content or "").strip()
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            return None
        data = json.loads(m.group(0))
        k = str(data.get("key") or "").strip()
        if k not in _VALID_KEYS:
            return None
        result = _result(
            k,  # type: ignore[arg-type]
            str(data.get("reason") or "LLM 分类"),
            confidence=min(0.95, max(0.5, float(data.get("confidence") or 0.8))),
            source="llm",
        )
        cache[key] = asdict(result)
        _save_cache(cache)
        return result
    except Exception:
        return None


def classify_admin_error(
    message: str,
    *,
    allow_llm: bool = True,
) -> AdminErrorClassification:
    msg = str(message or "").strip()
    if not msg:
        return _result("ADMIN_ERROR", "空回执", confidence=1.0, source="fallback")

    ruled = classify_admin_error_rules(msg)
    if ruled is not None:
        return ruled

    if allow_llm:
        llm = _classify_with_llm(msg)
        if llm is not None:
            return llm

    return classify_admin_error_keywords(msg)

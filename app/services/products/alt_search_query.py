"""换供图搜：从标题+类目清洗出短 search_query（LLM 为主，规则降级）。

提示词为可迭代基线，后续可专门训练/调优结构化输出。
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Optional

from app.core.config import get_settings

# 基线 system prompt — 后续专项调优入口
ALT_QUERY_SYSTEM_PROMPT = """你是跨境采购「换供图搜」检索词清洗器。

任务：根据商品标题与类目，产出给 1688 以图搜图接口用的短中文 query。
图搜以图片为主召回外观同款；你输出的文只做主体纠偏（同框多物体时锁真正卖的那件）与品类约束，不是商品文案。

规则：
1. subject：真正销售主体（一个品名），忽略模特身上同框的衣服/背景干扰物。
2. search_query：简体中文，8～20 字为宜，最多 2 个主体相关词 + 0～2 个区分属性；禁止堆砌营销词、批发/跨境、多语言整段标题。
3. category_hint：一级品类短词（如 项链、桌布）。
4. hard_exclude：明显不是主体的干扰品类词（标题/画面里可能有但不应成为搜品结果的）。
5. 文必须与图片可同时成立：不要编造图上看不到、标题也未暗示的品类。
6. 只输出一行 JSON，不要解释。

JSON 模板：
{"subject":"珍珠项链","search_query":"珍珠项链 锁骨链","category_hint":"项链","hard_exclude":["连衣裙","女装"],"confidence":0.85,"reason":"标题主体是项链"}
"""


@dataclass
class AltSearchQueryPlan:
    subject: str
    search_query: str
    category_hint: str
    hard_exclude: list[str]
    confidence: float
    reason: str
    source: str  # llm | rules | empty


def _clip_query(text: str, max_len: int = 24) -> str:
    q = re.sub(r"\s+", " ", (text or "").strip())
    q = re.sub(
        r"(批发|代发|跨境|爆款|热销|一件代发|工厂|现货|包邮|直销|OEM|ODM)",
        " ",
        q,
        flags=re.I,
    )
    q = re.sub(r"\s+", " ", q).strip()
    return q[:max_len].strip()


def _category_context(product: dict[str, Any]) -> dict[str, str]:
    hs = product.get("hs_mapping") if isinstance(product.get("hs_mapping"), dict) else {}
    return {
        "category": str(product.get("category") or "").strip(),
        "category_cn_name": str(hs.get("category_cn_name") or "").strip(),
        "declare_cn_name": str(hs.get("declare_cn_name") or "").strip(),
        "platform_category_hint": str(product.get("platform_category_hint") or "").strip(),
        "category_status": str(product.get("category_status") or "").strip(),
    }


def _rules_fallback(title: str, cat: dict[str, str]) -> AltSearchQueryPlan:
    """LLM 不可用时：类目中名优先，否则标题前若干汉字词。"""
    subject = (
        cat.get("declare_cn_name")
        or cat.get("category_cn_name")
        or cat.get("category")
        or ""
    ).strip()
    if not subject:
        cjk = re.findall(r"[\u4e00-\u9fff]{2,8}", title or "")
        # 取较短靠后的品名词倾向（粗略）
        subject = cjk[-1] if cjk else ""
        if not subject:
            latin = re.findall(r"[A-Za-z]{3,16}", title or "")
            subject = latin[0] if latin else ""

    subject = _clip_query(subject, 12)
    hint = _clip_query(
        cat.get("category_cn_name") or cat.get("category") or subject,
        12,
    )
    query = _clip_query(f"{subject} {hint}".strip() if hint and hint not in subject else subject)
    return AltSearchQueryPlan(
        subject=subject,
        search_query=query or subject,
        category_hint=hint or subject,
        hard_exclude=[],
        confidence=0.45 if subject else 0.0,
        reason="规则降级：类目/标题截取",
        source="rules" if subject else "empty",
    )


def _parse_llm_json(raw: str) -> Optional[dict[str, Any]]:
    text = (raw or "").strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _normalize_plan(data: dict[str, Any], *, source: str) -> Optional[AltSearchQueryPlan]:
    subject = _clip_query(str(data.get("subject") or ""), 16)
    search_query = _clip_query(str(data.get("search_query") or subject), 24)
    if not search_query and not subject:
        return None
    if not search_query:
        search_query = subject
    if not subject:
        subject = search_query.split()[0] if search_query else ""
    hint = _clip_query(str(data.get("category_hint") or ""), 16)
    excludes_raw = data.get("hard_exclude") or []
    excludes: list[str] = []
    if isinstance(excludes_raw, list):
        for x in excludes_raw:
            t = _clip_query(str(x), 12)
            if t and t not in excludes and t not in subject:
                excludes.append(t)
    try:
        confidence = float(data.get("confidence") or 0.7)
    except (TypeError, ValueError):
        confidence = 0.7
    reason = str(data.get("reason") or "").strip()[:80]
    return AltSearchQueryPlan(
        subject=subject,
        search_query=search_query,
        category_hint=hint,
        hard_exclude=excludes[:8],
        confidence=min(0.95, max(0.3, confidence)),
        reason=reason or ("LLM 清洗" if source == "llm" else "规则"),
        source=source,
    )


def _llm_plan(title: str, cat: dict[str, str]) -> Optional[AltSearchQueryPlan]:
    settings = get_settings()
    if not settings.llm_configured:
        return None

    from app.services.agent.llm import chat_completion

    user_payload = {
        "title": title,
        "category": cat.get("category") or None,
        "category_cn_name": cat.get("category_cn_name") or None,
        "declare_cn_name": cat.get("declare_cn_name") or None,
        "platform_category_hint": cat.get("platform_category_hint") or None,
        "category_status": cat.get("category_status") or None,
    }
    try:
        resp = chat_completion(
            [
                {"role": "system", "content": ALT_QUERY_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(user_payload, ensure_ascii=False),
                },
            ]
        )
        data = _parse_llm_json(resp.content or "")
        if not data:
            return None
        return _normalize_plan(data, source="llm")
    except Exception:
        return None


def build_alt_search_query_plan(product: dict[str, Any]) -> AltSearchQueryPlan:
    title = str(product.get("product_name") or product.get("title") or "").strip()
    cat = _category_context(product)
    llm = _llm_plan(title, cat)
    if llm and llm.search_query:
        return llm
    return _rules_fallback(title, cat)


def plan_to_store_fields(plan: AltSearchQueryPlan) -> dict[str, Any]:
    return {
        "alt_search_query": plan.search_query or None,
        "alt_search_subject": plan.subject or None,
        "alt_search_category_hint": plan.category_hint or None,
        "alt_search_hard_exclude": plan.hard_exclude or [],
        "alt_search_query_meta": {
            "confidence": plan.confidence,
            "reason": plan.reason,
            "source": plan.source,
        },
    }


def candidate_title_ok(title: str, plan: AltSearchQueryPlan) -> bool:
    """召回后硬过滤：命中 hard_exclude 且未体现 subject 则丢弃。"""
    t = (title or "").lower()
    if not t:
        return True
    subject = (plan.subject or "").strip()
    subject_hit = bool(subject) and subject.lower() in t
    for ex in plan.hard_exclude:
        ex_l = ex.lower()
        if len(ex_l) < 2:
            continue
        if ex_l in t and not subject_hit:
            return False
    return True


def plan_as_dict(plan: AltSearchQueryPlan) -> dict[str, Any]:
    return asdict(plan)

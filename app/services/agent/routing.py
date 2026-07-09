"""用户输入确定性路由（对齐 message-input-routing.ts）。"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from app.services.agent.followup import (
    looks_like_order_id_only,
    resolve_followup_order_id,
    resolve_followup_question,
)

PRODUCT_FIND_RE = re.compile(
    r"搜|找|查|推荐|选品|选几款|找几款|给我.*(?:款|几个)|有没有|流行|热门|爆款|同款|有哪些",
    re.I,
)
PRODUCT_COMPARE_RE = re.compile(r"比价|比一下|哪个便宜|哪家便宜|对比.*价", re.I)
ORDER_FOLLOWUP_RE = re.compile(
    r"催|发货|物流|改价|提醒商家|什么时候发|到货|追踪|催一下|跟进订单", re.I
)
FUZZY_SOURCING_RE = re.compile(
    r"采购|要买|进货|备货|订(?:货|购)|拿(?:货|一批)|需要\s*\d+\s*[件个套批箱]", re.I
)
MERCHANT_INQUIRY_RE = re.compile(
    r"问商家|联系商家|咨询商家|卖家|商家能|能不能定制|MOQ|起订|旺旺|定制|开模", re.I
)


def strip_trailing_punct(url: str) -> str:
    return re.sub(r"[),.。，；;]+$", "", url)


def extract_image_urls(text: str) -> list[str]:
    patterns = [
        re.compile(r"https?://[^\s<>\"']+\.(?:jpg|jpeg|png|webp|gif)(?:\?[^\s<>\"']*)?", re.I),
        re.compile(r"https?://[^\s<>\"']*alicdn\.com/[^\s<>\"']+", re.I),
    ]
    found: list[str] = []
    seen: set[str] = set()
    for pat in patterns:
        for m in pat.finditer(text):
            u = strip_trailing_punct(m.group(0))
            if u not in seen:
                seen.add(u)
                found.append(u)
    return found


def extract_1688_offer_urls(text: str) -> list[str]:
    return [
        strip_trailing_punct(m.group(0))
        for m in re.finditer(r"https?://detail\.1688\.com/offer/\d+\.html[^\s<>\"']*", text, re.I)
    ]


def looks_like_order_followup(text: str) -> bool:
    return bool(ORDER_FOLLOWUP_RE.search(text))


def looks_like_fuzzy_sourcing(text: str) -> bool:
    if extract_1688_offer_urls(text):
        return False
    return bool(FUZZY_SOURCING_RE.search(text))


def looks_like_merchant_inquiry(text: str) -> bool:
    return bool(MERCHANT_INQUIRY_RE.search(text))


def looks_like_product_compare(text: str) -> bool:
    return bool(PRODUCT_COMPARE_RE.search(text))


def looks_like_product_find(text: str) -> bool:
    if looks_like_merchant_inquiry(text) or looks_like_fuzzy_sourcing(text):
        return False
    return bool(PRODUCT_FIND_RE.search(text))


def looks_like_fabricated_products(text: str) -> bool:
    return bool(re.search(r"¥\d|detail\.1688\.com/offer/\d+", text)) and not text.startswith("❌")


def looks_like_fabricated_followup(text: str) -> bool:
    return bool(
        re.search(
            r"已.{0,12}(向商家)?发起催单|已向商家发起|商家回复后会?自动同步",
            text,
        )
    ) and "❌" not in text


_LEADING_FILLER = re.compile(
    r"^(帮我|请|想要|想找|搜索|找一下|推荐|给我|我要|找些|找一些)\s*",
    re.I,
)
_QUERY_NOISE = re.compile(
    r"(帮我|请|想要|想找|搜索|找一下|推荐|给我|我要|一些|几款|几个|有没有|偏向|风格|款式|的商品|商品|产品|的)",
)


def extract_product_search_query(text: str) -> str:
    q = text.strip()
    while True:
        nq = _LEADING_FILLER.sub("", q).strip()
        if nq == q:
            break
        q = nq
    q = re.sub(r"[？?！!。]+$", "", q).strip()
    q = _QUERY_NOISE.sub(" ", q)
    q = re.sub(r"\s+", " ", q).strip()
    return q[:80] or text.strip()[:80]


def build_supplychain_inquiry_args(user_text: str) -> dict[str, str]:
    questions: list[dict[str, str]] = []
    if re.search(r"MOQ|起订|起批量", user_text, re.I):
        questions.append({"question": "起订量 MOQ 是多少？", "type": "current"})
    if re.search(r"定制|logo|贴牌|OEM", user_text, re.I):
        questions.append({"question": "是否支持定制？定制方式和交期？", "type": "current"})
    if re.search(r"交货|交期|发货|货期", user_text, re.I):
        questions.append({"question": "交货期多久？", "type": "current"})
    if re.search(r"价格|多少钱|报价|便宜", user_text, re.I):
        questions.append({"question": "批发价格是多少？", "type": "current"})
    if not questions:
        questions.append({"question": "请报价并说明起订量", "type": "current"})
    qty = re.search(r"(\d+)\s*[件个套批箱]", user_text)
    return {
        "requirement": user_text.strip(),
        "questions": json.dumps(questions, ensure_ascii=False),
        "purchase_size": qty.group(1) if qty else "1",
        "inquiry_item_size": "3",
        "recall_item_size": "10",
    }


def build_merchant_consult_message(user_text: str, context: Optional[dict[str, Any]]) -> str:
    lines = ["【问商家】请向该商品卖家咨询并带回回复。"]
    if context:
        if context.get("splr_item_id"):
            lines.append(f"1688 offer：{context['splr_item_id']}")
        if context.get("item_nm"):
            lines.append(f"商品名：{context['item_nm']}")
    lines.append(f"采购员问题：{user_text.strip()}")
    return "\n".join(lines)


def resolve_order_followup_route(
    user_text: str,
    allowed: set[str],
    context: Optional[dict[str, Any]],
    intent: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    if "order_inquiry_send" not in allowed:
        return None
    order_id = resolve_followup_order_id(user_text, context)
    if not order_id:
        return None
    if intent != "followup" and not looks_like_order_followup(user_text) and not looks_like_order_id_only(user_text):
        return None
    question = resolve_followup_question(user_text, order_id, context)
    return {"tool": "order_inquiry_send", "args": {"order_id": order_id, "question": question}}


def resolve_product_compare_route(user_text: str, allowed: set[str]) -> Optional[dict[str, Any]]:
    if "product_compare" not in allowed or not looks_like_product_compare(user_text):
        return None
    images = extract_image_urls(user_text)
    offers = extract_1688_offer_urls(user_text)
    if images:
        return {"tool": "product_compare", "args": {"image_url": images[0], "limit": "3"}}
    if offers:
        return {"tool": "product_compare", "args": {"url": offers[0], "limit": "3"}}
    return None


def resolve_instant_product_route(user_text: str, allowed: set[str]) -> Optional[dict[str, Any]]:
    cmp_route = resolve_product_compare_route(user_text, allowed)
    if cmp_route:
        return cmp_route
    images = extract_image_urls(user_text)
    offers = extract_1688_offer_urls(user_text)
    if images and "product_image_search" in allowed:
        return {"tool": "product_image_search", "args": {"image_url": images[0], "limit": "10"}}
    if offers and "product_link_search" in allowed:
        args: dict[str, str] = {"url": offers[0], "limit": "10"}
        if images:
            args["image_url"] = images[0]
        return {"tool": "product_link_search", "args": args}
    if looks_like_product_find(user_text) and "product_text_search" in allowed:
        return {
            "tool": "product_text_search",
            "args": {"query": extract_product_search_query(user_text), "limit": "10"},
        }
    return None


_SKILL_DEFAULT_TOOL: dict[str, str] = {
    "order-followup": "order_inquiry_send",
    "1688-product-find": "product_text_search",
    "product-compare": "product_compare",
    "supplychain-procurement": "supplychain_inquiry_start",
    "1688-sourcing": "procurement_inquiry",
    "newton-cloud": "newton_consult",
}


def _build_evolution_route_args(
    tool: str,
    user_text: str,
    context: Optional[dict[str, Any]],
) -> dict[str, str]:
    if tool == "order_inquiry_send":
        order_id = resolve_followup_order_id(user_text, context) or ""
        question = resolve_followup_question(user_text, order_id, context) if order_id else user_text.strip()
        return {"order_id": order_id, "question": question}
    if tool == "product_text_search":
        return {"query": extract_product_search_query(user_text), "limit": "10"}
    if tool == "supplychain_inquiry_start":
        return build_supplychain_inquiry_args(user_text)
    if tool == "newton_consult":
        message = (
            build_merchant_consult_message(user_text, context)
            if looks_like_merchant_inquiry(user_text)
            else user_text.strip()
        )
        return {"message": message, "user_question": user_text.strip()}
    return {}


def resolve_evolution_route(
    user_text: str,
    allowed: set[str],
    context: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """自进化路由补丁：关键词命中时优先路由到目标技能对应工具。"""
    try:
        from app.services.evolution.patch_generator import get_active_route_patches

        rules = get_active_route_patches()
    except Exception:
        return None

    text = user_text.strip()
    if not text:
        return None

    for rule in rules:
        pattern = (rule.get("trigger_pattern") or "").strip()
        if not pattern or pattern not in text:
            continue
        tool = _SKILL_DEFAULT_TOOL.get(rule.get("target_skill") or "")
        if not tool or tool not in allowed:
            continue
        args = _build_evolution_route_args(tool, text, context)
        if tool == "order_inquiry_send" and not args.get("order_id"):
            continue
        return {"tool": tool, "args": args}
    return None


def resolve_deterministic_route(
    user_text: str,
    intent: Optional[str],
    allowed: set[str],
    context: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    evolution = resolve_evolution_route(user_text, allowed, context)
    if evolution:
        return evolution

    if intent == "sourcing":
        if looks_like_fuzzy_sourcing(user_text) and "supplychain_inquiry_start" in allowed:
            return {
                "tool": "supplychain_inquiry_start",
                "args": build_supplychain_inquiry_args(user_text),
            }
        instant = resolve_instant_product_route(user_text, allowed)
        if instant:
            return instant
        return None

    if intent == "followup":
        return resolve_order_followup_route(user_text, allowed, context, intent="followup")

    if intent == "consult":
        if looks_like_fuzzy_sourcing(user_text) and "supplychain_inquiry_start" in allowed:
            return {
                "tool": "supplychain_inquiry_start",
                "args": build_supplychain_inquiry_args(user_text),
            }
        if not looks_like_merchant_inquiry(user_text):
            instant = resolve_instant_product_route(user_text, allowed)
            if instant:
                return instant
        if "newton_consult" not in allowed:
            return None
        message = (
            build_merchant_consult_message(user_text, context)
            if looks_like_merchant_inquiry(user_text)
            else user_text.strip()
        )
        return {
            "tool": "newton_consult",
            "args": {"message": message, "user_question": user_text.strip()},
        }

    if looks_like_fuzzy_sourcing(user_text) and "supplychain_inquiry_start" in allowed:
        return {
            "tool": "supplychain_inquiry_start",
            "args": build_supplychain_inquiry_args(user_text),
        }

    if looks_like_merchant_inquiry(user_text) and "newton_consult" in allowed:
        return {
            "tool": "newton_consult",
            "args": {
                "message": build_merchant_consult_message(user_text, context),
                "user_question": user_text.strip(),
            },
        }

    followup = resolve_order_followup_route(user_text, allowed, context)
    if followup:
        return followup

    return resolve_instant_product_route(user_text, allowed)

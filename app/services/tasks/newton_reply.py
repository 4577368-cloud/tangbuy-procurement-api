"""牛顿回复识别（对齐 src/lib/tasks/newton-reply-detect.ts）。"""

from __future__ import annotations

import re
from typing import Any, Optional

ACK_PATTERNS = [
    re.compile(p)
    for p in [
        r"已向商家发起",
        r"商家收到您的询问",
        r"届时您将收到",
        r"将会.*回复",
        r"尽快回复",
        r"已为您发起",
        r"已发起.*询盘",
        r"询盘已提交",
        r"处理中",
        r"通常需要几分钟",
        r"无需记住任务编号",
        r"请稍候",
        r"正在为您",
        r"已收到您的",
        r"已为您向[\d一二三四五六七八九十]+家",
        r"已询盘的工厂",
        r"询盘表格中查看",
        r"询盘表格",
        r"您可以稍后.*查看商家",
    ]
]

MERCHANT_SIGNALS = [
    re.compile(p)
    for p in [
        r"已发货",
        r"今天发|明天发|后天发|下午发|上午发",
        r"发货时间",
        r"预计.*发",
        r"物流单号|快递单号|运单号",
        r"单号[:：]\s*[A-Za-z0-9]{8,}",
        r"SF\d|YT\d|ZTO|STO|YD\d",
        r"缺货|没货|库存不足|延期|晚发",
        r"不能发|无法发|不支持发",
    ]
]

DIRECT_MERCHANT_ANSWER_RE = re.compile(r"(?:商家|卖家|厂家)(?:回复|表示|称|答复)")
ASK_SELLER_ANSWER_RE = re.compile(
    r"不支持定制|无法定制|不能定制|可以开模|不支持开模|商家回复|卖家表示|厂家答复|旺旺"
)
ASK_SELLER_INTERIM_RE = re.compile(
    r"已为您向|已询盘的工厂|询盘表格|发起询盘|向供应商发起|部分推荐商品|已为您找到.*款|供应商收到|稍后.*查看.*回复|匹配.*供应商|源头工厂",
    re.I,
)


def is_newton_platform_runtime_error(text: Optional[str]) -> bool:
    if not text:
        return False
    return bool(re.search(r"initRuntime failed|RuntimeExecutor", text, re.I))


def format_newton_platform_error(text: Optional[str]) -> str:
    if not text or not text.strip():
        return "牛顿云执行失败，请重新发起"
    if is_newton_platform_runtime_error(text):
        return "牛顿云执行环境启动失败，未联系到商家。请稍后重新发起。"
    return re.sub(r"\[RuntimeExecutor\]\s*", "", text, flags=re.I).strip()


def is_newton_bulk_sourcing_ack(text: str) -> bool:
    t = text.strip()
    if not t:
        return False
    patterns = [
        r"已为您向[\d一二三四五六七八九十]+家",
        r"目前已有\s*[\d一二三四五六七八九十]+\s*家",
        r"[\d一二三四五六七八九十]+\s*家供应商",
        r"部分推荐商品如下",
        r"已为您找到.*(?:款|家|供应商)",
        r"向供应商发起了",
        r"供应商收到您的询盘",
        r"已询盘的工厂",
        r"询盘表格",
    ]
    return any(re.search(p, t) for p in patterns)


def join_newton_text(messages: Optional[list[dict[str, Any]]], content: Optional[str]) -> str:
    parts = [m.get("content", "") for m in (messages or []) if m.get("content")]
    if content:
        parts.append(content)
    return "\n\n".join(p for p in parts if p).strip()


def is_likely_merchant_reply(text: str, question: Optional[str] = None) -> bool:
    t = text.strip()
    if not t or is_newton_platform_runtime_error(t) or is_newton_bulk_sourcing_ack(t):
        return False
    if any(p.search(t) for p in ACK_PATTERNS) and not any(p.search(t) for p in MERCHANT_SIGNALS):
        return False
    if DIRECT_MERCHANT_ANSWER_RE.search(t):
        return True
    if any(p.search(t) for p in MERCHANT_SIGNALS):
        return True
    if ASK_SELLER_ANSWER_RE.search(t):
        return True
    if len(t) >= 24:
        return True
    return False


def pick_order_followup_reply(
    messages: Optional[list[dict[str, Any]]],
    content: Optional[str],
    question: str,
) -> Optional[str]:
    combined = join_newton_text(messages, content)
    if not combined:
        return None
    return combined if is_likely_merchant_reply(combined, question) else None


def pick_ask_seller_consult_reply(
    messages: Optional[list[dict[str, Any]]],
    content: Optional[str],
    question: str,
) -> Optional[str]:
    combined = join_newton_text(messages, content)
    if not combined or is_newton_bulk_sourcing_ack(combined):
        return None
    if ASK_SELLER_INTERIM_RE.search(combined) and not DIRECT_MERCHANT_ANSWER_RE.search(combined):
        return None
    if DIRECT_MERCHANT_ANSWER_RE.search(combined) or ASK_SELLER_ANSWER_RE.search(combined):
        return combined
    return combined if is_likely_merchant_reply(combined, question) else None

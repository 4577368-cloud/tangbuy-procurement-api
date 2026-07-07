"""订单催单：解析、发送、牛顿 message。"""

from __future__ import annotations

import re
import time
from typing import Any, Optional

from app.integrations.newton.tasks import NEWTON_ORDER_INQUIRY_SKILL, task_create
from app.integrations.skills_gateway.order_inquiry import send_order_inquiry

DEFAULT_QUESTION = "请提醒商家尽快发货，并告知预计发货时间"


def normalize_followup_order_id(raw: str) -> Optional[str]:
    trimmed = raw.strip()
    if not trimmed:
        return None
    if re.fullmatch(r"\d{10,22}", trimmed):
        return trimmed
    runs = re.findall(r"\d{10,22}", trimmed)
    return max(runs, key=len) if runs else None


def extract_order_ids(text: str) -> list[str]:
    trimmed = text.strip()
    if not trimmed:
        return []
    sole = normalize_followup_order_id(trimmed)
    if sole and looks_like_order_id_only(trimmed):
        return [sole]
    return list(dict.fromkeys(re.findall(r"\b\d{10,22}\b", trimmed)))


def looks_like_order_id_only(text: str) -> bool:
    trimmed = text.strip()
    if not trimmed:
        return False
    oid = normalize_followup_order_id(trimmed)
    if not oid:
        return False
    rest = trimmed.replace(oid, "")
    rest = re.sub(r"[\s,，。.、\-_]", "", rest)
    rest = re.sub(r"^(催单|催)?$", "", rest)
    return len(rest) == 0


def resolve_followup_order_id(user_text: str, context: Optional[dict[str, Any]]) -> Optional[str]:
    for oid in extract_order_ids(user_text):
        return oid
    if context and context.get("pur_no"):
        return normalize_followup_order_id(str(context["pur_no"]))
    return None


def build_default_followup_question(context: Optional[dict[str, Any]]) -> str:
    if context and str(context.get("exprs_no", "")).strip():
        return f"物流单号 {context['exprs_no']}，请核实发货进度及最新轨迹"
    return DEFAULT_QUESTION


def resolve_followup_question(
    user_text: str,
    order_id: str,
    context: Optional[dict[str, Any]],
) -> str:
    q = user_text.strip()
    q = re.sub(rf"\b{re.escape(order_id)}\b", " ", q)
    q = re.sub(r"^(请|帮我|麻烦|催单[:：]?)\s*", "", q)
    q = re.sub(r"\s+", " ", q).strip()
    if len(q) >= 4:
        return q
    return build_default_followup_question(context)


def build_order_followup_newton_message(order_id: str, question: str) -> str:
    q = question.strip() or DEFAULT_QUESTION
    return f"催下这个订单发货 订单ID:{order_id}。{q}"


def execute_order_followup_send(order_id: str, question: str) -> dict[str, Any]:
    message = build_order_followup_newton_message(order_id, question)
    outcome = task_create(message, skill_code=NEWTON_ORDER_INQUIRY_SKILL)

    if outcome.ok and outcome.result and outcome.result.get("success") and outcome.result.get("taskId"):
        r = outcome.result
        return {
            "success": True,
            "taskId": r.get("taskId"),
            "data": {
                "sessionId": r.get("sessionId"),
                "status": r.get("status"),
                "question": question,
                "order_id": order_id,
            },
            "markdown": f"已提交催单（{order_id}），回复见任务中心。",
        }

    gateway = send_order_inquiry(order_id, question)
    if gateway.get("success"):
        return {
            "success": True,
            "taskId": f"gateway-{int(time.time() * 1000)}",
            "gatewayOnly": True,
            "data": {
                "order_id": order_id,
                "question": question,
                "gateway_sent": True,
                "status": "SENT",
            },
            "markdown": gateway.get("markdown")
            or f"已发往商家（{order_id}）。长程任务暂不可用，回复请在旺旺或 1688 订单页查看。",
        }

    err = outcome.error or gateway.get("error") or "长程与网关均失败"
    return {
        "success": False,
        "error": err,
        "markdown": f"❌ 催单失败：{err}",
    }

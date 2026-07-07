"""工具调用后登记任务（写入 tasks.json）。"""

from __future__ import annotations

import random
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional

from app.services.agent.routing import looks_like_merchant_inquiry
from app.services.tasks import persistence

INQUIRY_WAIT_MS = 20 * 60 * 1000


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _new_id(prefix: str) -> str:
    return f"{prefix}-{int(time.time() * 1000)}-{random.randint(0, 99999):05d}"


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _pick_offer_url(text: str) -> Optional[str]:
    m = re.search(r"https?://detail\.1688\.com/offer/\d+\.html[^\s]*", text)
    return m.group(0) if m else None


def register_task_from_tool(
    skill_id: str,
    tool_name: str,
    args: dict[str, str],
    result: dict[str, Any],
) -> Optional[dict[str, Any]]:
    if not result.get("success"):
        return None

    if tool_name in ("supplychain_inquiry_start", "supplychain_inquiry_query"):
        return None

    tasks = persistence.load_and_repair()
    created = _now_iso()
    task: Optional[dict[str, Any]] = None

    if skill_id == "1688-sourcing" and tool_name == "procurement_inquiry":
        has_url = bool(result.get("requirementUrl"))
        task = {
            "id": _new_id("task-src"),
            "type": "sourcing_inquiry",
            "skill_id": skill_id,
            "skill_name": "1688 寻源询盘",
            "title": f"{args.get('offerName', '')} × {args.get('count', '')}",
            "subtitle": args.get("demand", ""),
            "status": "ready" if has_url else "in_progress",
            "created_at": created,
            "updated_at": created,
            "external_ref": result.get("requirementUrl"),
            "payload": {
                "offer_name": args.get("offerName"),
                "count": args.get("count"),
                "demand": args.get("demand"),
                "requirement_url": result.get("requirementUrl"),
            },
            "timeline": [
                {"at": created, "label": "发起寻源询盘", "detail": "1688 平台匹配供应商中"},
                *(
                    [
                        {
                            "at": created,
                            "label": "获取询盘页链接",
                            "detail": "报价与匹配结果在 1688 平台更新，本系统暂不支持拉回",
                        }
                    ]
                    if has_url
                    else []
                ),
            ],
            "result_summary": (
                "询盘已创建。供应商报价在 1688 询盘详情页查看，平台异步更新，不会自动推回任务中心。"
                if has_url
                else "询盘已提交，等待平台返回详情链接"
            ),
        }

    elif skill_id == "inquiry-1688" and tool_name == "inquiry_submit" and result.get("taskId"):
        query_available = (
            datetime.now(timezone.utc).timestamp() * 1000 + INQUIRY_WAIT_MS
        )
        qa_iso = datetime.fromtimestamp(query_available / 1000, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%f"
        )[:-3] + "Z"
        task = {
            "id": _new_id("task-inq"),
            "type": "inquiry_1688",
            "skill_id": skill_id,
            "skill_name": "1688 商家询盘",
            "title": _truncate(args.get("question") or "商家询盘", 40),
            "subtitle": _truncate(args.get("item") or "", 36),
            "status": "in_progress",
            "created_at": created,
            "updated_at": created,
            "external_ref": result["taskId"],
            "payload": {
                "alphashop_task_id": result["taskId"],
                "item": args.get("item"),
                "question": args.get("question"),
                "quantity": args.get("quantity"),
                "query_available_at": qa_iso,
            },
            "timeline": [
                {
                    "at": created,
                    "label": "提交询盘",
                    "detail": f"任务已创建，约 20 分钟后可查询（{qa_iso[:16]}）",
                }
            ],
            "result_summary": "等待商家回复",
        }

    elif not result.get("taskId"):
        return None

    elif skill_id == "newton-cloud" and tool_name == "newton_consult":
        data = result.get("data") or {}
        question = data.get("question") or args.get("user_question") or args.get("message") or "智能咨询"
        ask_seller = data.get("ask_seller") if data.get("ask_seller") is not None else looks_like_merchant_inquiry(question)
        item_url = _pick_offer_url(question) or _pick_offer_url(args.get("message", ""))
        task = {
            "id": _new_id("task-newton"),
            "type": "newton_agent",
            "skill_id": "newton-cloud",
            "skill_name": "问商家" if ask_seller else "智能咨询",
            "title": _truncate(str(question), 40),
            "status": "in_progress" if ask_seller else "in_progress",
            "created_at": created,
            "updated_at": created,
            "external_ref": result["taskId"],
            "payload": {
                "newton_task_id": result["taskId"],
                "session_id": data.get("sessionId"),
                "question": question,
                "ask_seller": ask_seller,
                "newton_status": data.get("status"),
                **({"item_url": item_url} if item_url else {}),
            },
            "timeline": [
                {
                    "at": created,
                    "label": "已向商家发起询问" if ask_seller else "已提交咨询",
                    "detail": "等待回复，结果自动带回" if ask_seller else "牛顿云处理中",
                }
            ],
            "result_summary": "已提交，等待商家回复" if ask_seller else "处理中…",
        }

    elif skill_id == "order-followup" and tool_name == "order_inquiry_send":
        data = result.get("data") or {}
        order_id = data.get("order_id") or args.get("order_id")
        question = data.get("question") or args.get("question") or "催单"
        gateway_only = bool(data.get("gateway_sent")) or str(result.get("taskId", "")).startswith("gateway-")
        task = {
            "id": _new_id("task-ord"),
            "type": "order_followup",
            "skill_id": skill_id,
            "skill_name": "催单",
            "title": _truncate(str(question), 40),
            "subtitle": f"订单 {order_id}",
            "status": "completed" if gateway_only else "in_progress",
            "created_at": created,
            "updated_at": created,
            "completed_at": created if gateway_only else None,
            "order_no": order_id,
            "external_ref": result["taskId"],
            "payload": {
                "order_id": order_id,
                "question": question,
                **({} if gateway_only else {"newton_task_id": result["taskId"]}),
                "session_id": data.get("sessionId"),
                "newton_status": data.get("status"),
                **({"gateway_sent": True} if gateway_only else {}),
            },
            "timeline": [
                {
                    "at": created,
                    "label": "已改走网关" if gateway_only else "已向商家发起催单",
                    "detail": "长程异常，询盘已直发商家"
                    if gateway_only
                    else "牛顿云长程任务处理中，商家回复后自动带回",
                }
            ],
            "result_summary": (
                persistence.ORDER_FOLLOWUP_GATEWAY_SUMMARY
                if gateway_only
                else "已提交，等待商家回复"
            ),
        }

    if not task:
        return None

    tasks.insert(0, task)
    persistence.save_runtime_tasks(tasks)
    return task

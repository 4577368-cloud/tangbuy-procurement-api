"""供应链询盘任务创建（对齐 task-store.ts createSupplychainInquiryTask）。"""

from __future__ import annotations

import json
import os
import random
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.integrations.newton.tasks import task_create
from app.integrations.skill_cli import run_supplychain_inquiry
from app.services.tasks import persistence

SUPPLYCHAIN_QUERY_WAIT_MS = 5 * 60 * 1000


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _new_id(prefix: str) -> str:
    return f"{prefix}-{int(time.time() * 1000)}-{random.randint(0, 99999):05d}"


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def is_supplychain_gateway_denied(error: Optional[str]) -> bool:
    if not error:
        return False
    return bool(re.search(r"403|NEWTON_CLIENT_ONLY|牛顿客户端", error))


def build_supplychain_newton_message(
    requirement: str,
    questions: list[dict[str, str]],
) -> str:
    q_lines = (
        "\n".join(f"{i + 1}. {q.get('question', '')}" for i, q in enumerate(questions))
        if questions
        else "1. 请报价并说明起订量"
    )
    return "\n".join(
        [
            "【任务类型】模糊批量采购寻源询盘：向1688平台匹配供应商并询盘，带回商家回复。",
            "【禁止】编造已收到回复、自行匹配无关供应商充数。",
            f"【采购需求】{requirement.strip()}",
            f"【必问问题】\n{q_lines}",
        ]
    )


def _is_newton_api_configured() -> bool:
    return bool(os.environ.get("ALIBABA_NEWTON_APIKEY", "").strip())


def start_supplychain_inquiry(
    requirement: str,
    questions: list[dict[str, str]],
    *,
    purchase_size: int = 1,
    inquiry_item_size: int = 3,
    recall_item_size: int = 10,
    image_urls: Optional[list[str]] = None,
) -> dict[str, Any]:
    args = [
        "--requirement",
        requirement.strip(),
        "--purchase-size",
        str(purchase_size),
        "--inquiry-item-size",
        str(inquiry_item_size),
        "--recall-item-size",
        str(recall_item_size),
        "--questions",
        json.dumps(questions, ensure_ascii=False),
    ]
    if image_urls:
        args.extend(["--image-url", ",".join(u for u in image_urls if u)])
    result = run_supplychain_inquiry(args)
    if not result.get("success"):
        return {"ok": False, "error": result.get("error") or result.get("markdown")}
    data = result.get("data") or {}
    if isinstance(data, dict) and data.get("success") is False:
        return {"ok": False, "error": data.get("markdown") or data.get("message")}
    root = data if isinstance(data, dict) else {}
    instance_id = str(root.get("instanceId") or "").strip()
    if not instance_id:
        return {"ok": False, "error": "创建成功但未返回 instanceId"}
    return {"ok": True, "instance_id": instance_id}


def _add_runtime_task(task: dict[str, Any]) -> dict[str, Any]:
    persistence.append_runtime_task(task)
    return task


def create_supplychain_inquiry_via_newton(
    requirement: str,
    questions: list[dict[str, str]],
) -> dict[str, Any]:
    if not _is_newton_api_configured():
        return {
            "error": "供应链直连网关仅牛顿客户端可用。请在 .env.local 配置 ALIBABA_NEWTON_APIKEY 后重试。"
        }
    message = build_supplychain_newton_message(requirement, questions)
    created = _now_iso()
    outcome = task_create(message)
    if not outcome.ok or not outcome.result:
        return {"error": outcome.error or "牛顿云调用失败"}
    r = outcome.result
    if not r.get("success") or not r.get("taskId"):
        return {"error": r.get("error") or "牛顿云未返回 taskId"}
    title_source = requirement.strip() or (questions[0].get("question") if questions else "供应链询盘")
    task = {
        "id": _new_id("task-sc"),
        "type": "newton_agent",
        "skill_id": "1688-supplychain-procurement",
        "skill_name": "供应链询盘",
        "title": _truncate(title_source, 40),
        "status": "in_progress",
        "created_at": created,
        "updated_at": created,
        "external_ref": r["taskId"],
        "payload": {
            "newton_task_id": r["taskId"],
            "session_id": r.get("sessionId"),
            "question": requirement.strip(),
            "fuzzy_sourcing": True,
            "newton_status": r.get("status"),
        },
        "timeline": [{"at": created, "label": "已发起询盘", "detail": "牛顿云长程任务，商家回复自动带回"}],
        "result_summary": "询盘进行中，约数分钟后可查看",
    }
    return {"task": _add_runtime_task(task), "via": "newton"}


def create_supplychain_inquiry_task(
    requirement: str,
    questions: list[dict[str, str]],
    *,
    purchase_size: int = 1,
    inquiry_item_size: int = 3,
    recall_item_size: int = 10,
    image_urls: Optional[list[str]] = None,
) -> dict[str, Any]:
    outcome = start_supplychain_inquiry(
        requirement,
        questions,
        purchase_size=purchase_size,
        inquiry_item_size=inquiry_item_size,
        recall_item_size=recall_item_size,
        image_urls=image_urls,
    )
    if outcome.get("ok") and outcome.get("instance_id"):
        created = _now_iso()
        query_available = (
            datetime.now(timezone.utc) + timedelta(milliseconds=SUPPLYCHAIN_QUERY_WAIT_MS)
        ).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        title_source = requirement.strip() or (questions[0].get("question") if questions else "供应链询盘")
        instance_id = outcome["instance_id"]
        task = {
            "id": _new_id("task-sc"),
            "type": "supplychain_inquiry",
            "skill_id": "1688-supplychain-procurement",
            "skill_name": "供应链询盘",
            "title": _truncate(title_source, 40),
            "status": "in_progress",
            "created_at": created,
            "updated_at": created,
            "external_ref": instance_id,
            "payload": {
                "instance_id": instance_id,
                "requirement": requirement.strip(),
                "questions": questions,
                "purchase_size": purchase_size,
                "inquiry_item_size": inquiry_item_size,
                "recall_item_size": recall_item_size,
                "image_urls": image_urls,
                "query_available_at": query_available,
            },
            "timeline": [
                {
                    "at": created,
                    "label": "已发起询盘",
                    "detail": f"instanceId {instance_id[:8]}…",
                }
            ],
            "result_summary": "询盘进行中，约 5 分钟后可查询",
        }
        return {"task": _add_runtime_task(task), "via": "direct"}

    if is_supplychain_gateway_denied(outcome.get("error")):
        return create_supplychain_inquiry_via_newton(requirement, questions)
    return {"error": outcome.get("error") or "发起失败"}


def parse_supplychain_query(instance_id: str, raw: Any) -> Optional[dict[str, Any]]:
    if not raw or not isinstance(raw, dict):
        return None
    root = raw

    def pick_str(obj: Any, *keys: str) -> Optional[str]:
        if not isinstance(obj, dict):
            return None
        for k in keys:
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return None

    def nested(obj: dict[str, Any], keys: list[str]) -> Any:
        cur: Any = obj
        for k in keys:
            if not isinstance(cur, dict):
                return None
            cur = cur.get(k)
        return cur

    result_items = root.get("result")
    if not isinstance(result_items, list):
        inner = nested(root, ["data", "result"])
        result_items = inner if isinstance(inner, list) else []

    inquired = []
    for item in result_items:
        if not isinstance(item, dict):
            continue
        summary = pick_str(item, "inquirySummary", "inquiry_summary", "summary")
        if summary:
            inquired.append(item)

    stage = pick_str(root, "stage") or pick_str(nested(root, ["data"]) or {}, "stage") or ""
    status = pick_str(root, "status") or pick_str(nested(root, ["data"]) or {}, "status") or ""
    if not status:
        status = "finish" if inquired else "running"

    snapshot = {
        "instance_id": instance_id,
        "stage": stage,
        "status": status,
        "total_items": len(result_items),
        "inquired_items": inquired,
    }
    return {"snapshot": snapshot, "reply_count": len(inquired)}

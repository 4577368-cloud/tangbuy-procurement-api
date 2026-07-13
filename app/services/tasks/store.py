"""任务中心业务（牛顿长程轮询、网关兜底、列表统计）。"""

from __future__ import annotations

import os
import re
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from app.integrations.newton.tasks import is_terminal_status, task_get, task_kill
from app.integrations.skills_gateway.order_inquiry import send_order_inquiry
from app.services.tasks import persistence
from app.services.tasks.seeds import merge_tasks_with_seeds
from app.services.tasks.newton_reply import (
    format_newton_platform_error,
    is_newton_platform_runtime_error,
    is_order_followup_ack,
    join_newton_text,
    pick_ask_seller_consult_reply,
    pick_order_followup_reply,
)

NEWTON_GET_MIN_MS = int(os.environ.get("NEWTON_TASK_GET_MIN_MS", "120000"))
NEWTON_GET_BACKOFF_MS = int(os.environ.get("NEWTON_TASK_GET_BACKOFF_MS", "180000"))
NEWTON_GET_GRACE_MS = int(os.environ.get("NEWTON_TASK_GET_GRACE_MS", "180000"))

TERMINAL_STATUSES = frozenset({"completed", "failed", "killed"})
ACTIVE_STATUSES = frozenset({"in_progress", "ready", "needs_review"})

OPERATION_TASK_TYPES = (
    "order_followup",
    "newton_agent",
    "supplychain_inquiry",
    "inquiry_1688",
    "sourcing_inquiry",
    "auto_release",
    "category_mapping",
)

_newton_get_cache: dict[str, dict[str, int]] = {}
_refresh_locks: dict[str, threading.Lock] = {}
_store_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _push_timeline(task: dict[str, Any], label: str, detail: Optional[str] = None) -> None:
    task.setdefault("timeline", []).append({"at": _now_iso(), "label": label, "detail": detail})
    task["updated_at"] = _now_iso()


def _push_timeline_if_new(task: dict[str, Any], label: str, detail: Optional[str] = None) -> None:
    timeline = task.get("timeline") or []
    if timeline and timeline[-1].get("label") == label and timeline[-1].get("detail") == detail:
        return
    _push_timeline(task, label, detail)


def _map_newton_status(raw: Optional[str]) -> str:
    mapping = {"END": "completed", "KILL": "killed", "WAIT_USER": "needs_review"}
    return mapping.get(raw or "", "in_progress")


def _is_newton_rate_limit(err: str) -> bool:
    return bool(re.search(r"QosAPIFrequencyLimit|frequency limit", err, re.I))


def _is_newton_transient(err: str) -> bool:
    if is_newton_platform_runtime_error(err):
        return False
    return bool(
        re.search(
            r"fetch failed|ECONNRESET|ETIMEDOUT|socket hang up|network|timeout|暂时无响应|服务不可用",
            err,
            re.I,
        )
    )


def _query_failure_timeline(err: str) -> tuple[str, str]:
    if _is_newton_rate_limit(err):
        return "查询过于频繁", "开放平台限流，稍后会自动重试"
    if is_newton_platform_runtime_error(err):
        return "平台执行失败", format_newton_platform_error(err)
    if _is_newton_transient(err):
        return "查询暂不可用", "接口暂时无响应，稍后会自动重试"
    cleaned = re.sub(r"^❌\s*", "", err)
    cleaned = re.sub(r"\[RuntimeExecutor\]\s*", "", cleaned, flags=re.I).strip()
    return "查询进度失败", (cleaned[:120] or "调用失败")


def _is_newton_backed(task: dict[str, Any]) -> bool:
    payload = task.get("payload") or {}
    if payload.get("gateway_sent"):
        return False
    nid = payload.get("newton_task_id") or ""
    if str(nid).startswith("gateway-"):
        return False
    return bool(nid)


def _looks_like_merchant_inquiry(question: str) -> bool:
    return bool(re.search(r"问商家|联系商家|咨询商家|卖家|商家能|能不能定制|MOQ|起订", question, re.I))


def _looks_like_fuzzy_sourcing(question: str) -> bool:
    return bool(re.search(r"寻源|找供应商|找工厂|批量采购|谁能供货", question, re.I))


def _apply_gateway_sent(task: dict[str, Any], markdown: Optional[str] = None) -> None:
    payload = task.setdefault("payload", {})
    payload["gateway_sent"] = True
    payload.pop("error_message", None)
    task["status"] = "completed"
    summary = (markdown or "").replace("❌", "").strip()
    if summary and not is_newton_platform_runtime_error(summary) and len(summary) <= 80:
        task["result_summary"] = summary
    else:
        task["result_summary"] = persistence.ORDER_FOLLOWUP_GATEWAY_SUMMARY
    task["completed_at"] = task.get("completed_at") or _now_iso()
    timeline = task.get("timeline") or []
    if not any(e.get("label") == "已改走网关" for e in timeline):
        _push_timeline(task, "已改走网关", "长程异常，询盘已直发商家")
    task["timeline"] = [e for e in timeline if e.get("label") != "平台终止"]
    task["updated_at"] = _now_iso()


def _retry_gateway(order_id: str, question: str) -> bool:
    outcome = send_order_inquiry(order_id, question)
    return bool(outcome.get("success"))


def _parse_iso_ms(iso: str) -> Optional[int]:
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def _refresh_time_based_statuses(tasks: list[dict[str, Any]]) -> None:
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    for task in tasks:
        if task.get("status") in TERMINAL_STATUSES:
            continue
        qa = (task.get("payload") or {}).get("query_available_at")
        if not qa or task.get("status") != "in_progress":
            continue
        available_ms = _parse_iso_ms(str(qa))
        if available_ms is None or now_ms < available_ms:
            continue
        if task.get("type") == "inquiry_1688":
            task["status"] = "ready"
            _push_timeline_if_new(task, "可查询结果", "无需记住任务编号，在任务中心一键查询")
        elif task.get("type") == "supplychain_inquiry":
            task["status"] = "ready"
            _push_timeline_if_new(task, "可查询结果", "可查询商家回复")


def _load_tasks() -> list[dict[str, Any]]:
    with _store_lock:
        return persistence.load_and_repair()


def _save_tasks(tasks: list[dict[str, Any]]) -> None:
    with _store_lock:
        persistence.save_runtime_tasks(tasks)


def _refresh_and_persist(runtime: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """时间态刷新；仅状态真变时写回，降低与 append 的竞态窗口。"""
    before = {
        str(t.get("id")): (t.get("status"), t.get("updated_at"), len(t.get("timeline") or []))
        for t in runtime
    }
    _refresh_time_based_statuses(runtime)
    dirty = any(
        before.get(str(t.get("id")))
        != (t.get("status"), t.get("updated_at"), len(t.get("timeline") or []))
        for t in runtime
    )
    if dirty:
        _save_tasks(runtime)
    return runtime


def list_tasks(
    task_type: Optional[str] = None,
    status: Optional[str] = None,
) -> list[dict[str, Any]]:
    runtime = _refresh_and_persist(_load_tasks())
    tasks = merge_tasks_with_seeds(runtime)
    out = tasks
    if task_type:
        out = [t for t in out if t.get("type") == task_type]
    if status:
        out = [t for t in out if t.get("status") == status]
    return out


def get_task(task_id: str) -> Optional[dict[str, Any]]:
    runtime = _refresh_and_persist(_load_tasks())
    tasks = merge_tasks_with_seeds(runtime)
    return next((t for t in tasks if t.get("id") == task_id), None)


def get_task_stats() -> dict[str, int]:
    tasks = list_tasks()
    return {
        "total": len(tasks),
        "in_progress": sum(1 for t in tasks if t.get("status") == "in_progress"),
        "ready": sum(1 for t in tasks if t.get("status") == "ready"),
        "needs_review": sum(1 for t in tasks if t.get("status") == "needs_review"),
        "completed": sum(1 for t in tasks if t.get("status") == "completed"),
        "failed": sum(1 for t in tasks if t.get("status") == "failed"),
        "killed": sum(1 for t in tasks if t.get("status") == "killed"),
    }


def get_agent_operation_stats() -> dict[str, Any]:
    tasks = list_tasks()
    by_type = []
    for task_type in OPERATION_TASK_TYPES:
        typed = [t for t in tasks if t.get("type") == task_type]
        by_type.append(
            {
                "type": task_type,
                "total": len(typed),
                "active": sum(1 for t in typed if t.get("status") in ACTIVE_STATUSES),
                "completed": sum(1 for t in typed if t.get("status") == "completed"),
                "failed": sum(
                    1 for t in typed if t.get("status") in ("failed", "killed")
                ),
            }
        )
    by_status = {
        "in_progress": sum(1 for t in tasks if t.get("status") == "in_progress"),
        "ready": sum(1 for t in tasks if t.get("status") == "ready"),
        "needs_review": sum(1 for t in tasks if t.get("status") == "needs_review"),
        "completed": sum(1 for t in tasks if t.get("status") == "completed"),
        "failed": sum(1 for t in tasks if t.get("status") == "failed"),
        "killed": sum(1 for t in tasks if t.get("status") == "killed"),
    }
    return {
        "total": len(tasks),
        "active": sum(1 for t in tasks if t.get("status") in ACTIVE_STATUSES),
        "completed": by_status["completed"],
        "failed": by_status["failed"] + by_status["killed"],
        "by_type": by_type,
        "by_status": by_status,
    }


def refresh_newton_task(task_id: str, *, force: bool = False) -> Optional[dict[str, Any]]:
    lock = _refresh_locks.setdefault(task_id, threading.Lock())
    if not lock.acquire(blocking=False):
        return get_task(task_id)
    try:
        return _refresh_newton_task_inner(task_id, force=force)
    finally:
        lock.release()


def _refresh_newton_task_inner(task_id: str, *, force: bool = False) -> Optional[dict[str, Any]]:
    tasks = _load_tasks()
    task = next((t for t in tasks if t.get("id") == task_id), None)
    if not task or not _is_newton_backed(task):
        return None
    if task.get("status") == "killed":
        return task

    payload = task.setdefault("payload", {})
    newton_id = payload.get("newton_task_id", "")
    if str(newton_id).startswith("demo-newton-"):
        return task

    entry = _newton_get_cache.setdefault(newton_id, {"last_at": 0, "backoff_until": 0})
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    created_ms = int(datetime.fromisoformat(task["created_at"].replace("Z", "+00:00")).timestamp() * 1000)
    task_age_ms = now_ms - created_ms

    if not force:
        if task_age_ms < NEWTON_GET_GRACE_MS:
            return task
        if now_ms < entry["backoff_until"]:
            return task
        if now_ms - entry["last_at"] < NEWTON_GET_MIN_MS:
            return task
    elif now_ms < entry["backoff_until"]:
        return task

    if force and task_age_ms < NEWTON_GET_GRACE_MS:
        _push_timeline_if_new(task, "任务处理中", "刚提交不久，请几分钟后再查")
        entry["backoff_until"] = now_ms + min(NEWTON_GET_GRACE_MS - task_age_ms, NEWTON_GET_BACKOFF_MS)
        _save_tasks(tasks)
        return task

    outcome = task_get(newton_id)
    entry["last_at"] = now_ms
    r = outcome.result or {}

    if not outcome.ok or not r:
        err = outcome.error or r.get("error") or "调用失败"
        label, detail = _query_failure_timeline(err)
        if _is_newton_rate_limit(err) or _is_newton_transient(err):
            entry["backoff_until"] = now_ms + NEWTON_GET_BACKOFF_MS
        _push_timeline_if_new(task, label, detail)
        _save_tasks(tasks)
        return task

    entry["backoff_until"] = 0
    prev_status = task.get("status")
    payload["newton_status"] = r.get("status")
    if r.get("taskType"):
        payload["task_type"] = r.get("taskType")
    if r.get("content"):
        payload["content"] = r.get("content")
    if r.get("messages"):
        payload["messages"] = r.get("messages")
    if r.get("errorMessage"):
        em = r["errorMessage"]
        payload["error_message"] = (
            format_newton_platform_error(em) if is_newton_platform_runtime_error(em) else em
        )

    is_order = task.get("type") == "order_followup"
    question = payload.get("question") or task.get("title", "")

    if is_order:
        combined = join_newton_text(r.get("messages"), r.get("content"))
        merchant_reply = pick_order_followup_reply(r.get("messages"), r.get("content"), question)
        status = r.get("status")
        err_msg = r.get("errorMessage")
        if status == "KILL" or is_newton_platform_runtime_error(err_msg):
            if not payload.get("gateway_sent"):
                if _retry_gateway(payload.get("order_id", ""), question):
                    _apply_gateway_sent(
                        task,
                        f"长程任务异常，已改走网关直发商家（{payload.get('order_id')}）。回复请在旺旺或订单页查看。",
                    )
                    _save_tasks(tasks)
                    return task
            else:
                _apply_gateway_sent(task)
                _save_tasks(tasks)
                return task
            task["status"] = "killed"
            task["result_summary"] = format_newton_platform_error(err_msg or r.get("content") or task.get("result_summary"))
            task["completed_at"] = task.get("completed_at") or _now_iso()
            _push_timeline(task, "平台终止", task["result_summary"][:120])
        elif status == "WAIT_USER":
            task["status"] = "needs_review"
            task["result_summary"] = (r.get("content") or "待补充信息")[:200]
            if prev_status != "needs_review":
                _push_timeline(task, "待补充信息", task["result_summary"][:120])
        elif merchant_reply:
            task["status"] = "completed"
            task["result_summary"] = merchant_reply[:500]
            task["completed_at"] = task.get("completed_at") or _now_iso()
            if not any(e.get("label") == "商家已回复" for e in task.get("timeline", [])):
                _push_timeline(task, "商家已回复", task["result_summary"][:120])
        elif combined and is_order_followup_ack(combined, question):
            task["status"] = "in_progress"
            task["result_summary"] = "询盘已发出，等待商家回复"
            task.pop("completed_at", None)
            task["timeline"] = [
                e for e in (task.get("timeline") or [])
                if e.get("label") != "商家已回复"
            ]
            _push_timeline_if_new(task, "询盘已发出", "平台受理回执，非商家原话")
        else:
            task["status"] = "in_progress"
            task["result_summary"] = "已提交，等待商家回复"
            task.pop("completed_at", None)
    else:
        ask_seller = payload.get("ask_seller") is True
        if not ask_seller and _looks_like_merchant_inquiry(question):
            payload["ask_seller"] = True
            ask_seller = True
            task["skill_name"] = "问商家"

        if ask_seller:
            seller_reply = pick_ask_seller_consult_reply(r.get("messages"), r.get("content"), question)
            status = r.get("status")
            err_msg = r.get("errorMessage")
            if status == "KILL" or is_newton_platform_runtime_error(err_msg):
                task["status"] = "killed"
                task["result_summary"] = format_newton_platform_error(err_msg or r.get("content") or task.get("result_summary"))
                task["completed_at"] = task.get("completed_at") or _now_iso()
                _push_timeline(task, "平台终止", task["result_summary"][:120])
            elif status == "WAIT_USER":
                task["status"] = "needs_review"
                task["result_summary"] = (r.get("content") or "待补充信息")[:200]
                if prev_status != "needs_review":
                    _push_timeline(task, "待补充信息", task["result_summary"][:120])
            elif seller_reply:
                task["status"] = "completed"
                task["result_summary"] = seller_reply[:500]
                task["completed_at"] = task.get("completed_at") or _now_iso()
                if not any(e.get("label") == "商家已回复" for e in task.get("timeline", [])):
                    _push_timeline(task, "商家已回复", task["result_summary"][:120])
            else:
                task["status"] = "in_progress"
                task["result_summary"] = "已提交，等待商家回复"
                task.pop("completed_at", None)
        else:
            answer = join_newton_text(r.get("messages"), r.get("content"))
            task["status"] = _map_newton_status(r.get("status"))
            if is_terminal_status(r.get("status")):
                task["result_summary"] = answer or r.get("errorMessage") or (
                    "任务已终止" if r.get("status") == "KILL" else "已完成"
                )
                task["completed_at"] = task.get("completed_at") or _now_iso()
            else:
                task["result_summary"] = (r.get("content") or "处理中…")[:200]

    task["updated_at"] = _now_iso()
    _save_tasks(tasks)
    return task


def refresh_task_by_id(task_id: str, *, force: bool = False) -> Optional[dict[str, Any]]:
    task = get_task(task_id)
    if not task:
        return None
    if _is_newton_backed(task):
        return refresh_newton_task(task_id, force=force)
    return task


def kill_task_by_id(task_id: str, reason: str, operator: Optional[str] = None) -> Optional[dict[str, Any]]:
    tasks = _load_tasks()
    task = next((t for t in tasks if t.get("id") == task_id), None)
    if not task:
        return None
    if task.get("status") in TERMINAL_STATUSES:
        return task
    if task.get("status") not in ("in_progress", "needs_review"):
        return None

    payload = task.get("payload") or {}
    newton_id = payload.get("newton_task_id")
    if newton_id and not str(newton_id).startswith("demo-newton-"):
        task_kill(str(newton_id), reason or "用户主动终止")

    task["status"] = "killed"
    task["completed_at"] = _now_iso()
    task["result_summary"] = reason or "用户主动终止"
    if "newton_status" in payload:
        payload["newton_status"] = "KILL"
    _push_timeline(task, "人工终止", reason or "用户主动终止")
    task["updated_at"] = _now_iso()
    _save_tasks(tasks)
    return task


def refresh_all_active_newton_tasks() -> list[dict[str, Any]]:
    tasks = _load_tasks()
    active = [
        t
        for t in tasks
        if _is_newton_backed(t)
        and t.get("status") in ("in_progress", "needs_review")
        and (t.get("payload") or {}).get("newton_task_id")
        and not str((t.get("payload") or {}).get("newton_task_id")).startswith("demo-newton-")
    ]
    updated: list[dict[str, Any]] = []
    for t in active:
        u = refresh_newton_task(t["id"])
        if u:
            updated.append(u)
    return updated

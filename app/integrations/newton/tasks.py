"""牛顿云长程任务 API。"""

from __future__ import annotations

from typing import Any, Optional

from app.integrations.alibaba_open.client import CallOutcome, get_newton_api_key, open_api_call

NAMESPACE = "com.alibaba.agent"
VERSION = "1"
NEWTON_ORDER_INQUIRY_SKILL = "1688-supplychain-order-inquiry"


def _agent_call(name: str, params: dict[str, Any]) -> CallOutcome:
    outcome = open_api_call(NAMESPACE, name, params, version=VERSION)
    if not outcome.ok or not outcome.result:
        return outcome
    # newtoncloud.task.get 在 KILL 时 result 可能在顶层
    result = outcome.result
    if "result" in result and isinstance(result["result"], dict):
        inner = result["result"]
        if inner:
            return CallOutcome(ok=True, result=inner)
    return outcome


def task_create(
    message: str,
    *,
    session_id: Optional[str] = None,
    task_id: Optional[str] = None,
    skill_code: Optional[str] = None,
    api_key: Optional[str] = None,
) -> CallOutcome:
    params: dict[str, Any] = {"message": message}
    if session_id:
        params["sessionId"] = session_id
    if task_id:
        params["taskId"] = task_id
    if skill_code and skill_code.strip():
        params["skillCode"] = skill_code.strip()
    key = (api_key or get_newton_api_key() or "").strip()
    if key:
        params["apiKey"] = key
    return _agent_call("newtoncloud.task.create", params)


def task_get(task_id: str) -> CallOutcome:
    return _agent_call("newtoncloud.task.get", {"taskId": task_id})


def task_kill(task_id: str, reason: str) -> CallOutcome:
    return _agent_call("newtoncloud.task.kill", {"taskId": task_id, "reason": reason})


def is_terminal_status(status: Optional[str]) -> bool:
    return status in ("END", "KILL")

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.deps import require_permission
from app.services.tasks import store
from app.services.tasks.supplychain import create_supplychain_inquiry_task

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


class KillBody(BaseModel):
    reason: Optional[str] = None


class SupplychainInquiryBody(BaseModel):
    requirement: str
    questions: list[dict[str, str]] = Field(default_factory=list)
    purchase_size: int = 1
    inquiry_item_size: int = 3
    recall_item_size: int = 10
    image_urls: Optional[list[str]] = None


@router.get("")
def list_tasks_endpoint(
    request: Request,
    type: Optional[str] = None,
    status: Optional[str] = None,
) -> dict[str, Any]:
    tasks = store.list_tasks(task_type=type, status=status)
    return {"tasks": tasks, "stats": store.get_task_stats()}


@router.post("/refresh-active")
def refresh_active() -> dict[str, int]:
    updated = store.refresh_all_active_newton_tasks()
    return {"count": len(updated)}


@router.post("/{task_id}/refresh")
def refresh_task(task_id: str, request: Request, force: Optional[str] = None) -> dict[str, Any]:
    task = store.refresh_task_by_id(task_id, force=force == "1")
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在或不可刷新")
    return {"task": task}


@router.post("/refresh-ready")
def refresh_ready() -> dict[str, Any]:
    """刷新可查询的询盘/供应链任务（后续接 inquiry 查询实现）。"""
    return {"tasks": [], "count": 0}


@router.post("/supplychain-inquiry")
def supplychain_inquiry(body: SupplychainInquiryBody) -> dict[str, Any]:
    outcome = create_supplychain_inquiry_task(
        body.requirement,
        body.questions,
        purchase_size=body.purchase_size,
        inquiry_item_size=body.inquiry_item_size,
        recall_item_size=body.recall_item_size,
        image_urls=body.image_urls,
    )
    if not outcome.get("task"):
        raise HTTPException(status_code=400, detail=outcome.get("error") or "发起失败")
    return {"task": outcome["task"], "via": outcome.get("via")}


@router.post("/{task_id}/kill")
def kill_task(task_id: str, request: Request, body: KillBody) -> dict[str, Any]:
    user = require_permission(request, "task.control", "edit")
    task = store.kill_task_by_id(task_id, body.reason or "用户主动终止", user.account)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在或不可终止")
    return {"task": task}

"""HTTP 路由 — AI 自进化引擎。"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.api.deps import require_auth
from app.services.evolution.engine import (
    capture_feedback,
    trigger_analysis,
    approve_patch,
    deploy_patch,
    rollback_patch,
    discard_patch,
    get_overview,
)
from app.services.evolution.store import (
    get_feedback_records,
    get_patches,
    get_reports,
    get_report_by_id,
)
from app.services.evolution.skill_registry import get_all_evolution_skills  # 【问题1修复】技能列表由 Python 后端提供
from app.services.evolution.replay import replay_skill  # 被动复盘：推理回溯 + 诊断

router = APIRouter(prefix="/api/evolution", tags=["evolution"])


# ─── Pydantic 模型 ───


class FeedbackBody(BaseModel):
    skill_id: str
    domain: str
    source: str
    sentiment: str
    ai_output_preview: str
    human_decision_preview: Optional[str] = None
    correction_value: Optional[str] = None
    context_ref: Optional[str] = None
    error_category: Optional[str] = None
    feedback_intent: Optional[str] = "neutral"       # 【问题5修复】correction | enrichment | confirmation | neutral
    is_priority_badcase: Optional[bool] = False       # 【问题4修复】高优先级 badcase 单条可触发分析


class AnalysisBody(BaseModel):
    min_feedback_count: int = 10
    skill_id: Optional[str] = None  # 【问题4修复】支持只分析某个技能


class PatchApproveBody(BaseModel):
    patch_id: str
    approved_by: Optional[str] = None


class PatchActionBody(BaseModel):
    patch_id: str


class ReplayBody(BaseModel):
    """被动复盘请求体。"""
    skill_id: str
    title: str
    ai_suggestion: str
    human_correction: str
    correction_value: Optional[str] = None
    context_ref: Optional[str] = None
    reviewer_note: Optional[str] = None         # 用户纠正备注（核心教材信号）


# ─── 总览 ───


@router.get("/overview")
def evolution_overview(request: Request) -> dict[str, Any]:
    require_auth(request)
    return get_overview()


@router.get("/skills")
def list_skills(request: Request) -> dict[str, Any]:
    """【问题1修复】Python 为唯一 Registry 源。前端从此端点拉技能列表，不再维护 TS 版本。"""
    require_auth(request)
    return {"skills": get_all_evolution_skills()}


# ─── 反馈 ───


@router.post("/feedback")
def submit_feedback(request: Request, body: FeedbackBody) -> dict[str, Any]:
    require_auth(request)
    item = body.model_dump()
    item_id = capture_feedback(item)
    return {"ok": True, "id": item_id}


@router.get("/feedback")
def list_feedback(
    request: Request,
    skill_id: Optional[str] = None,
    domain: Optional[str] = None,
    sentiment: Optional[str] = None,
    analyzed: Optional[bool] = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    require_auth(request)
    records = get_feedback_records(
        skill_id=skill_id,
        domain=domain,
        sentiment=sentiment,
        analyzed=analyzed,
        limit=limit,
        offset=offset,
    )
    return {"records": records, "count": len(records)}


# ─── 被动复盘 ───


@router.post("/replay")
def run_replay(request: Request, body: ReplayBody) -> dict[str, Any]:
    """被动复盘：用户覆盖 AI 建议后，回溯推理路径并生成诊断摘要。

    轻量复盘（纯 Python），< 100ms。
    """
    require_auth(request)
    result = replay_skill(
        skill_id=body.skill_id,
        title=body.title,
        ai_suggestion=body.ai_suggestion,
        human_correction=body.human_correction,
        correction_value=body.correction_value,
        context_ref=body.context_ref,
        reviewer_note=body.reviewer_note,
    )
    return {"ok": True, "result": result.to_public()}


# ─── 分析 ───


@router.post("/analyze")
def run_analysis(request: Request, body: AnalysisBody) -> dict[str, Any]:
    require_auth(request)
    report = trigger_analysis(body.min_feedback_count, skill_id=body.skill_id)
    if not report:
        return {"ok": False, "reason": "feedback_insufficient", "message": "反馈不足，暂不触发分析"}
    return {"ok": True, "report": report}


@router.get("/reports")
def list_reports(request: Request, limit: int = 20) -> dict[str, Any]:
    require_auth(request)
    return {"reports": get_reports(limit=limit)}


@router.get("/reports/{report_id}")
def get_report(request: Request, report_id: str) -> dict[str, Any]:
    require_auth(request)
    report = get_report_by_id(report_id)
    if not report:
        raise HTTPException(status_code=404, detail="分析报告不存在")
    return report


# ─── 补丁 ───


@router.get("/patches")
def list_patches(
    request: Request,
    skill_id: Optional[str] = None,
    status: Optional[str] = None,
    patch_type: Optional[str] = None,
    active_only: bool = False,
    limit: int = 50,
) -> dict[str, Any]:
    require_auth(request)
    patches = get_patches(
        skill_id=skill_id,
        status=status,
        patch_type=patch_type,
        active_only=active_only,
        limit=limit,
    )
    return {"patches": patches, "count": len(patches)}


@router.post("/patches/approve")
def approve_patch_action(request: Request, body: PatchApproveBody) -> dict[str, Any]:
    user = require_auth(request)
    result = approve_patch(body.patch_id, approved_by=user.account)
    if not result:
        raise HTTPException(status_code=404, detail="补丁不存在")
    return {"ok": True, "patch": result}


@router.post("/patches/deploy")
def deploy_patch_action(request: Request, body: PatchActionBody) -> dict[str, Any]:
    require_auth(request)
    result = deploy_patch(body.patch_id)
    if not result:
        raise HTTPException(status_code=404, detail="补丁不存在")
    return {"ok": True, "patch": result}


@router.post("/patches/rollback")
def rollback_patch_action(request: Request, body: PatchActionBody) -> dict[str, Any]:
    require_auth(request)
    result = rollback_patch(body.patch_id)
    if not result:
        raise HTTPException(status_code=404, detail="补丁不存在")
    return {"ok": True, "patch": result}


@router.post("/patches/discard")
def discard_patch_action(request: Request, body: PatchActionBody) -> dict[str, Any]:
    require_auth(request)
    result = discard_patch(body.patch_id)
    if not result:
        raise HTTPException(status_code=404, detail="补丁不存在")
    return {"ok": True, "patch": result}

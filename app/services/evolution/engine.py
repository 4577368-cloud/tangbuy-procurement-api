"""AI 自进化引擎 · 进化周期编排（一站式 API）。"""

from __future__ import annotations

from typing import Any, Optional

from app.services.evolution.analyzer import run_badcase_analysis
from app.services.evolution.auto_deploy import advance_gray_percent
from app.services.evolution.eval.metrics import list_deploy_metrics, should_rollback_patch
from app.services.evolution.eval.shadow import run_shadow_eval
from app.services.evolution.patch_generator import generate_patches_from_report
from app.services.evolution.store import (
    append_feedback,
    get_evolution_overview,
    get_feedback_records,
    get_patch_by_id,
    get_patches,
    get_reports,
    update_patch_gray,
    update_patch_status,
    update_patch_content,
)
from app.services.evolution.skill_registry import get_all_evolution_skills, get_domain_summary


def capture_feedback(item: dict[str, Any]) -> str:
    """捕获一条反馈记录。"""
    from app.services.evolution.eval.deploy_tracking import track_deploy_feedback

    item_id = append_feedback(item)
    track_deploy_feedback(item)
    return item_id


def trigger_analysis(min_feedback_count: int = 10, skill_id: Optional[str] = None) -> Optional[dict[str, Any]]:
    """触发 Badcase 分析。返回分析报告或 None（反馈不足）。"""
    report = run_badcase_analysis(min_feedback_count, skill_id=skill_id)
    if report:
        patches = generate_patches_from_report(report)
        report["generated_patch_count"] = len(patches)
    return report


def approve_patch(patch_id: str, approved_by: Optional[str] = None) -> Optional[dict[str, Any]]:
    """审批补丁（draft/pending → approved）。"""
    return update_patch_status(patch_id, "approved", approved_by=approved_by)


def start_shadow_eval(patch_id: str) -> Optional[dict[str, Any]]:
    """Shadow 试运行：历史纠正样本对比。"""
    return run_shadow_eval(patch_id)


def deploy_patch(patch_id: str, *, force: bool = False) -> Optional[dict[str, Any]]:
    """灰度部署（approved → deployed @5%）。需 shadow eval 通过，除非 force。"""
    patch = get_patch_by_id(patch_id)
    if not patch:
        return None
    if patch.get("status") != "approved":
        return None
    eval_result = patch.get("eval_result") if isinstance(patch.get("eval_result"), dict) else {}
    if not force and not eval_result.get("passed"):
        return None
    return update_patch_status(patch_id, "deployed")


def advance_patch_gray(patch_id: str) -> Optional[dict[str, Any]]:
    """推进灰度：5% → 20% → 50% → 100%。"""
    patch = get_patch_by_id(patch_id)
    if not patch or patch.get("status") != "deployed":
        return None
    cur = int(patch.get("gray_percent") or 0)
    nxt = advance_gray_percent(cur)
    return update_patch_gray(patch_id, nxt)


def rollback_patch(patch_id: str) -> Optional[dict[str, Any]]:
    """回滚补丁（deployed → rolled_back）。"""
    return update_patch_status(patch_id, "rolled_back")


def discard_patch(patch_id: str) -> Optional[dict[str, Any]]:
    """废弃补丁。"""
    return update_patch_status(patch_id, "discarded")


def revise_patch(
    patch_id: str,
    content: str,
    *,
    payload: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """修改补丁正文。"""
    return update_patch_content(patch_id, content, payload=payload)


def get_metrics(*, patch_id: Optional[str] = None) -> dict[str, Any]:
    items = list_deploy_metrics(patch_id=patch_id)
    rollback_suggested = bool(patch_id and should_rollback_patch(patch_id))
    return {"items": items, "rollback_suggested": rollback_suggested}


def get_overview() -> dict[str, Any]:
    """进化引擎总览。"""
    base = get_evolution_overview()
    base["skills"] = get_all_evolution_skills()
    base["domain_summary"] = get_domain_summary()
    return base

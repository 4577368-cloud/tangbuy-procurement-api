"""AI 自进化引擎 · 进化周期编排（一站式 API）。"""

from __future__ import annotations

from typing import Any, Optional

from app.services.evolution.analyzer import run_badcase_analysis
from app.services.evolution.patch_generator import generate_patches_from_report
from app.services.evolution.store import (
    append_feedback,
    get_evolution_overview,
    get_feedback_records,
    get_patches,
    get_reports,
    update_patch_status,
    update_patch_content,
)
from app.services.evolution.skill_registry import get_all_evolution_skills, get_domain_summary


def capture_feedback(item: dict[str, Any]) -> str:
    """捕获一条反馈记录。"""
    return append_feedback(item)


def trigger_analysis(min_feedback_count: int = 10, skill_id: Optional[str] = None) -> Optional[dict[str, Any]]:
    """触发 Badcase 分析。返回分析报告或 None（反馈不足）。
    
    【问题4修复】支持指定 skill_id 只分析某个技能。
    """
    report = run_badcase_analysis(min_feedback_count, skill_id=skill_id)
    if report:
        patches = generate_patches_from_report(report)
        report["generated_patch_count"] = len(patches)
    return report


def approve_patch(patch_id: str, approved_by: Optional[str] = None) -> Optional[dict[str, Any]]:
    """审批补丁（draft/pending → approved）。"""
    return update_patch_status(patch_id, "approved", approved_by=approved_by)


def deploy_patch(patch_id: str) -> Optional[dict[str, Any]]:
    """部署补丁（approved → deployed）。"""
    return update_patch_status(patch_id, "deployed")


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


def get_overview() -> dict[str, Any]:
    """进化引擎总览。"""
    base = get_evolution_overview()
    base["skills"] = get_all_evolution_skills()
    base["domain_summary"] = get_domain_summary()
    return base

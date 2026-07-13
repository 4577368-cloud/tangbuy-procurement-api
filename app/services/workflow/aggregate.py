"""WorkflowRun 聚合：步骤 trace + Skill 审计。"""

from __future__ import annotations

from typing import Any, Optional

from app.services.evolution.skill_registry import get_evolution_skill


def _group_invocations_by_stage(invocations: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for inv in invocations:
        stage = str(inv.get("workflow_stage") or "unscoped")
        grouped.setdefault(stage, []).append(inv)
    return grouped


def _summarize_invocations(invocations: list[dict[str, Any]]) -> dict[str, Any]:
    pending = sum(1 for i in invocations if i.get("audit_status") == "pending")
    badcase = sum(1 for i in invocations if i.get("audit_status") == "badcase")
    return {
        "total": len(invocations),
        "pending_audit": pending,
        "badcase": badcase,
    }


def enrich_workflow_run(
    run: dict[str, Any],
    *,
    include_invocations: bool = True,
    invocation_limit: int = 50,
) -> dict[str, Any]:
    """为 WorkflowRun 附加 Skill 审计与按步骤分组。"""
    ord_line = str(run.get("ord_line_no") or "").strip()
    out: dict[str, Any] = {**run}
    if not ord_line:
        return out

    invocations: list[dict[str, Any]] = []
    if include_invocations:
        try:
            from app.services.skill_audit.store import list_invocations_for_ord_line

            invocations = list_invocations_for_ord_line(ord_line, limit=invocation_limit)
        except Exception:
            invocations = []

    by_stage = _group_invocations_by_stage(invocations)
    out["invocation_summary"] = _summarize_invocations(invocations)
    if include_invocations:
        out["invocations"] = invocations
        out["invocations_by_stage"] = by_stage

    # 步骤历史 enriched：挂上该步骤的 skill 名
    history = []
    for step in run.get("step_history") or []:
        if not isinstance(step, dict):
            continue
        stage = str(step.get("step") or "")
        stage_invs = by_stage.get(stage) or []
        skill_names: list[str] = []
        for inv in stage_invs:
            sid = str(inv.get("skill_id") or "")
            desc = get_evolution_skill(sid) if sid else None
            name = desc.skill_name if desc else sid
            if name and name not in skill_names:
                skill_names.append(name)
        row = {**step}
        if skill_names:
            row["linked_skills"] = skill_names
        if stage_invs:
            row["linked_invocation_ids"] = [str(i.get("id")) for i in stage_invs if i.get("id")]
        history.append(row)
    out["step_history_enriched"] = history
    return out

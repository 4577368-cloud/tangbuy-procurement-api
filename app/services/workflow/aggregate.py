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


def _step_display_summary(step: dict[str, Any]) -> str:
    ev = step.get("evidence") if isinstance(step.get("evidence"), dict) else {}
    summary = str(ev.get("summary") or "").strip()
    if summary:
        return summary
    blockers = step.get("blockers") if isinstance(step.get("blockers"), list) else []
    if blockers:
        parts: list[str] = []
        for b in blockers[:4]:
            if not isinstance(b, dict):
                continue
            label = str(b.get("label") or b.get("key") or "").strip()
            detail = str(b.get("detail") or "").strip()
            parts.append(f"{label} · {detail}" if label and detail else label or detail)
        return "；".join(p for p in parts if p)
    summaries = ev.get("blocker_summaries")
    if isinstance(summaries, list) and summaries:
        return "；".join(str(s) for s in summaries[:4])
    if step.get("step") == "category_map":
        name = ev.get("category_cn_name")
        if name:
            return f"品类 → {name}"
    if step.get("step") == "admin_writeback":
        st = ev.get("status")
        if st:
            return f"Admin回写 {st}"
    if step.get("step") == "release_gate":
        failed = ev.get("failed_conditions") if isinstance(ev.get("failed_conditions"), list) else []
        if failed:
            return "；".join(
                str(c.get("label") or c.get("key") or "")
                for c in failed[:3]
                if isinstance(c, dict)
            )
    pipeline_label = ev.get("pipeline_step_label") or ev.get("pipeline_step")
    if pipeline_label:
        return str(pipeline_label)
    return ""


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
    run_blockers = run.get("blockers") if isinstance(run.get("blockers"), list) else []
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
        if (
            row.get("status") == "blocked"
            and not row.get("blockers")
            and run_blockers
            and stage == str(run.get("current_step") or "")
        ):
            row["blockers"] = run_blockers
        display = _step_display_summary(row)
        if display:
            row["display_summary"] = display
        if skill_names:
            row["linked_skills"] = skill_names
        if stage_invs:
            row["linked_invocation_ids"] = [str(i.get("id")) for i in stage_invs if i.get("id")]
        history.append(row)
    out["step_history_enriched"] = history
    return out

"""领域钩子：在现有流程节点写入 WorkflowRun trace。"""

from __future__ import annotations

from typing import Any, Optional

from app.services.workflow.engine import record_workflow_step


def trace_category_map(
    ord_line_nos: list[str],
    *,
    product_id: str,
    hs: dict[str, Any],
    manual: bool = False,
    resolution: Optional[str] = None,
) -> None:
    actor = "user" if manual else "agent"
    cid = hs.get("category_id")
    name = hs.get("category_cn_name") or hs.get("declare_cn_name")
    for raw in ord_line_nos:
        key = str(raw or "").strip()
        if not key:
            continue
        record_workflow_step(
            key,
            "category_map",
            status="ok",
            actor=actor,  # type: ignore[arg-type]
            evidence={
                "category_id": cid,
                "category_cn_name": name,
                "resolution": resolution or ("manual_correct" if manual else "auto"),
            },
            linked_refs={"product_id": product_id},
            item_id=product_id,
        )


def trace_admin_writeback(
    ord_line_nos: list[str],
    *,
    product_id: str,
    wb: dict[str, Any],
) -> None:
    st = str(wb.get("status") or "")
    if st == "ok":
        step_status = "ok"
    elif st == "skipped":
        step_status = "skipped"
    elif st == "failed":
        step_status = "failed"
    elif st == "writing":
        step_status = "running"
    else:
        step_status = "skipped"
    actor: str = "user" if wb.get("resolution") in ("manual_confirm", "manual_correct") else "system"
    for raw in ord_line_nos:
        key = str(raw or "").strip()
        if not key:
            continue
        record_workflow_step(
            key,
            "admin_writeback",
            status=step_status,  # type: ignore[arg-type]
            actor=actor,  # type: ignore[arg-type]
            evidence={
                "status": st,
                "cid": wb.get("cid") or wb.get("to_cid"),
                "to_category": wb.get("to_category"),
                "error": wb.get("error"),
                "skip_reason": wb.get("skip_reason"),
            },
            linked_refs={"product_id": product_id},
            item_id=product_id,
        )


def trace_release_gate(
    ord_line_no: str,
    evaluation: dict[str, Any],
    *,
    result: str,
    trigger: str = "manual",
) -> None:
    key = ord_line_no.strip()
    if not key:
        return
    eligible = bool(evaluation.get("eligible"))
    all_passed = bool(evaluation.get("all_passed"))
    if result in ("submitted", "already_submitted"):
        step_status = "ok"
    elif result in ("needs_review", "flagged"):
        step_status = "blocked"
    elif result == "rejected":
        step_status = "failed"
    elif all_passed and eligible:
        step_status = "ok"
    else:
        step_status = "blocked"
    failed = [c for c in (evaluation.get("conditions") or []) if not c.get("passed")]
    record_workflow_step(
        key,
        "release_gate",
        status=step_status,  # type: ignore[arg-type]
        actor="rule",
        evidence={
            "result": result,
            "trigger": trigger,
            "eligible": eligible,
            "all_passed": all_passed,
            "failed_conditions": failed[:8],
        },
        blockers=[{"key": c.get("key"), "label": c.get("label"), "detail": c.get("detail")} for c in failed[:8]]
        if failed
        else None,
        ord_no=str(evaluation.get("ord_no") or "") or None,
    )


def trace_pipeline_advance(
    ord_line_no: str,
    *,
    pipeline_step: str,
    ord_line_stat: Optional[int] = None,
    blockers: Optional[list[dict[str, Any]]] = None,
    operator: Optional[str] = None,
) -> None:
    key = ord_line_no.strip()
    if not key:
        return
    has_blockers = bool(blockers)
    if pipeline_step == "done":
        step_status = "ok"
    elif pipeline_step == "blocked" or has_blockers:
        step_status = "blocked"
    else:
        step_status = "running"
    record_workflow_step(
        key,
        "pipeline_advance",
        status=step_status,  # type: ignore[arg-type]
        actor="user" if operator and operator != "system" else "system",
        evidence={
            "pipeline_step": pipeline_step,
            "ord_line_stat": ord_line_stat,
            "blocker_count": len(blockers or []),
        },
        blockers=blockers,
    )

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
        resolution_val = resolution or ("manual_correct" if manual else "auto")
        record_workflow_step(
            key,
            "category_map",
            status="ok",
            actor=actor,  # type: ignore[arg-type]
            evidence={
                "category_id": cid,
                "category_cn_name": name,
                "resolution": resolution_val,
                "summary": f"品类 → {name or '—'}（{resolution_val}）",
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
        wb_summary = f"Admin回写 {st}"
        if wb.get("to_category"):
            wb_summary += f" → {wb.get('to_category')}"
        if st == "failed" and wb.get("error"):
            wb_summary += f" · {wb.get('error')}"
        elif st == "skipped" and wb.get("skip_reason"):
            wb_summary += f" · {wb.get('skip_reason')}"
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
                "summary": wb_summary,
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
    failed_labels = [
        f"{c.get('label') or c.get('key')}"
        + (f" · {c.get('detail')}" if c.get("detail") else "")
        for c in failed[:6]
    ]
    summary = (
        f"放行未通过：{'；'.join(failed_labels)}"
        if failed_labels
        else (f"放行结果 {result}" if result else "放行评估")
    )
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
            "summary": summary,
        },
        blockers=[{"key": c.get("key"), "label": c.get("label"), "detail": c.get("detail")} for c in failed[:8]]
        if failed
        else None,
        ord_no=str(evaluation.get("ord_no") or "") or None,
    )


_PIPELINE_STEP_LABELS: dict[str, str] = {
    "accept": "接单",
    "prepare": "备货处理",
    "pre_purchase": "1688预订购",
    "place_order": "1688下单",
    "payment": "待支付",
    "followup": "已订购跟进",
    "done": "完成",
    "blocked": "卡点",
}


def _format_blocker_summaries(blockers: Optional[list[dict[str, Any]]]) -> list[str]:
    out: list[str] = []
    for b in blockers or []:
        if not isinstance(b, dict):
            continue
        label = str(b.get("label") or b.get("key") or "阻塞").strip()
        detail = str(b.get("detail") or "").strip()
        out.append(f"{label} · {detail}" if detail else label)
    return out


def _pipeline_trace_summary(
    pipeline_step: str,
    *,
    blockers: Optional[list[dict[str, Any]]],
    step_status: str,
) -> str:
    step_label = _PIPELINE_STEP_LABELS.get(pipeline_step, pipeline_step)
    if blockers:
        summaries = _format_blocker_summaries(blockers)
        return f"{step_label}：{'；'.join(summaries[:4])}"
    if step_status == "ok":
        return f"{step_label} 已通过"
    if step_status == "running":
        return f"{step_label} 进行中"
    return step_label


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
    summaries = _format_blocker_summaries(blockers)
    record_workflow_step(
        key,
        "pipeline_advance",
        status=step_status,  # type: ignore[arg-type]
        actor="user" if operator and operator != "system" else "system",
        evidence={
            "pipeline_step": pipeline_step,
            "pipeline_step_label": _PIPELINE_STEP_LABELS.get(pipeline_step, pipeline_step),
            "ord_line_stat": ord_line_stat,
            "blocker_count": len(blockers or []),
            "blocker_summaries": summaries[:8],
            "summary": _pipeline_trace_summary(
                pipeline_step,
                blockers=blockers,
                step_status=step_status,
            ),
        },
        blockers=blockers,
    )

"""WorkflowRun 引擎：ensure / record_step / 状态推导。"""

from __future__ import annotations

import logging
from typing import Any, Optional

from app.services.workflow.run_store import get_workflow_run, save_workflow_run
from app.services.workflow.types import StepActor, StepStatus, WorkflowStep

_log = logging.getLogger(__name__)

_STEP_ORDER: list[WorkflowStep] = [
    "pay_accept",
    "category_map",
    "admin_writeback",
    "release_gate",
    "pipeline_advance",
]

_TERMINAL_STEP_STATUS: set[str] = {"ok", "failed", "skipped", "blocked"}


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _run_id(ord_line_no: str) -> str:
    return f"wf-{ord_line_no.strip()}"


def _derive_run_status(step: WorkflowStep, step_status: StepStatus) -> str:
    if step_status == "blocked":
        return "blocked"
    if step_status == "failed":
        return "failed"
    if step == "pipeline_advance" and step_status == "ok":
        return "completed"
    return "running"


def ensure_workflow_run(
    ord_line_no: str,
    *,
    ord_no: Optional[str] = None,
    item_id: Optional[str] = None,
) -> dict[str, Any]:
    """确保 ord_line_no 存在 WorkflowRun；不存在则创建。"""
    key = ord_line_no.strip()
    existing = get_workflow_run(key)
    if existing:
        changed = False
        patch: dict[str, Any] = dict(existing)
        if ord_no and not patch.get("ord_no"):
            patch["ord_no"] = ord_no
            changed = True
        if item_id and not patch.get("item_id"):
            patch["item_id"] = item_id
            changed = True
        if changed:
            return save_workflow_run(patch)
        return existing

    now = _now_iso()
    run: dict[str, Any] = {
        "run_id": _run_id(key),
        "ord_line_no": key,
        "ord_no": ord_no,
        "item_id": item_id,
        "workflow_type": "procurement_fulfillment",
        "current_step": "pay_accept",
        "status": "running",
        "step_history": [],
        "blockers": [],
        "created_at": now,
        "updated_at": now,
    }
    return save_workflow_run(run)


def record_workflow_step(
    ord_line_no: str,
    step: WorkflowStep,
    *,
    status: StepStatus,
    actor: StepActor = "system",
    evidence: Optional[dict[str, Any]] = None,
    linked_refs: Optional[dict[str, str]] = None,
    blockers: Optional[list[dict[str, Any]]] = None,
    ord_no: Optional[str] = None,
    item_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """追加一步 trace 并更新 WorkflowRun 当前状态。"""
    key = ord_line_no.strip()
    if not key:
        return None
    try:
        run = ensure_workflow_run(key, ord_no=ord_no, item_id=item_id)
        history = list(run.get("step_history") or [])
        entry: dict[str, Any] = {
            "step": step,
            "status": status,
            "actor": actor,
            "at": _now_iso(),
        }
        if evidence:
            entry["evidence"] = evidence
        if linked_refs:
            entry["linked_refs"] = linked_refs
        history.append(entry)

        patch: dict[str, Any] = {
            **run,
            "current_step": step,
            "status": _derive_run_status(step, status),
            "step_history": history[-100:],
        }
        if blockers is not None:
            patch["blockers"] = blockers
        elif status == "blocked" and evidence:
            patch["blockers"] = [
                {
                    "step": step,
                    "detail": evidence.get("detail") or evidence.get("error") or str(evidence)[:200],
                }
            ]
        return save_workflow_run(patch)
    except Exception as exc:
        _log.warning("record_workflow_step failed ord_line=%s step=%s: %s", key, step, exc)
        return None


def get_workflow_run_for_line(ord_line_no: str) -> Optional[dict[str, Any]]:
    return get_workflow_run(ord_line_no.strip())


def list_workflow_runs(*, limit: int = 200, status: Optional[str] = None) -> list[dict[str, Any]]:
    from app.services.workflow.run_store import list_workflow_runs as _list

    return _list(limit=limit, status=status)

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


def _blocker_fingerprint(blockers: Optional[list[dict[str, Any]]]) -> str:
    if not blockers:
        return ""
    keys: list[str] = []
    for b in blockers:
        if not isinstance(b, dict):
            continue
        keys.append(
            "|".join(
                [
                    str(b.get("key") or ""),
                    str(b.get("label") or ""),
                    str(b.get("detail") or "")[:80],
                ]
            )
        )
    return ";".join(sorted(keys))


def _step_fingerprint(entry: dict[str, Any]) -> str:
    ev = entry.get("evidence") if isinstance(entry.get("evidence"), dict) else {}
    return "|".join(
        [
            str(entry.get("step") or ""),
            str(entry.get("status") or ""),
            str(ev.get("pipeline_step") or ""),
            str(ev.get("result") or ""),
            _blocker_fingerprint(entry.get("blockers") if isinstance(entry.get("blockers"), list) else None),
        ]
    )


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
        if blockers:
            entry["blockers"] = blockers

        # 相同步骤+状态+阻塞指纹：只刷新时间，避免 sync 刷屏
        if history and _step_fingerprint(history[-1]) == _step_fingerprint(entry):
            history[-1] = {**history[-1], "at": entry["at"]}
        else:
            history.append(entry)

        patch: dict[str, Any] = {
            **run,
            "current_step": step,
            "status": _derive_run_status(step, status),
            "step_history": history[-100:],
            "updated_at": _now_iso(),
        }
        if blockers is not None:
            patch["blockers"] = blockers
        elif status == "blocked":
            step_blockers = entry.get("blockers")
            if isinstance(step_blockers, list) and step_blockers:
                patch["blockers"] = step_blockers
            elif evidence:
                patch["blockers"] = [
                    {
                        "step": step,
                        "detail": evidence.get("summary")
                        or evidence.get("detail")
                        or evidence.get("error")
                        or str(evidence)[:200],
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

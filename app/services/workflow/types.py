"""WorkflowRun 类型定义（采购履约端到端 trace）。"""

from __future__ import annotations

from typing import Any, Literal, Optional, TypedDict

WorkflowType = Literal["procurement_fulfillment"]

WorkflowRunStatus = Literal["running", "blocked", "completed", "failed"]

WorkflowStep = Literal[
    "pay_accept",
    "category_map",
    "admin_writeback",
    "release_gate",
    "pipeline_advance",
]

StepStatus = Literal["running", "ok", "failed", "skipped", "blocked"]

StepActor = Literal["system", "agent", "user", "rule"]


class WorkflowStepRecord(TypedDict, total=False):
    step: WorkflowStep
    status: StepStatus
    actor: StepActor
    evidence: dict[str, Any]
    linked_refs: dict[str, str]
    at: str


class WorkflowRun(TypedDict, total=False):
    run_id: str
    ord_line_no: str
    ord_no: Optional[str]
    item_id: Optional[str]
    workflow_type: WorkflowType
    current_step: WorkflowStep
    status: WorkflowRunStatus
    step_history: list[WorkflowStepRecord]
    blockers: list[dict[str, Any]]
    created_at: str
    updated_at: str

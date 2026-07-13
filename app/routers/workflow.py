"""WorkflowRun 查询 API。"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request

from app.api.deps import require_auth
from app.services.workflow.aggregate import enrich_workflow_run
from app.services.workflow.engine import get_workflow_run_for_line, list_workflow_runs

router = APIRouter(prefix="/api/workflow", tags=["workflow"])


@router.get("/runs")
def list_runs(
    request: Request,
    limit: int = Query(100, ge=1, le=500),
    status: Optional[str] = Query(None, description="running|blocked|completed|failed"),
    enrich: bool = Query(False, description="附加 invocation 摘要"),
) -> dict:
    require_auth(request)
    items = list_workflow_runs(limit=limit, status=status)
    if enrich:
        items = [
            enrich_workflow_run(r, include_invocations=False)
            for r in items
        ]
    return {"items": items, "total": len(items)}


@router.get("/runs/{ord_line_no}")
def get_run(
    request: Request,
    ord_line_no: str,
    include_invocations: bool = Query(True),
) -> dict:
    require_auth(request)
    run = get_workflow_run_for_line(ord_line_no)
    if not run:
        raise HTTPException(status_code=404, detail="WorkflowRun 不存在")
    return {"run": enrich_workflow_run(run, include_invocations=include_invocations)}

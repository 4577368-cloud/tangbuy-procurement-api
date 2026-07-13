"""后台任务状态查询。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.api.deps import require_auth
from app.services.background_jobs import get_job

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.get("/{job_id}")
def read_job(request: Request, job_id: str) -> dict:
    require_auth(request)
    row = get_job(job_id)
    if not row:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    return row

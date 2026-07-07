"""HTTP 路由 — Skill 审计。"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.api.deps import require_auth
from app.services.skill_audit import store as audit_store

router = APIRouter(prefix="/api/agent/skill-audit", tags=["skill-audit"])


class AuditBody(BaseModel):
    action: Optional[str] = None
    invocation_id: Optional[str] = None
    issue: Optional[str] = None
    agent_instruction: Optional[str] = None
    note: Optional[str] = None
    id: Optional[str] = None


@router.get("")
def audit_overview(request: Request, days: int = 7) -> dict[str, Any]:
    require_auth(request)
    period = days if days >= 0 else 7
    return audit_store.get_skill_audit_overview(period)


@router.post("")
def audit_action(request: Request, body: AuditBody) -> dict[str, Any]:
    user = require_auth(request)
    if body.action == "deactivate" and body.id:
        return {"ok": audit_store.deactivate_tuning_entry(body.id)}
    if body.action == "audit_ok":
        if not body.invocation_id:
            raise HTTPException(status_code=400, detail="缺少 invocation_id")
        inv = audit_store.audit_invocation_ok(body.invocation_id.strip())
        if not inv:
            raise HTTPException(status_code=404, detail="执行记录不存在")
        return {"invocation": inv}
    if body.action == "audit_tune":
        if not body.invocation_id or not body.issue or not body.agent_instruction:
            raise HTTPException(status_code=400, detail="缺少 invocation_id / issue / agent_instruction")
        outcome = audit_store.audit_invocation_with_patch(
            invocation_id=body.invocation_id.strip(),
            issue=body.issue,
            agent_instruction=body.agent_instruction.strip(),
            created_by=user.account,
        )
        if outcome.get("error"):
            raise HTTPException(status_code=400, detail=outcome["error"])
        return outcome
    if body.action == "audit_badcase":
        if not body.invocation_id:
            raise HTTPException(status_code=400, detail="缺少 invocation_id")
        outcome = audit_store.audit_invocation_badcase(
            body.invocation_id.strip(),
            note=body.note,
            created_by=user.account,
        )
        if outcome.get("error"):
            raise HTTPException(status_code=400, detail=outcome["error"])
        return outcome
    raise HTTPException(status_code=400, detail="未知 action")

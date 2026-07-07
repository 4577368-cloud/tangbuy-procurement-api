"""HTTP 路由 — 配置中心。"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.api.deps import require_auth
from app.auth.permissions import Role, grants_allow
from app.config.store import config_snapshot, get_role_grants, update_business_config, update_matrix, set_user_role

router = APIRouter(prefix="/api/config", tags=["config"])


class ConfigPutBody(BaseModel):
    business: Optional[dict[str, Any]] = None
    matrix: Optional[dict[str, Any]] = None
    userRoles: Optional[list[dict[str, str]]] = None


@router.get("")
def get_config(request: Request) -> dict[str, Any]:
    user = require_auth(request)
    grants = get_role_grants(user.role)
    can_view = grants_allow(grants, "config.business", "view") or grants_allow(
        grants, "config.permission", "view"
    )
    if not can_view:
        raise HTTPException(status_code=403, detail="无权限")
    return config_snapshot()


@router.put("")
def put_config(request: Request, body: ConfigPutBody) -> dict[str, Any]:
    user = require_auth(request)
    grants = get_role_grants(user.role)
    if body.business is not None:
        if not grants_allow(grants, "config.business", "edit"):
            raise HTTPException(status_code=403, detail="无「业务参数」编辑权限")
        update_business_config(body.business)
    if body.matrix is not None:
        if not grants_allow(grants, "config.permission", "edit"):
            raise HTTPException(status_code=403, detail="无「权限配置」编辑权限")
        update_matrix(body.matrix)  # type: ignore[arg-type]
    if body.userRoles is not None:
        if not grants_allow(grants, "config.permission", "edit"):
            raise HTTPException(status_code=403, detail="无「权限配置」编辑权限")
        for entry in body.userRoles:
            account = (entry.get("account") or "").strip()
            role = (entry.get("role") or "").strip()
            if account and role in ("admin", "buyer", "bd"):
                set_user_role(account, role)  # type: ignore[arg-type]
    return config_snapshot()

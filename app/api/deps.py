"""FastAPI 依赖（认证）。"""

from __future__ import annotations

from typing import Optional

from fastapi import HTTPException, Request

from app.auth.permissions import grants_allow
from app.auth.session import get_auth_context, get_current_user
from app.auth.users import AppUser
from app.config.store import get_role_grants


def optional_auth(request: Request) -> Optional[dict]:
    return get_auth_context(request)


def require_auth(request: Request) -> AppUser:
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    return user


def require_permission(request: Request, item_key: str, action: str = "edit") -> AppUser:
    user = require_auth(request)
    grants = get_role_grants(user.role)
    if action not in ("view", "edit") or not grants_allow(grants, item_key, action):  # type: ignore[arg-type]
        raise HTTPException(status_code=403, detail=f"无权限：{item_key}")
    return user

"""HMAC 会话 cookie（与 Next src/lib/auth/session.ts 兼容）。"""

from __future__ import annotations

import base64
import hmac
import hashlib

from fastapi import Request, Response

from app.auth.permissions import RoleGrants, grants_allow
from app.auth.users import AppUser, to_public_user
from app.config.store import find_user, get_role_grants
from app.core.config import get_settings

SESSION_COOKIE = "tangbuy_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 7


def _secret() -> bytes:
    return get_settings().auth_session_secret.encode()


def sign(account: str) -> str:
    digest = hmac.new(_secret(), account.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def make_session_token(account: str) -> str:
    return f"{account}.{sign(account)}"


def parse_token(token: str | None) -> str | None:
    if not token:
        return None
    idx = token.rfind(".")
    if idx <= 0:
        return None
    account = token[:idx]
    sig = token[idx + 1 :]
    expected = sign(account)
    if len(sig) != len(expected):
        return None
    if not hmac.compare_digest(sig, expected):
        return None
    return account


def get_current_user(request: Request) -> AppUser | None:
    account = parse_token(request.cookies.get(SESSION_COOKIE))
    if not account:
        return None
    return find_user(account)


def get_auth_context(request: Request) -> dict | None:
    user = get_current_user(request)
    if not user:
        return None
    return {
        "user": to_public_user(user).model_dump(),
        "role": user.role,
        "grants": get_role_grants(user.role),
    }


def user_can(user: AppUser, item_key: str, action: str) -> bool:
    grants = get_role_grants(user.role)
    if action not in ("view", "edit"):
        return False
    return grants_allow(grants, item_key, action)  # type: ignore[arg-type]


def establish_session(response: Response, account: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE,
        value=make_session_token(account),
        httponly=True,
        samesite="lax",
        path="/",
        max_age=SESSION_MAX_AGE,
    )


def clear_session(response: Response) -> None:
    response.delete_cookie(key=SESSION_COOKIE, path="/")

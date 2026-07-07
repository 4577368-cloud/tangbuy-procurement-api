from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from app.auth.session import clear_session, establish_session, get_auth_context
from app.auth.users import verify_password, to_public_user
from app.config.store import find_user, get_role_grants

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginBody(BaseModel):
    account: str = ""
    password: str = ""


@router.post("/login")
def login(body: LoginBody, response: Response) -> dict:
    account = body.account.strip()
    password = body.password
    if not account or not password:
        raise HTTPException(status_code=400, detail="请输入账号和密码")

    user = find_user(account)
    if not user or not verify_password(user, password):
        raise HTTPException(status_code=401, detail="账号或密码错误")

    establish_session(response, user.account)
    return {
        "user": to_public_user(user).model_dump(),
        "role": user.role,
        "grants": get_role_grants(user.role),
    }


@router.get("/me")
def me(request: Request) -> dict:
    ctx = get_auth_context(request)
    if not ctx:
        return {"user": None}
    return ctx


@router.post("/logout")
def logout(response: Response) -> dict[str, bool]:
    clear_session(response)
    return {"ok": True}

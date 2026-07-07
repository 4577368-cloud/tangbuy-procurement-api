"""用户目录（种子用户；后续接 UserDirectoryPort）。"""

from __future__ import annotations

import hashlib
import hmac
from typing import Literal, Optional

from pydantic import BaseModel

Role = Literal["bd", "buyer", "admin"]

DEFAULT_PASSWORD = "tangbuy123"


class AppUser(BaseModel):
    account: str
    name: str
    role: Role
    password_hash: Optional[str] = None

    class Config:
        populate_by_name = True


class PublicUser(BaseModel):
    account: str
    name: str
    role: Role


SEED_USERS: list[AppUser] = [
    AppUser(account="jody", name="Jody", role="bd"),
    AppUser(account="lydia", name="Lydia", role="bd"),
    AppUser(account="kevin", name="Kevin", role="bd"),
    AppUser(account="sunyutian", name="孙玉田", role="buyer"),
    AppUser(account="guifeng", name="贵峰", role="admin"),
    AppUser(account="laok", name="老K", role="admin"),
    AppUser(account="xuezhi", name="雪芝", role="admin"),
]


def hash_password(pw: str) -> str:
    return hashlib.sha256(f"tangbuy::{pw}".encode()).hexdigest()


def verify_password(user: AppUser, password: str) -> bool:
    target = user.password_hash or hash_password(DEFAULT_PASSWORD)
    provided = hash_password(password)
    return hmac.compare_digest(target, provided)


def to_public_user(user: AppUser) -> PublicUser:
    return PublicUser(account=user.account, name=user.name, role=user.role)

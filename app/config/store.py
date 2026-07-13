"""配置中心（DB 或 data/config/config-center.json）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.auth.permissions import PermissionMatrix, RoleGrants, merge_matrix
from app.auth.users import SEED_USERS, AppUser, Role, to_public_user
from app.config.business_config import normalize_business_config
from app.core.paths import data_dir
from app.db.session import db_session, is_db_enabled

_cache: dict[str, Any] | None = None


def _store_path() -> Path:
    return data_dir() / "config" / "config-center.json"


def _normalize_loaded(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "business": normalize_business_config(raw.get("business")),
        "matrix": merge_matrix(raw.get("matrix")),
        "user_roles": raw.get("userRoles") or raw.get("user_roles") or {},
    }


def _load() -> dict[str, Any]:
    global _cache
    if _cache is not None and not is_db_enabled():
        return _cache

    raw: dict[str, Any] = {}
    if is_db_enabled():
        from app.db.catalog_repos import ConfigRepository

        with db_session() as session:
            stored = ConfigRepository(session).load()
            if stored:
                raw = stored
    else:
        path = _store_path()
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                raw = {}

    normalized = _normalize_loaded(raw)
    if not is_db_enabled():
        _cache = normalized
    return normalized


def _persist(data: dict[str, Any]) -> None:
    global _cache
    payload = {
        "business": data["business"],
        "matrix": data["matrix"],
        "userRoles": data["user_roles"],
    }
    if is_db_enabled():
        from app.db.catalog_repos import ConfigRepository

        with db_session() as session:
            ConfigRepository(session).save(payload)
        return

    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    _cache = data


def get_business_config() -> dict[str, Any]:
    return _load()["business"]


def update_business_config(patch: dict[str, Any]) -> dict[str, Any]:
    cur = _load()
    merged = normalize_business_config({**cur["business"], **patch})
    _persist({**cur, "business": merged})
    return merged


def update_matrix(matrix: PermissionMatrix) -> PermissionMatrix:
    cur = _load()
    merged = merge_matrix(matrix)
    _persist({**cur, "matrix": merged})
    return merged


def set_user_role(account: str, role: Role) -> None:
    cur = _load()
    roles = dict(cur["user_roles"])
    roles[account] = role
    _persist({**cur, "user_roles": roles})


def config_snapshot() -> dict[str, Any]:
    cur = _load()
    return {
        "business": cur["business"],
        "matrix": cur["matrix"],
        "users": [to_public_user(u).model_dump() for u in list_users()],
    }


def get_matrix() -> PermissionMatrix:
    return _load()["matrix"]


def get_role_grants(role: Role) -> RoleGrants:
    matrix = get_matrix()
    return matrix.get(role) or merge_matrix()["admin"]


def list_users() -> list[AppUser]:
    user_roles: dict[str, Role] = _load()["user_roles"]
    out: list[AppUser] = []
    for seed in SEED_USERS:
        role = user_roles.get(seed.account, seed.role)
        out.append(seed.model_copy(update={"role": role}))
    return out


def find_user(account: str) -> AppUser | None:
    acc = account.strip().lower()
    for user in list_users():
        if user.account.lower() == acc:
            return user
    return None

"""RBAC 权限矩阵（与 src/lib/auth/permissions.ts 默认矩阵一致）。"""

from __future__ import annotations

from typing import Literal

Role = Literal["bd", "buyer", "admin"]
PermissionAction = Literal["view", "edit"]
ActionGrant = dict[str, bool]
RoleGrants = dict[str, ActionGrant]
PermissionMatrix = dict[Role, RoleGrants]

PERMISSION_ITEMS: list[tuple[str, list[PermissionAction]]] = [
    ("assistant.sourcing", ["edit"]),
    ("assistant.image_search", ["edit"]),
    ("assistant.order_followup", ["edit"]),
    ("assistant.consult", ["edit"]),
    ("product.catalog", ["view", "edit"]),
    ("product.category_mapping", ["view", "edit"]),
    ("product.add_to_store", ["edit"]),
    ("order.data", ["view"]),
    ("order.disposition", ["view", "edit"]),
    ("task.data", ["view"]),
    ("task.control", ["edit"]),
    ("config.business", ["view", "edit"]),
    ("config.permission", ["view", "edit"]),
]


def _grant_all() -> RoleGrants:
    grants: RoleGrants = {}
    for key, actions in PERMISSION_ITEMS:
        grants[key] = {
            "view": "view" in actions,
            "edit": "edit" in actions,
        }
    return grants


DEFAULT_MATRIX: PermissionMatrix = {
    "admin": _grant_all(),
    "bd": {
        "assistant.sourcing": {"edit": True},
        "assistant.image_search": {"edit": True},
        "assistant.order_followup": {"edit": True},
        "assistant.consult": {"edit": True},
        "product.catalog": {"view": True},
        "product.category_mapping": {"view": True, "edit": True},
        "product.add_to_store": {"edit": True},
        "order.data": {"view": True},
        "order.disposition": {"view": True},
        "task.data": {"view": True},
        "task.control": {"edit": True},
    },
    "buyer": {
        "assistant.sourcing": {"edit": True},
        "assistant.image_search": {"edit": True},
        "assistant.order_followup": {"edit": True},
        "assistant.consult": {"edit": True},
        "product.catalog": {"view": True},
        "product.category_mapping": {"view": True, "edit": True},
        "order.data": {"view": True},
        "order.disposition": {"view": True, "edit": True},
        "task.data": {"view": True},
        "task.control": {"edit": True},
    },
}


def merge_matrix(override: dict[str, RoleGrants] | None = None) -> PermissionMatrix:
    out: PermissionMatrix = {}
    for role in ("admin", "bd", "buyer"):
        base = dict(DEFAULT_MATRIX[role])
        if override and role in override:
            base.update(override[role])
        out[role] = base
    return out


def grants_allow(grants: RoleGrants | None, item_key: str, action: PermissionAction) -> bool:
    if not grants:
        return False
    return grants.get(item_key, {}).get(action) is True

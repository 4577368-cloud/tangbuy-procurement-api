from __future__ import annotations

import socket
from typing import Any

from fastapi import APIRouter

from app.core.config import get_settings
from app.integrations.tangbuy_admin.token_store import resolve_admin_token

router = APIRouter(tags=["health"])


def _admin_host_probe() -> dict[str, Any]:
    settings = get_settings()
    base = settings.tangbuy_admin_base_url.rstrip("/")
    host = base.split("//", 1)[-1].split("/", 1)[0].strip()
    try:
        addrs = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        return {"host": host, "ok": True, "addrs": [a[4][0] for a in addrs[:3]]}
    except OSError as exc:
        return {"host": host, "ok": False, "error": str(exc)}


@router.get("/api/health")
def health() -> dict[str, object]:
    token = resolve_admin_token().strip()
    admin_configured = bool(token) and token != "your-admin-bearer-token"
    settings = get_settings()
    return {
        "status": "ok",
        "service": "tangbuy-procurement-api",
        "admin_configured": admin_configured,
        "admin_base_url": settings.tangbuy_admin_base_url,
        "admin_connect_ip": settings.tangbuy_admin_connect_ip or None,
        "admin_dns": _admin_host_probe(),
    }

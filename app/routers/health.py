from __future__ import annotations

import socket
from typing import Any

from fastapi import APIRouter

from app.core.config import get_settings
from app.integrations.tangbuy_admin.token_store import resolve_admin_token

router = APIRouter(tags=["health"])


def _admin_host_from_settings() -> str:
    base = get_settings().tangbuy_admin_base_url.rstrip("/")
    return base.split("//", 1)[-1].split("/", 1)[0].strip()


def _admin_dns_probe(host: str) -> dict[str, Any]:
    try:
        addrs = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        return {"ok": True, "addrs": [a[4][0] for a in addrs[:3]]}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}


def _admin_connect_probe(host: str, connect_ip: str) -> dict[str, Any]:
    ip = (connect_ip or "").strip()
    if not ip:
        return {"ok": False, "skipped": True}
    try:
        with socket.create_connection((ip, 443), timeout=8):
            pass
        return {"ok": True, "ip": ip}
    except OSError as exc:
        return {"ok": False, "ip": ip, "error": str(exc)}


def _newton_ak_probe() -> dict[str, Any]:
    try:
        import newton_cli  # noqa: WPS433

        ready = bool(newton_cli._ak_ready())
        return {"ok": ready, "configured": ready}
    except Exception as exc:
        return {"ok": False, "configured": False, "error": str(exc)}


@router.get("/api/health")
def health() -> dict[str, object]:
    token = resolve_admin_token().strip()
    admin_configured = bool(token) and token != "your-admin-bearer-token"
    settings = get_settings()
    host = _admin_host_from_settings()
    connect_ip = settings.tangbuy_admin_connect_ip or ""
    dns = _admin_dns_probe(host)
    connect = _admin_connect_probe(host, connect_ip)
    admin_reachable = connect.get("ok") is True or dns.get("ok") is True
    return {
        "status": "ok",
        "service": "tangbuy-procurement-api",
        "admin_configured": admin_configured,
        "admin_base_url": settings.tangbuy_admin_base_url,
        "admin_connect_ip": connect_ip or None,
        "admin_reachable": admin_reachable,
        "admin_dns": {"host": host, **dns},
        "admin_connect": {"host": host, **connect},
        "newton_ak": _newton_ak_probe(),
    }

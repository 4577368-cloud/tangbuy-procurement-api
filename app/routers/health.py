from fastapi import APIRouter

from app.core.config import get_settings
from app.integrations.tangbuy_admin.token_store import resolve_admin_token

router = APIRouter(tags=["health"])


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
    }

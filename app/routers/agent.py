from __future__ import annotations

import sys
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.deps import require_auth
from app.auth.permissions import RoleGrants
from app.config.store import get_role_grants
from app.core.config import get_settings
from app.core.paths import PROJECT_ROOT, data_dir
from app.services.agent.orchestrator import run_agent_chat
from app.services.agent.skills import LEGACY_SKILLS, UNIFIED_ASSISTANT_ID

router = APIRouter(prefix="/api/agent", tags=["agent"])

_SCRIPTS = PROJECT_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


class ChatMessage(BaseModel):
    role: str
    content: str = ""


class ChatBody(BaseModel):
    skillId: Optional[str] = None
    messages: list[ChatMessage] = Field(default_factory=list)
    context: Optional[dict[str, Any]] = None
    intent: Optional[str] = None


@router.post("/chat")
def agent_chat(request: Request, body: ChatBody) -> dict[str, Any]:
    user = require_auth(request)
    if not body.messages:
        raise HTTPException(status_code=400, detail="messages 不能为空")

    skill_id = body.skillId or UNIFIED_ASSISTANT_ID
    grants: RoleGrants = get_role_grants(user.role)

    try:
        return run_agent_chat(
            skill_id,
            [m.model_dump() for m in body.messages],
            context=body.context,
            intent=body.intent,
            grants=grants,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/skills")
def agent_skills() -> dict[str, Any]:
    import newton_cli  # noqa: E402

    settings = get_settings()
    newton_ready = bool(newton_cli._ak_ready())
    category_ready = (data_dir() / "category" / "catalog.json").exists()

    unified = {
        "id": UNIFIED_ASSISTANT_ID,
        "name": "采购助手",
        "description": "统一对话：选品、比价、寻源、催单",
        "status": "ready",
        "welcomeMessage": "需要什么？直接说；长程任务点底部标签。",
        "toolCount": 11,
        "configured": newton_ready and category_ready,
        "isUnified": True,
    }

    legacy = []
    for skill in LEGACY_SKILLS:
        configured = None
        sid = skill["id"]
        if sid in (
            "1688-product-find",
            "product-compare",
            "1688-sourcing",
            "order-followup",
            "supplychain-procurement",
            "newton-cloud",
        ):
            configured = newton_ready
        elif sid == "category-mapping":
            configured = category_ready
        legacy.append({**skill, "configured": configured, "isUnified": False})

    return {
        "skills": [unified, *legacy],
        "defaultSkillId": UNIFIED_ASSISTANT_ID,
        "llm": {
            "configured": settings.llm_configured,
            "model": settings.llm_model_model_id or None,
        },
        "integrations": {
            "newtonAk": newton_ready,
            "categoryData": category_ready,
        },
    }

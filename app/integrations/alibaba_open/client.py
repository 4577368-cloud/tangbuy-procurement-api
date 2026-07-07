"""1688 开放平台 param2 客户端（直接 import scripts/alibaba_open_cli）。"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from app.core.paths import PROJECT_ROOT

_SCRIPTS = PROJECT_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import alibaba_open_cli as open_cli  # noqa: E402


@dataclass
class CallOutcome:
    ok: bool
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None


def open_api_call(
    namespace: str,
    name: str,
    params: dict[str, Any],
    version: str = "1",
    use_token: bool = True,
) -> CallOutcome:
    try:
        raw = open_cli.api_call(namespace, name, params, version=version, use_token=use_token)
    except Exception as exc:
        return CallOutcome(ok=False, error=str(exc))

    err = open_cli._extract_error(raw)
    if err:
        return CallOutcome(ok=False, error=err, result=raw)

    inner = raw.get("result") if isinstance(raw.get("result"), dict) else raw
    return CallOutcome(ok=True, result=inner if isinstance(inner, dict) else raw)


def get_newton_api_key() -> Optional[str]:
    import os

    key = os.environ.get("ALIBABA_NEWTON_APIKEY", "").strip()
    return key or None


def exchange_code(code: str) -> dict[str, Any]:
    """OAuth code 换 token。"""
    cfg = open_cli._config()
    if not cfg.get("app_key") or not cfg.get("app_secret"):
        return {"success": False, "error": "未配置 AppKey/AppSecret"}
    result = open_cli._oauth_token_request(
        {
            "grant_type": "authorization_code",
            "need_refresh_token": "true",
            "redirect_uri": cfg.get("redirect_uri", ""),
            "code": code,
        }
    )
    if not result.get("access_token"):
        return {"success": False, "error": f"换取 token 失败"}
    open_cli._persist_token(result)
    return {"success": True}


async def handle_message_push(request) -> dict[str, str]:
    """1688 消息推送（完整验签逻辑待接 lib/integrations/alibaba-open）。"""
    _ = await request.body()
    return {"status": "success"}


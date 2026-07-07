"""HTTP 路由 — 1688 开放平台集成。"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.integrations.alibaba_open.client import exchange_code, handle_message_push

router = APIRouter(prefix="/api/integrations/alibaba-open", tags=["integrations"])


def _html_response(ok: bool, message: str) -> HTMLResponse:
    color = "#166534" if ok else "#b91c1c"
    title = "✅ 1688 授权完成" if ok else "❌ 1688 授权失败"
    html = (
        f"<!doctype html><html lang='zh'><head><meta charset='utf-8'/>"
        f"<title>{title}</title></head>"
        f"<body style='font-family:system-ui;padding:48px'>"
        f"<h1 style='color:{color}'>{title}</h1>"
        f"<p>{message}</p></body></html>"
    )
    return HTMLResponse(content=html, status_code=200 if ok else 400)


@router.get("/callback")
def oauth_callback(code: Optional[str] = None, error: Optional[str] = None) -> HTMLResponse:
    if error:
        return _html_response(False, f"授权被拒绝或失败：{error}")
    if not code:
        return _html_response(False, "回调缺少 code 参数")
    result = exchange_code(code)
    if not result.get("success"):
        return _html_response(False, result.get("error") or result.get("markdown") or "换取 token 失败")
    return _html_response(True, "授权成功，access_token 已保存。可关闭此页面。")


@router.api_route("/message", methods=["GET", "POST"])
async def message_webhook(request: Request) -> dict:
    return await handle_message_push(request)

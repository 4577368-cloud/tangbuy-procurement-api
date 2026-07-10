"""HTTP 路由 — 指挥中心简报。"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.services.command_center.briefing import get_command_center_stats, get_briefing_payload, stream_briefing

router = APIRouter(prefix="/api/command-center", tags=["command-center"])


@router.get("/stats")
def command_center_stats(force: bool = False) -> dict:
    return get_command_center_stats(force=force)


@router.get("/briefing/facts")
def briefing_facts(force: bool = False) -> dict:
    return get_briefing_payload(force=force)


@router.get("/briefing/stream")
def briefing_stream(force: bool = False) -> StreamingResponse:
    return StreamingResponse(
        stream_briefing(force=force),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )

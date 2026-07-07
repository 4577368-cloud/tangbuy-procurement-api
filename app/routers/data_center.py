"""HTTP 路由 — 数据中心聚合。"""

from __future__ import annotations

from fastapi import APIRouter

from app.services.data_center import get_data_center_snapshot

router = APIRouter(prefix="/api/data-center", tags=["data-center"])


@router.get("")
def data_center() -> dict:
    return get_data_center_snapshot()

"""HTTP 路由 — 真实订单（Tangbuy Admin）。"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.api.deps import require_auth

from app.services.orders import disposition as disposition_service
from app.services.orders import service as order_service
from app.services.orders.disposition import DispositionError
from app.services.orders.topup_message import TOPUP_LANG_META, translate_topup_message

router = APIRouter(prefix="/api/orders", tags=["orders"])


@router.get("/cached")
def list_cached_orders(
    request: Request,
    queue: Optional[str] = Query(None, description="队列筛选"),
    page: int = Query(1, ge=1),
    page_size: int = Query(200, ge=1, le=500),
) -> dict:
    require_auth(request)
    from app.services.orders import line_cache

    from app.services.orders.pipeline_store import enrich_rows_pipeline

    all_lines = line_cache.list_cached_lines(queue=queue)
    total = len(all_lines)
    start = (page - 1) * page_size
    slice_rows = all_lines[start : start + page_size]
    items = enrich_rows_pipeline(slice_rows)
    state = line_cache.load_sync_state()
    return {
        "items": items,
        "total": len(items),
        "page": page,
        "page_size": page_size,
        "cache_total": state.get("cached_total", total),
        "sync_state": {
            "last_incremental_at": state.get("last_incremental_at"),
            "last_backfill_at": state.get("last_backfill_at"),
            "backfill_complete": state.get("backfill_complete"),
        },
        "source": "cache",
    }


class OrderSyncBody(BaseModel):
    mode: str = "incremental"
    queue: Optional[str] = None
    page_size: Optional[int] = 200
    pages: Optional[int] = 2
    batches: Optional[int] = 1
    wait: bool = False


@router.post("/sync")
def sync_orders(request: Request, body: OrderSyncBody):
    require_auth(request)
    from app.services.background_jobs import create_job, run_job
    from app.services.orders.sync_jobs import execute_order_sync

    payload = {
        "mode": body.mode,
        "queue": body.queue,
        "page_size": body.page_size,
        "pages": body.pages,
        "batches": body.batches,
        "pipeline_inline": body.wait,
    }
    if body.wait:
        return execute_order_sync(**payload, source="api:wait")

    label = (body.mode or "incremental").strip().lower()
    job_id = create_job("order_sync", label=label)
    run_job(job_id, lambda: execute_order_sync(**payload, source="api:job"))
    return JSONResponse(
        status_code=202,
        content={"job_id": job_id, "status": "pending", "kind": "order_sync"},
    )


@router.get("")
def list_orders(
    request: Request,
    queue: Optional[str] = Query(None, description="作业队列，如 pending_procurement"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    ord_no: Optional[str] = None,
    ord_line_no: Optional[str] = None,
    live: bool = Query(False, description="true 时直连 Admin（默认读本地缓存）"),
) -> dict:
    require_auth(request)
    if live:
        result = order_service.list_ord_lines(
            queue=queue,
            page=page,
            page_size=page_size,
            ord_no=ord_no,
            ord_line_no=ord_line_no,
        )
        if ord_line_no or ord_no:
            from app.services.orders import line_cache

            items = result.get("items") or []
            if items:
                line_cache.merge_lines(items)
        return result
    return order_service.list_cached_ord_lines(
        queue=queue,
        page=page,
        page_size=page_size,
        ord_no=ord_no,
        ord_line_no=ord_line_no,
    )


@router.get("/summary")
def orders_summary(
    request: Request,
    live: bool = Query(False, description="true 时直连 Admin 统计（默认读本地缓存）"),
) -> dict:
    require_auth(request)
    if live:
        return order_service.queue_summary()
    return order_service.cached_queue_summary()


class OrderDispositionBody(BaseModel):
    ord_line_no: str = Field(..., min_length=1)
    action_key: str = Field(..., min_length=1)
    action_label: str = Field(..., min_length=1)
    signal_type: Optional[str] = None
    stage: Optional[str] = None
    feedback_type: Optional[str] = None
    override_reason: Optional[str] = None
    operator: Optional[str] = None


@router.post("/disposition")
def submit_order_disposition(body: OrderDispositionBody) -> dict:
    try:
        return disposition_service.submit_disposition(
            ord_line_no=body.ord_line_no,
            action_key=body.action_key,
            action_label=body.action_label,
            signal_type=body.signal_type,
            stage=body.stage,
            feedback_type=body.feedback_type,
            override_reason=body.override_reason,
            operator=body.operator,
        )
    except DispositionError as exc:
        from app.config.demo_submit import disposition_stub, is_demo_submit_always_success

        if is_demo_submit_always_success():
            return disposition_stub(
                ord_line_no=body.ord_line_no,
                action_key=body.action_key,
                stage_after=body.stage,
            )
        status = 404 if exc.code == "not_found" else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc


class PrePurchaseBody(BaseModel):
    ord_line_no: str = Field(..., min_length=1)
    force: bool = False
    operator: Optional[str] = None


@router.get("/auto-releases")
def list_auto_releases(
    request: Request,
    limit: int = Query(200, ge=1, le=500),
) -> dict:
    require_auth(request)
    from app.services.orders.procurement_accept import list_accept_audits
    from app.services.orders.procurement_release import list_auto_releases as list_releases

    releases = list_releases(limit=limit)
    accepts = list_accept_audits(limit=limit)
    items = accepts + releases
    items.sort(key=lambda x: str(x.get("released_at") or ""), reverse=True)
    return {"items": items[:limit], "total": len(items)}


@router.post("/1688/pre-purchase")
def submit_1688_pre_purchase_route(request: Request, body: PrePurchaseBody) -> dict:
    require_auth(request)
    from app.services.orders.procurement_release import (
        ProcurementReleaseError,
        submit_1688_pre_purchase,
    )

    try:
        return submit_1688_pre_purchase(
            body.ord_line_no,
            operator=body.operator,
            trigger="manual",
            force=body.force,
        )
    except ProcurementReleaseError as exc:
        from app.config.demo_submit import (
            is_demo_submit_always_success,
            pre_purchase_stub,
        )

        if is_demo_submit_always_success():
            return pre_purchase_stub(body.ord_line_no)
        status = 404 if exc.code == "not_found" else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc


class PlaceOrderBody(BaseModel):
    ord_line_nos: list[str] = Field(..., min_length=1)
    merge_same_store: bool = True
    operator: Optional[str] = None


@router.get("/1688/wait-generate")
def get_wait_generate_orders(
    request: Request,
    page_num: int = Query(1, ge=1),
    page_size: int = Query(200, ge=1, le=200),
    order_no: Optional[str] = None,
) -> dict:
    require_auth(request)
    from app.services.orders.procurement_place_order import (
        ProcurementPlaceOrderError,
        list_wait_generate_orders,
    )

    try:
        return list_wait_generate_orders(
            page_num=page_num,
            page_size=page_size,
            order_no=order_no,
        )
    except ProcurementPlaceOrderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/1688/place-order")
def submit_1688_place_order_route(request: Request, body: PlaceOrderBody) -> dict:
    require_auth(request)
    from app.services.orders.procurement_place_order import (
        ProcurementPlaceOrderError,
        submit_1688_place_order,
    )

    try:
        return submit_1688_place_order(
            body.ord_line_nos,
            operator=body.operator,
            trigger="manual",
            merge_same_store=body.merge_same_store,
        )
    except ProcurementPlaceOrderError as exc:
        from app.config.demo_submit import (
            is_demo_submit_always_success,
            place_order_stub,
        )

        if is_demo_submit_always_success():
            return place_order_stub(body.ord_line_nos)
        status = 404 if exc.code == "not_found" else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc


@router.get("/1688/place-orders")
def list_1688_place_orders(
    request: Request,
    limit: int = Query(200, ge=1, le=500),
) -> dict:
    require_auth(request)
    from app.services.orders.procurement_place_order import list_place_order_audits

    items = list_place_order_audits(limit=limit)
    return {"items": items, "total": len(items)}


class PlatformOrderSyncBody(BaseModel):
    ord_line_nos: list[str] = Field(..., min_length=1)


@router.get("/1688/platform-order")
def get_platform_order(
    request: Request,
    ord_line_no: str = Query(..., min_length=1),
    sync: bool = Query(False, description="true 时从 Admin 拉取最新回传"),
) -> dict:
    require_auth(request)
    from app.integrations.tangbuy_admin.client import TangbuyAdminError
    from app.services.orders.platform_order_sync import (
        get_platform_order_view,
        sync_platform_order_for_line,
    )
    from app.services.orders.service import get_ord_line

    key = ord_line_no.strip()
    if sync:
        row = get_ord_line(key) or {}
        ord_no = str(row.get("ord_no") or "").strip() or None
        try:
            fields = sync_platform_order_for_line(key, ord_no=ord_no)
        except TangbuyAdminError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        if not fields:
            raise HTTPException(status_code=404, detail="未找到 1688 订单回传")
        return {"item": fields, "source": "admin"}

    item = get_platform_order_view(key)
    if not item:
        raise HTTPException(status_code=404, detail="暂无 1688 订单回传，可传 sync=true 拉取")
    return {"item": item, "source": "cache"}


@router.post("/1688/platform-order/sync")
def sync_platform_orders(request: Request, body: PlatformOrderSyncBody) -> dict:
    require_auth(request)
    from app.integrations.tangbuy_admin.client import TangbuyAdminError
    from app.services.orders.platform_order_sync import sync_platform_orders_for_lines
    from app.services.orders.service import get_ord_line

    rows_by_line = {
        key: get_ord_line(key) or {}
        for key in body.ord_line_nos
        if str(key).strip()
    }
    try:
        return sync_platform_orders_for_lines(body.ord_line_nos, rows_by_line=rows_by_line)
    except TangbuyAdminError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/1688/platform-order/query")
def query_platform_orders_route(
    request: Request,
    page_num: int = Query(1, ge=1),
    page_size: int = Query(30, ge=1, le=200),
    status: int = Query(0),
    checking: int = Query(0),
    trade_order_no: Optional[str] = None,
    order_no: Optional[str] = None,
    item_no: Optional[str] = None,
) -> dict:
    require_auth(request)
    from app.integrations.tangbuy_admin.alibaba_order_api import query_platform_orders
    from app.integrations.tangbuy_admin.client import TangbuyAdminError

    try:
        return query_platform_orders(
            page_num=page_num,
            page_size=page_size,
            status=status,
            checking=checking,
            trade_order_no=trade_order_no,
            order_no=order_no,
            item_no=item_no,
        )
    except TangbuyAdminError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


class PipelineRunBody(BaseModel):
    ord_line_nos: Optional[list[str]] = None


@router.post("/pipeline/run")
def run_pipeline(request: Request, body: PipelineRunBody) -> dict:
    require_auth(request)
    from app.services.orders.procurement_pipeline import run_pipeline_batch

    return run_pipeline_batch(body.ord_line_nos, trigger="manual")


class PipelineAckBody(BaseModel):
    ord_line_no: str = Field(..., min_length=1)
    blocker_key: str = Field(..., min_length=1)
    operator: Optional[str] = None


@router.post("/pipeline/ack")
def ack_pipeline_blocker(request: Request, body: PipelineAckBody) -> dict:
    require_auth(request)
    from app.config.demo_submit import demo_submit_stub, is_demo_submit_always_success
    from app.services.orders.procurement_pipeline import ack_blocker_and_resume

    try:
        return ack_blocker_and_resume(body.ord_line_no, body.blocker_key, operator=body.operator)
    except Exception:
        if is_demo_submit_always_success():
            return demo_submit_stub(
                ord_line_no=body.ord_line_no,
                blocker_key=body.blocker_key,
            )
        raise


@router.get("/pipeline")
def list_pipeline_states_route(
    request: Request,
    limit: int = Query(200, ge=1, le=500),
) -> dict:
    require_auth(request)
    from app.services.orders import pipeline_store

    items = pipeline_store.list_pipeline_states(limit=limit)
    return {"items": items, "total": len(items)}


@router.get("/{ord_line_no}/pipeline")
def get_order_pipeline(ord_line_no: str, request: Request) -> dict:
    require_auth(request)
    from app.services.orders.procurement_pipeline import get_pipeline_view

    return get_pipeline_view(ord_line_no)


class FlagReleaseBody(BaseModel):
    ord_line_no: str = Field(..., min_length=1)
    note: Optional[str] = None
    operator: Optional[str] = None


@router.post("/auto-releases/flag")
def flag_auto_release(request: Request, body: FlagReleaseBody) -> dict:
    require_auth(request)
    from app.services.orders.procurement_release import ProcurementReleaseError, flag_release

    try:
        return flag_release(body.ord_line_no, note=body.note, operator=body.operator)
    except ProcurementReleaseError as exc:
        from app.config.demo_submit import flag_release_stub, is_demo_submit_always_success

        if is_demo_submit_always_success():
            return flag_release_stub(body.ord_line_no)
        status = 404 if exc.code == "not_found" else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc


class AcknowledgeReleaseBody(BaseModel):
    ord_line_no: str = Field(..., min_length=1)
    note: Optional[str] = None
    operator: Optional[str] = None


@router.post("/auto-releases/acknowledge")
def acknowledge_auto_release(request: Request, body: AcknowledgeReleaseBody) -> dict:
    require_auth(request)
    from app.services.orders.procurement_release import ProcurementReleaseError, acknowledge_release

    try:
        return acknowledge_release(body.ord_line_no, note=body.note, operator=body.operator)
    except ProcurementReleaseError as exc:
        from app.config.demo_submit import (
            acknowledge_release_stub,
            is_demo_submit_always_success,
        )

        if is_demo_submit_always_success():
            return acknowledge_release_stub(body.ord_line_no)
        status = 404 if exc.code == "not_found" else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc


@router.get("/{ord_line_no}")
def get_order(
    ord_line_no: str,
    request: Request,
    refresh: bool = Query(False, description="true 时强制从 Admin 刷新"),
) -> dict:
    require_auth(request)
    detail = order_service.get_ord_line_detail(ord_line_no, refresh=refresh)
    if not detail:
        raise HTTPException(status_code=404, detail="订单不存在")
    if refresh and detail.get("row"):
        from app.services.orders import line_cache

        line_cache.merge_lines([detail["row"]])
    return detail


class TopupOrderContextBody(BaseModel):
    ord_line_no: str
    ord_no: Optional[str] = None
    pay_no: Optional[str] = None
    ds_ord_no: Optional[str] = None
    user_id: Optional[str] = None
    user_nickname: Optional[str] = None
    product_title: Optional[str] = None
    ti_status_label: Optional[str] = None
    to_status_label: Optional[str] = None
    customer_paid: float = 0
    purchase_payable: float = 0
    topup_amount: float = 0
    currency: Optional[str] = None


class TopupTranslateBody(BaseModel):
    source_text: str = Field(..., min_length=1)
    reason_key: str = Field(..., min_length=1)
    reason_label: str = Field(..., min_length=1)
    target_lang: str = Field(..., min_length=2, max_length=5)
    order_context: TopupOrderContextBody


@router.post("/topup/translate")
def topup_translate(body: TopupTranslateBody) -> dict:
    lang = body.target_lang.strip().lower()
    if lang not in TOPUP_LANG_META:
        raise HTTPException(status_code=400, detail=f"不支持的语言: {body.target_lang}")
    try:
        translated = translate_topup_message(
            source_text=body.source_text,
            reason_label=body.reason_label,
            target_lang=lang,
            order_context=body.order_context.model_dump(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc) or "翻译失败") from exc
    return {"translated": translated, "target_lang": lang}


class TopupSendBody(BaseModel):
    ord_line_no: str = Field(..., min_length=1)
    amount: float = Field(..., gt=0)
    reason_key: str = Field(..., min_length=1)
    reason_label: str = Field(..., min_length=1)
    message_zh: str = Field(..., min_length=1)
    message_localized: str = Field(..., min_length=1)
    target_lang: str = Field(..., min_length=2, max_length=5)
    order_context: TopupOrderContextBody


@router.post("/topup/send")
def topup_send(body: TopupSendBody) -> dict:
    """测试环境：模拟发送补款通知，后续接站内信 + 账单 WritePort。"""
    lang = body.target_lang.strip().lower()
    if lang not in TOPUP_LANG_META:
        raise HTTPException(status_code=400, detail=f"不支持的语言: {body.target_lang}")
    return {
        "ok": True,
        "message": f"补款通知已发送（¥{body.amount:.2f} · {body.reason_label}）",
    }

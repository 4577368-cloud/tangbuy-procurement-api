"""HTTP 路由 — 真实订单（Tangbuy Admin）。"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.services.orders import disposition as disposition_service
from app.services.orders import service as order_service
from app.services.orders.disposition import DispositionError
from app.services.orders.topup_message import TOPUP_LANG_META, translate_topup_message

router = APIRouter(prefix="/api/orders", tags=["orders"])


@router.get("")
def list_orders(
    queue: Optional[str] = Query(None, description="作业队列，如 pending_procurement"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    ord_no: Optional[str] = None,
) -> dict:
    return order_service.list_ord_lines(
        queue=queue,
        page=page,
        page_size=page_size,
        ord_no=ord_no,
    )


@router.get("/summary")
def orders_summary() -> dict:
    return order_service.queue_summary()


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
        status = 404 if exc.code == "not_found" else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc


@router.get("/{ord_line_no}")
def get_order(ord_line_no: str) -> dict:
    detail = order_service.get_ord_line_detail(ord_line_no)
    if not detail:
        raise HTTPException(status_code=404, detail="订单不存在")
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

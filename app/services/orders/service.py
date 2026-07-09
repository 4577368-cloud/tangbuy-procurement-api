"""订单读服务 — Tangbuy Admin OrdLineReadPort 实现。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

from app.core.config import get_settings
from app.integrations.tangbuy_admin.client import TangbuyAdminError, list_order_detail
from app.integrations.tangbuy_admin.mapper import flatten_admin_rows
from app.integrations.tangbuy_admin.token_store import resolve_admin_token
from app.services.orders import disposition_store
from app.services.orders.order_note_classify import enrich_row_note_fields
from app.services.orders.order_sku_check import enrich_row_sku_fields
from app.services.orders.purchase_cost import enrich_row_purchase_cost_fields
from app.services.orders.queue_filters import (
    _base_list_body,
    build_list_body,
    pending_procurement_admin_filters,
    resolve_order_queue,
)


def _strip_internal(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if not str(k).startswith("_")}


def _public_row(row: dict[str, Any]) -> dict[str, Any]:
    return _strip_internal(row)


def _enrich_row(row: dict[str, Any]) -> dict[str, Any]:
    return enrich_row_purchase_cost_fields(
        enrich_row_sku_fields(enrich_row_note_fields(row))
    )


def _fetch_admin_rows(body: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    data = list_order_detail(body)
    rows = flatten_admin_rows(data.get("rows") if isinstance(data.get("rows"), list) else [])
    total = int(data.get("total") or len(rows))
    return rows, total


def _fetch_pending_procurement_rows(
    *,
    page: int,
    page_size: int,
    storage_no: int,
    event_type_pending: int,
) -> tuple[list[dict[str, Any]], int]:
    """合并 Admin 多状态码的待采购子单。"""
    merged: dict[str, dict[str, Any]] = {}
    total_hint = 0
    for filt in pending_procurement_admin_filters(event_type_pending):
        body = _base_list_body(page=page, page_size=page_size, storage_no=storage_no)
        body.update(filt)
        rows, bucket_total = _fetch_admin_rows(body)
        total_hint += bucket_total
        for row in rows:
            key = str(row.get("ord_line_no") or "")
            if key:
                merged[key] = row

    lines = sorted(
        merged.values(),
        key=lambda r: str(r.get("pay_time") or ""),
        reverse=True,
    )
    start = (max(page, 1) - 1) * page_size
    page_lines = lines[start : start + page_size]
    return page_lines, max(len(lines), total_hint)


def _pending_procurement_summary_total(storage_no: int, event_type_pending: int) -> int:
    total = 0
    for filt in pending_procurement_admin_filters(event_type_pending):
        body = _base_list_body(page=1, page_size=1, storage_no=storage_no)
        body.update(filt)
        try:
            _, bucket_total = _fetch_admin_rows(body)
            total += bucket_total
        except TangbuyAdminError:
            continue
    return total


def list_ord_lines(
    *,
    queue: Optional[str] = None,
    page: int = 1,
    page_size: int = 50,
    ord_line_no: Optional[str] = None,
    ord_no: Optional[str] = None,
) -> dict[str, Any]:
    settings = get_settings()
    token = resolve_admin_token().strip()
    if not token or token == "your-admin-bearer-token":
        return {
            "items": [],
            "total": 0,
            "page": page,
            "page_size": page_size,
            "source": "unconfigured",
            "error": "未配置 TANGBUY_ADMIN_TOKEN（.env.local）",
        }

    if queue == "pending_procurement" and not ord_line_no and not ord_no:
        try:
            rows, admin_total = _fetch_pending_procurement_rows(
                page=page,
                page_size=page_size,
                storage_no=settings.tangbuy_admin_storage_no,
                event_type_pending=settings.tangbuy_admin_event_type_pending,
            )
        except TangbuyAdminError as exc:
            return {
                "items": [],
                "total": 0,
                "page": page,
                "page_size": page_size,
                "source": "tangbuy_admin",
                "error": str(exc),
            }
    else:
        body = build_list_body(
            page=page,
            page_size=page_size,
            storage_no=settings.tangbuy_admin_storage_no,
            queue=queue,
            ord_line_no=ord_line_no,
            ord_no=ord_no,
            event_type_pending=settings.tangbuy_admin_event_type_pending,
        )
        try:
            rows, admin_total = _fetch_admin_rows(body)
        except TangbuyAdminError as exc:
            return {
                "items": [],
                "total": 0,
                "page": page,
                "page_size": page_size,
                "source": "tangbuy_admin",
                "error": str(exc),
            }

    lines = [
        _enrich_row(disposition_store.apply_row_override(ln))
        for ln in rows
    ]

    if queue == "pending_payment" and not ord_line_no and not ord_no:
        existing = {str(ln.get("ord_line_no") or "") for ln in lines}
        for passed_no, override in disposition_store.list_overrides_for_queue(
            "pending_payment"
        ).items():
            if passed_no in existing:
                continue
            passed_row = get_ord_line(passed_no)
            if passed_row:
                lines.append(disposition_store.apply_row_override({**passed_row, **override}))

    # 队列二次过滤（Admin 单码筛选可能漏/多）
    if queue and queue != "all" and not ord_line_no and not ord_no:
        lines = [ln for ln in lines if resolve_order_queue(ln) == queue]

    if queue == "pending_payment":
        total = max(admin_total, len(lines))
    else:
        total = admin_total
    return {
        "items": [_public_row(ln) for ln in lines],
        "total": total,
        "page": page,
        "page_size": page_size,
        "source": "tangbuy_admin",
    }


def get_ord_line(ord_line_no: str) -> Optional[dict[str, Any]]:
    result = list_ord_lines(ord_line_no=ord_line_no.strip(), page=1, page_size=1)
    items = result.get("items") or []
    return items[0] if items else None


def get_ord_line_detail(ord_line_no: str) -> Optional[dict[str, Any]]:
    settings = get_settings()
    if not settings.tangbuy_admin_configured:
        return None

    body = build_list_body(
        page=1,
        page_size=1,
        storage_no=settings.tangbuy_admin_storage_no,
        ord_line_no=ord_line_no.strip(),
    )
    try:
        data = list_order_detail(body)
    except TangbuyAdminError:
        return None

    rows = data.get("rows") if isinstance(data.get("rows"), list) else []
    lines = flatten_admin_rows(rows)
    if not lines:
        return None

    row = _enrich_row(disposition_store.apply_row_override(lines[0]))
    return {
        "row": _public_row(row),
        "timeline": row.get("_timeline") or [],
    }


def queue_summary() -> dict[str, Any]:
    settings = get_settings()
    if not settings.tangbuy_admin_configured:
        return {
            "counts": {},
            "source": "unconfigured",
            "error": "未配置 TANGBUY_ADMIN_TOKEN（.env.local）",
        }

    queues = [
        "pending_procurement",
        "pending_payment",
        "ordered",
        "shipped",
        "in_warehouse",
        "dispatched",
        "exception",
        "reverse",
    ]

    def _count_one(q: str) -> tuple[str, int]:
        if q == "pending_procurement":
            try:
                return q, _pending_procurement_summary_total(
                    settings.tangbuy_admin_storage_no,
                    settings.tangbuy_admin_event_type_pending,
                )
            except TangbuyAdminError:
                return q, 0
        body = build_list_body(
            page=1,
            page_size=1,
            storage_no=settings.tangbuy_admin_storage_no,
            queue=q,
            event_type_pending=settings.tangbuy_admin_event_type_pending,
        )
        try:
            data = list_order_detail(body)
            return q, int(data.get("total") or 0)
        except TangbuyAdminError:
            return q, 0

    counts: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=len(queues)) as pool:
        futures = [pool.submit(_count_one, q) for q in queues]
        for fut in as_completed(futures):
            q, n = fut.result()
            counts[q] = n

    counts["all"] = sum(counts[q] for q in queues)
    for key, delta in disposition_store.summary_adjustments().items():
        if key in counts:
            counts[key] = max(0, counts[key] + delta)
    counts["all"] = sum(counts[q] for q in queues)
    return {"counts": counts, "source": "tangbuy_admin"}

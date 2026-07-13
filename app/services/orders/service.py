"""订单读服务 — Tangbuy Admin OrdLineReadPort 实现。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Optional

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
    from app.services.orders.pipeline_store import enrich_row_pipeline_fields
    from app.services.orders.platform_order_enrich import enrich_row_platform_order_fields
    from app.services.orders.product_category_enrich import enrich_row_mapped_category

    return enrich_row_pipeline_fields(
        enrich_row_mapped_category(
            enrich_row_platform_order_fields(
                enrich_row_purchase_cost_fields(
                    enrich_row_sku_fields(enrich_row_note_fields(row))
                )
            )
        )
    )


_OVERRIDE_SKIP_KEYS = ("queue_override", "passed_at", "action_key", "signal_type")


def _apply_override(
    row: dict[str, Any], overrides: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """用预载覆盖表打补丁，避免逐行 disposition_store.get_override（每行一次会话）。"""
    key = str(row.get("ord_line_no") or "").strip()
    if not key:
        return row
    override = overrides.get(key)
    if not override:
        return row
    merged = {**row}
    for k, v in override.items():
        if k in _OVERRIDE_SKIP_KEYS:
            continue
        merged[k] = v
    return merged


def _enrich_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """批量 enrich：商品索引 / pipeline 状态只加载一次，列表不调备注 LLM。

    替代逐行 _enrich_row（每行多次 DB 会话），供列表分页页使用。
    """
    from app.services.orders.pipeline_store import (
        _pipeline_state_map,
        enrich_row_pipeline_fields,
    )
    from app.services.orders.platform_order_enrich import enrich_row_platform_order_fields
    from app.services.orders.product_category_enrich import enrich_row_mapped_category
    from app.services.products.store import build_ord_line_product_index

    states = _pipeline_state_map()
    product_index = build_ord_line_product_index()
    out: list[dict[str, Any]] = []
    for row in rows:
        enriched = enrich_row_platform_order_fields(
            enrich_row_purchase_cost_fields(
                enrich_row_sku_fields(enrich_row_note_fields(row, allow_llm=False))
            )
        )
        enriched = enrich_row_mapped_category(enriched, product_index=product_index)
        enriched = enrich_row_pipeline_fields(enriched, states=states)
        out.append(enriched)
    return out


def _fetch_admin_rows(body: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    import time

    last_exc: TangbuyAdminError | None = None
    for attempt in range(3):
        try:
            data = list_order_detail(body)
            rows = flatten_admin_rows(data.get("rows") if isinstance(data.get("rows"), list) else [])
            total = int(data.get("total") or len(rows))
            return rows, total
        except TangbuyAdminError as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(0.4 * (attempt + 1))
    assert last_exc is not None
    raise last_exc


def _fetch_pending_procurement_rows(
    *,
    page: int,
    page_size: int,
    storage_no: int,
    event_type_pending: int,
) -> tuple[list[dict[str, Any]], int]:
    """合并 Admin 多状态码的待采购子单（并发拉取各桶）。"""
    filters = pending_procurement_admin_filters(event_type_pending)
    merged: dict[str, dict[str, Any]] = {}
    total_hint = 0

    if len(filters) <= 1:
        for filt in filters:
            body = _base_list_body(page=page, page_size=page_size, storage_no=storage_no)
            body.update(filt)
            rows, bucket_total = _fetch_admin_rows(body)
            total_hint += bucket_total
            for row in rows:
                key = str(row.get("ord_line_no") or "")
                if key:
                    merged[key] = row
    else:
        def _fetch_one(filt):
            body = _base_list_body(page=page, page_size=page_size, storage_no=storage_no)
            body.update(filt)
            return _fetch_admin_rows(body)

        with ThreadPoolExecutor(max_workers=len(filters)) as pool:
            futures = {pool.submit(_fetch_one, filt): filt for filt in filters}
            for fut in as_completed(futures):
                try:
                    rows, bucket_total = fut.result()
                    total_hint += bucket_total
                    for row in rows:
                        key = str(row.get("ord_line_no") or "")
                        if key:
                            merged[key] = row
                except TangbuyAdminError:
                    continue

    lines = sorted(
        merged.values(),
        key=lambda r: str(r.get("pay_time") or ""),
        reverse=True,
    )
    start = (max(page, 1) - 1) * page_size
    page_lines = lines[start : start + page_size]
    return page_lines, max(len(lines), total_hint)


def fetch_pending_procurement_bucket(
    *,
    filter_index: int,
    page: int,
    page_size: int,
    storage_no: Optional[int] = None,
    event_type_pending: Optional[int] = None,
) -> tuple[list[dict[str, Any]], int]:
    """待采购单桶分页（回填用，避免多桶合并后页码错乱）。"""
    settings = get_settings()
    sn = storage_no if storage_no is not None else settings.tangbuy_admin_storage_no
    evt = event_type_pending if event_type_pending is not None else settings.tangbuy_admin_event_type_pending
    filters = pending_procurement_admin_filters(evt)
    if filter_index < 0 or filter_index >= len(filters):
        return [], 0
    body = _base_list_body(page=page, page_size=page_size, storage_no=sn)
    body.update(filters[filter_index])
    return _fetch_admin_rows(body)


def pending_procurement_filter_count(event_type_pending: Optional[int] = None) -> int:
    settings = get_settings()
    evt = event_type_pending if event_type_pending is not None else settings.tangbuy_admin_event_type_pending
    return len(pending_procurement_admin_filters(evt))


def _pending_procurement_summary_total(storage_no: int, event_type_pending: int) -> int:
    filters = pending_procurement_admin_filters(event_type_pending)

    def _count_one(filt):
        body = _base_list_body(page=1, page_size=1, storage_no=storage_no)
        body.update(filt)
        try:
            _, bucket_total = _fetch_admin_rows(body)
            return bucket_total
        except TangbuyAdminError:
            return 0

    if len(filters) <= 1:
        return sum(_count_one(f) for f in filters)

    with ThreadPoolExecutor(max_workers=len(filters)) as pool:
        futures = [pool.submit(_count_one, f) for f in filters]
        return sum(f.result() for f in as_completed(futures))


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


_SUMMARY_QUEUES = (
    "pending_procurement",
    "pending_payment",
    "ordered",
    "shipped",
    "in_warehouse",
    "dispatched",
    "exception",
    "reverse",
)


def list_cached_ord_lines(
    *,
    queue: Optional[str] = None,
    page: int = 1,
    page_size: int = 50,
    ord_line_no: Optional[str] = None,
    ord_no: Optional[str] = None,
) -> dict[str, Any]:
    """从本地快照分页列表（不访问 Admin）。"""
    from app.services.orders import line_cache

    q = None if not queue or queue == "all" else queue
    lines = line_cache.list_cached_lines(queue=q)

    if ord_line_no:
        key = ord_line_no.strip()
        lines = [ln for ln in lines if str(ln.get("ord_line_no") or "") == key]
    if ord_no:
        needle = ord_no.strip()
        lines = [ln for ln in lines if needle in str(ln.get("ord_no") or "")]

    # 先分页，只对当前页做重 enrich（商品索引 / pipeline / 备注）
    total = len(lines)
    start = (max(page, 1) - 1) * page_size
    page_lines = lines[start : start + page_size]

    overrides = disposition_store.load_all_overrides()
    page_lines = [_apply_override(ln, overrides) for ln in page_lines]
    enriched = [_public_row(row) for row in _enrich_rows(page_lines)]
    return {
        "items": enriched,
        "total": total,
        "page": page,
        "page_size": page_size,
        "source": "cache",
    }


def cached_queue_summary() -> dict[str, Any]:
    """按本地快照统计各队列数量（不访问 Admin）。"""
    from app.services.orders import line_cache

    counts: dict[str, int] = {q: 0 for q in _SUMMARY_QUEUES}
    for row in line_cache.load_all_lines().values():
        q = resolve_order_queue(row)
        if q in counts:
            counts[q] += 1
    for key, delta in disposition_store.summary_adjustments().items():
        if key in counts:
            counts[key] = max(0, counts[key] + delta)
    counts["all"] = sum(counts[q] for q in _SUMMARY_QUEUES)
    state = line_cache.load_sync_state()
    return {
        "counts": counts,
        "source": "cache",
        "cache_total": state.get("cached_total", counts["all"]),
        "sync_state": {
            "last_incremental_at": state.get("last_incremental_at"),
            "last_backfill_at": state.get("last_backfill_at"),
            "backfill_complete": state.get("backfill_complete"),
        },
    }


def get_ord_line(ord_line_no: str) -> Optional[dict[str, Any]]:
    from app.services.orders import line_cache

    key = ord_line_no.strip()
    if not key:
        return None
    cached = line_cache.get_line(key)
    if cached:
        row = _enrich_row(disposition_store.apply_row_override(cached))
        return _public_row(row)
    result = list_ord_lines(ord_line_no=key, page=1, page_size=1)
    items = result.get("items") or []
    return items[0] if items else None


def get_ord_line_detail(ord_line_no: str, *, refresh: bool = False) -> Optional[dict[str, Any]]:
    key = ord_line_no.strip()
    if not key:
        return None

    if not refresh:
        row = get_ord_line(key)
        if row:
            return {
                "row": row,
                "timeline": row.get("_timeline") or [],
                "source": "cache",
            }

    settings = get_settings()
    if not settings.tangbuy_admin_configured:
        return None

    body = build_list_body(
        page=1,
        page_size=1,
        storage_no=settings.tangbuy_admin_storage_no,
        ord_line_no=key,
    )
    try:
        data = list_order_detail(body)
    except TangbuyAdminError:
        cached = get_ord_line(key)
        if cached:
            return {"row": cached, "timeline": cached.get("_timeline") or [], "source": "cache"}
        return None

    rows = data.get("rows") if isinstance(data.get("rows"), list) else []
    lines = flatten_admin_rows(rows)
    if not lines:
        cached = get_ord_line(key)
        if cached:
            return {"row": cached, "timeline": cached.get("_timeline") or [], "source": "cache"}
        return None

    row = _enrich_row(disposition_store.apply_row_override(lines[0]))
    try:
        from app.services.orders.platform_order_sync import sync_platform_order_for_line

        ord_no = str(row.get("ord_no") or "").strip() or None
        synced = sync_platform_order_for_line(key, ord_no=ord_no, persist_cache=True)
        if synced:
            row = _enrich_row({**row, **synced})
    except Exception:
        pass
    return {
        "row": _public_row(row),
        "timeline": row.get("_timeline") or [],
        "source": "tangbuy_admin",
    }


def queue_summary() -> dict[str, Any]:
    settings = get_settings()
    if not settings.tangbuy_admin_configured:
        return {
            "counts": {},
            "source": "unconfigured",
            "error": "未配置 TANGBUY_ADMIN_TOKEN（.env.local）",
        }

    queues = list(_SUMMARY_QUEUES)

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
    auth_error: str | None = None
    with ThreadPoolExecutor(max_workers=len(queues)) as pool:
        futures = [pool.submit(_count_one, q) for q in queues]
        for fut in as_completed(futures):
            q, n = fut.result()
            counts[q] = n

    # 探测 Admin 认证：全 0 时可能是 Token 失效，避免与本地覆盖叠加后误导
    probe_body = build_list_body(
        page=1,
        page_size=1,
        storage_no=settings.tangbuy_admin_storage_no,
        queue="all",
        event_type_pending=settings.tangbuy_admin_event_type_pending,
    )
    try:
        list_order_detail(probe_body)
    except TangbuyAdminError as exc:
        if exc.status == 401 or "Token" in str(exc):
            auth_error = str(exc)

    if auth_error and sum(counts.get(q, 0) for q in queues) == 0:
        return {
            "counts": {},
            "source": "tangbuy_admin",
            "error": auth_error,
        }

    counts["all"] = sum(counts[q] for q in queues)
    for key, delta in disposition_store.summary_adjustments().items():
        if key in counts:
            counts[key] = max(0, counts[key] + delta)
    counts["all"] = sum(counts[q] for q in queues)
    return {"counts": counts, "source": "tangbuy_admin"}

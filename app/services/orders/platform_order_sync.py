"""1688 订单回传同步。"""

from __future__ import annotations

from typing import Any, Optional

from app.integrations.tangbuy_admin.alibaba_order_api import query_platform_orders
from app.integrations.tangbuy_admin.client import TangbuyAdminError
from app.integrations.tangbuy_admin.platform_order_mapper import index_platform_rows_by_line
from app.services.orders import line_cache, platform_order_store


class PlatformOrderSyncError(Exception):
    def __init__(self, message: str, *, code: str = "platform_sync_failed") -> None:
        super().__init__(message)
        self.code = code


def _extract_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    rows = result.get("rows")
    return rows if isinstance(rows, list) else []


def _find_line_in_rows(rows: list[dict[str, Any]], key: str) -> Optional[dict[str, Any]]:
    indexed = index_platform_rows_by_line(rows)
    return indexed.get(key)


def _query_for_line(
    *,
    ord_line_no: str,
    ord_no: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """按子单号 / 主单号查询订单回传。

    Admin query 的 itemNo/orderNo 过滤目前不生效，需在待支付列表分页扫描。
    """
    key = ord_line_no.strip()
    if not key:
        return None

    page_size = 50
    try:
        first = query_platform_orders(page_num=1, page_size=page_size, status=0)
    except TangbuyAdminError:
        return None

    hit = _find_line_in_rows(_extract_rows(first), key)
    if hit:
        return hit

    total = int(first.get("total") or 0)
    if total <= page_size:
        return None

    max_page = (total + page_size - 1) // page_size
    for page_num in range(2, max_page + 1):
        try:
            result = query_platform_orders(page_num=page_num, page_size=page_size, status=0)
        except TangbuyAdminError:
            break
        hit = _find_line_in_rows(_extract_rows(result), key)
        if hit:
            return hit
    return None


def sync_platform_order_for_line(
    ord_line_no: str,
    *,
    ord_no: Optional[str] = None,
    persist_cache: bool = True,
) -> Optional[dict[str, Any]]:
    """拉取单条子单的 1688 订单回传并落库。"""
    fields = _query_for_line(ord_line_no=ord_line_no, ord_no=ord_no)
    if not fields:
        return None
    platform_order_store.upsert_platform_orders({ord_line_no.strip(): fields})
    if persist_cache:
        _merge_into_line_cache(fields)
    return fields


def sync_platform_orders_for_lines(
    ord_line_nos: list[str],
    *,
    rows_by_line: Optional[dict[str, dict[str, Any]]] = None,
) -> dict[str, Any]:
    """批量同步订单回传。"""
    keys = [str(k).strip() for k in ord_line_nos if str(k).strip()]
    synced: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, str]] = []
    rows_by_line = rows_by_line or {}

    for key in keys:
        row = rows_by_line.get(key) or {}
        ord_no = str(row.get("ord_no") or "").strip() or None
        try:
            fields = sync_platform_order_for_line(key, ord_no=ord_no)
            if fields:
                synced[key] = fields
        except TangbuyAdminError as exc:
            errors.append({"ord_line_no": key, "error": str(exc)})

    return {
        "ok": bool(synced),
        "synced": len(synced),
        "items": synced,
        "errors": errors or None,
    }


def get_platform_order_view(ord_line_no: str) -> Optional[dict[str, Any]]:
    """读取已同步的 1688 订单回传（无则返回 None）。"""
    return platform_order_store.get_platform_order(ord_line_no.strip())


def _merge_into_line_cache(fields: dict[str, Any]) -> None:
    key = str(fields.get("ord_line_no") or "").strip()
    if not key:
        return
    prev = line_cache.get_line(key)
    if not prev:
        return
    merged = {**prev, **fields}
    line_cache.merge_lines([merged])

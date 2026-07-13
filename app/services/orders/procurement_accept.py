"""待接单自动确认（stat=0 → 处理中）。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from app.config.business_config import normalize_business_config
from app.config.store import get_business_config
from app.integrations.tangbuy_admin.client import TangbuyAdminError
from app.integrations.tangbuy_admin.order_accept_api import (
    MAX_CONFIRM_BATCH,
    confirm_order_list,
    list_order_pool,
)
from app.services.orders import disposition_store, line_cache
from app.services.orders.service import get_ord_line

_ACCEPTED_ORD_NOS: set[str] = set()


class ProcurementAcceptError(Exception):
    def __init__(self, message: str, *, code: str = "accept_failed") -> None:
        super().__init__(message)
        self.code = code


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_pool_rows(response: Any) -> list[dict[str, Any]]:
    if not isinstance(response, dict):
        return []
    data = response.get("data")
    if isinstance(data, dict):
        rows = data.get("rows")
        if isinstance(rows, list):
            return [r for r in rows if isinstance(r, dict)]
    rows = response.get("rows")
    return [r for r in rows] if isinstance(rows, list) else []


def _pool_order_no(row: dict[str, Any]) -> str:
    for field in ("orderNo", "ordNo", "order_no"):
        val = str(row.get(field) or "").strip()
        if val:
            return val
    return ""


def _audit_accept(ord_no: str, *, operator: Optional[str] = None) -> None:
    lines = [
        ln
        for ln in line_cache.load_all_lines().values()
        if str(ln.get("ord_no") or "").strip() == ord_no
    ]
    targets = [str(ln.get("ord_line_no") or "").strip() for ln in lines if ln.get("ord_line_no")]
    if not targets:
        targets = [ord_no]
    for key in targets:
        disposition_store.append_audit(
            {
                "ord_line_no": key,
                "ord_no": ord_no,
                "action_key": "accept_order",
                "action_label": "自动接单",
                "stage_before": "pending_procurement",
                "stage_after": "pending_procurement",
                "admin_write": "ok",
                "operator": operator,
                "trigger": "auto_accept",
                "release_type": "order_accept",
                "ord_line_stat_before": 0,
                "ord_line_stat_after": 23,
                "at": _now_iso(),
            }
        )


def accept_orders_batch(order_nos: list[str], *, operator: Optional[str] = None) -> dict[str, Any]:
    cleaned = [str(n).strip() for n in order_nos if str(n).strip()]
    if not cleaned:
        return {"ok": True, "accepted": [], "skipped": []}

    accepted: list[str] = []
    errors: list[dict[str, str]] = []
    for i in range(0, len(cleaned), MAX_CONFIRM_BATCH):
        chunk = cleaned[i : i + MAX_CONFIRM_BATCH]
        try:
            confirm_order_list(chunk)
            accepted.extend(chunk)
            for ord_no in chunk:
                _ACCEPTED_ORD_NOS.add(ord_no)
                _audit_accept(ord_no, operator=operator)
        except TangbuyAdminError as exc:
            errors.append({"order_nos": ",".join(chunk), "error": str(exc)})

    if errors and not accepted:
        raise ProcurementAcceptError(errors[0]["error"], code="admin_write_failed")
    return {"ok": True, "accepted": accepted, "errors": errors or None}


def scan_and_accept_pool(*, operator: Optional[str] = None) -> dict[str, Any]:
    cfg = normalize_business_config(get_business_config())
    if not cfg.get("auto_accept_orders_enabled", True):
        return {"ok": True, "accepted": [], "skipped": [], "reason": "disabled"}

    try:
        response = list_order_pool(page_size=200)
    except TangbuyAdminError as exc:
        raise ProcurementAcceptError(f"接单池查询失败：{exc}", code="admin_read_failed") from exc

    order_nos: list[str] = []
    for row in _extract_pool_rows(response):
        ord_no = _pool_order_no(row)
        if ord_no and ord_no not in _ACCEPTED_ORD_NOS:
            order_nos.append(ord_no)

    if not order_nos:
        return {"ok": True, "accepted": [], "candidates": 0}

    result = accept_orders_batch(order_nos, operator=operator or "system")
    return {**result, "candidates": len(order_nos)}


def accept_order_for_line(ord_line_no: str, *, operator: Optional[str] = None) -> dict[str, Any]:
    row = get_ord_line(ord_line_no)
    if not row:
        raise ProcurementAcceptError(f"子单不存在：{ord_line_no}", code="not_found")
    stat = row.get("ord_line_stat")
    try:
        stat_int = int(stat) if stat is not None else None
    except (TypeError, ValueError):
        stat_int = None
    if stat_int != 0:
        return {"ok": True, "code": "skip", "reason": "not_stat_0"}

    ord_no = str(row.get("ord_no") or "").strip()
    if not ord_no:
        raise ProcurementAcceptError("缺少主单号 ord_no", code="missing_ord_no")
    if ord_no in _ACCEPTED_ORD_NOS:
        return {"ok": True, "code": "already_accepted", "ord_no": ord_no}

    return accept_orders_batch([ord_no], operator=operator)


def list_accept_audits(*, limit: int = 200) -> list[dict[str, Any]]:
    """将接单审计转为 AgentReleaseMonitor 可展示结构。"""
    rows = disposition_store.list_audits(action_key="accept_order", limit=limit)
    out: list[dict[str, Any]] = []
    for item in rows:
        key = str(item.get("ord_line_no") or "").strip()
        if not key:
            continue
        out.append(
            {
                "release_id": f"accept-{key}-{item.get('at', '')}",
                "ord_line_no": key,
                "order_id": key,
                "external_order_no": str(item.get("ord_no") or ""),
                "product_title": "",
                "release_type": "order_accept",
                "agent_label": "采购流水线",
                "stage_before": "pending_procurement",
                "stage_after": "pending_procurement",
                "released_at": item.get("at") or _now_iso(),
                "conditions": [
                    {
                        "key": "accept",
                        "label": "待接单 → 处理中",
                        "passed": True,
                        "detail": "confirmList",
                    }
                ],
                "summary": "自动接单",
                "review_status": "confirmed",
                "auto_confirmed": True,
            }
        )
    return out

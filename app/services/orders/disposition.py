"""指挥中心处置写回 — manual_confirm / procurement_pass。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from app.core.config import get_settings
from app.integrations.tangbuy_admin.client import TangbuyAdminError, admin_post
from app.services.orders import disposition_store
from app.services.orders.queue_filters import resolve_order_queue
from app.services.orders.service import get_ord_line


class DispositionError(Exception):
    def __init__(self, message: str, *, code: str = "disposition_failed") -> None:
        super().__init__(message)
        self.code = code


def _try_admin_procurement_pass(row: dict[str, Any]) -> str:
    settings = get_settings()
    path = (settings.tangbuy_admin_procurement_pass_path or "").strip()
    if not path:
        return "skipped"

    body = {
        "itemNo": row.get("ord_line_no"),
        "orderNo": row.get("ord_no"),
        "storageNo": row.get("wh_id") or settings.tangbuy_admin_storage_no,
    }
    try:
        admin_post(path, body)
        return "ok"
    except TangbuyAdminError as exc:
        raise DispositionError(f"Admin 推进失败：{exc}", code="admin_write_failed") from exc


def submit_disposition(
    *,
    ord_line_no: str,
    action_key: str,
    action_label: str,
    signal_type: Optional[str] = None,
    stage: Optional[str] = None,
    feedback_type: Optional[str] = None,
    override_reason: Optional[str] = None,
    operator: Optional[str] = None,
) -> dict[str, Any]:
    key = ord_line_no.strip()
    if not key:
        raise DispositionError("缺少子单号 ord_line_no")

    row = get_ord_line(key)
    if not row:
        raise DispositionError(f"子单不存在：{key}", code="not_found")

    queue = resolve_order_queue(row) or "pending_procurement"
    effective_stage = stage or queue

    if action_key == "change_seller":
        raise DispositionError(
            "换供请调用 POST /api/products/switch-supplier",
            code="use_switch_supplier",
        )

    if action_key != "manual_confirm":
        raise DispositionError(f"暂不支持动作：{action_key}", code="unsupported_action")

    if effective_stage != "pending_procurement" and queue != "pending_procurement":
        raise DispositionError("仅待下单子单可放行", code="invalid_stage")

    admin_result = _try_admin_procurement_pass(row)
    now = datetime.now(timezone.utc).isoformat()

    disposition_store.set_procurement_passed(
        key,
        ord_no=row.get("ord_no"),
        action_key=action_key,
        signal_type=signal_type,
        operator=operator,
        note=override_reason or action_label,
    )

    disposition_store.append_audit(
        {
            "ord_line_no": key,
            "ord_no": row.get("ord_no"),
            "action_key": action_key,
            "action_label": action_label,
            "signal_type": signal_type,
            "stage_before": "pending_procurement",
            "stage_after": "pending_payment",
            "feedback_type": feedback_type,
            "override_reason": override_reason,
            "operator": operator,
            "admin_write": admin_result,
            "at": now,
        }
    )

    return {
        "ok": True,
        "ord_line_no": key,
        "stage_before": "pending_procurement",
        "stage_after": "pending_payment",
        "admin_write": admin_result,
    }

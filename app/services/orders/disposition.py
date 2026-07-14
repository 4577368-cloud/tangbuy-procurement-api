"""指挥中心处置写回 — manual_confirm / generate_1688_order。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from app.core.config import get_settings
from app.integrations.tangbuy_admin.client import TangbuyAdminError, admin_post
from app.services.orders import disposition_store
from app.services.orders.procurement_release import (
    ProcurementReleaseError,
    submit_1688_pre_purchase,
)
from app.services.orders.procurement_place_order import (
    ProcurementPlaceOrderError,
    submit_1688_place_order,
)
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


def _note_clear_and_resume(
    key: str,
    row: dict[str, Any],
    *,
    action_key: str,
    action_label: str,
    operator: Optional[str],
    signal_type: Optional[str],
    feedback_type: Optional[str],
    verify: Optional[dict[str, Any]],
) -> dict[str, Any]:
    from app.services.orders import pipeline_store
    from app.services.orders.procurement_pipeline import resume_pipeline

    pipeline_store.ack_blocker(key, "NOTE_HANDLED", operator=operator)
    pipeline_store.ack_blocker(key, "NOTE_BLOCK", operator=operator)
    now = datetime.now(timezone.utc).isoformat()
    disposition_store.append_audit(
        {
            "ord_line_no": key,
            "ord_no": row.get("ord_no"),
            "action_key": action_key,
            "action_label": action_label,
            "signal_type": signal_type,
            "feedback_type": feedback_type,
            "operator": operator,
            "admin_write": "ok",
            "verify": verify,
            "at": now,
        }
    )
    try:
        result = resume_pipeline(key, operator=operator)
    except Exception as exc:
        raise DispositionError(str(exc), code="pipeline_failed") from exc
    state = result.get("state") or {}
    return {
        "ok": bool(result.get("ok")),
        "ord_line_no": key,
        "action_key": action_key,
        "pipeline_step": state.get("pipeline_step"),
        "blockers": state.get("blockers"),
        "ord_line_stat_after": state.get("ord_line_stat") or result.get("ord_line_stat"),
        "verify": verify,
        "state": state,
    }


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
    spec_before: Optional[str] = None,
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

    if action_key == "generate_1688_order":
        try:
            result = submit_1688_pre_purchase(
                key,
                operator=operator,
                trigger="disposition",
                force=True,
            )
        except ProcurementReleaseError as exc:
            raise DispositionError(str(exc), code=exc.code) from exc
        release = result.get("release") or {}
        return {
            "ok": True,
            "ord_line_no": key,
            "action_key": action_key,
            "stage_before": "pending_procurement",
            "stage_after": release.get("stage_after") or "pending_procurement",
            "admin_write": result.get("admin_write", "ok"),
            "ord_line_stat_before": result.get("ord_line_stat_before"),
            "ord_line_stat_after": result.get("ord_line_stat_after"),
            "auto_confirmed": result.get("auto_confirmed"),
            "release_id": release.get("release_id"),
        }

    if action_key == "place_1688_order":
        try:
            result = submit_1688_place_order(
                [key],
                operator=operator,
                trigger="disposition",
                merge_same_store=True,
            )
        except ProcurementPlaceOrderError as exc:
            raise DispositionError(str(exc), code=exc.code) from exc
        batches = result.get("batches") or []
        first_release = (batches[0] or {}).get("release") if batches else {}
        return {
            "ok": True,
            "ord_line_no": key,
            "action_key": action_key,
            "stage_before": "pending_procurement",
            "stage_after": "pending_payment",
            "admin_write": (batches[0] or {}).get("admin_write", "ok") if batches else "ok",
            "ord_line_stat_before": 54,
            "ord_line_stat_after": result.get("ord_line_stat_after"),
            "release_id": first_release.get("release_id"),
            "batches": batches,
        }

    if action_key == "ack_blocker":
        from app.services.orders.procurement_pipeline import ack_blocker_and_resume

        blocker_key = (override_reason or action_label or "").strip()
        if not blocker_key:
            raise DispositionError("缺少 blocker_key", code="missing_blocker_key")
        try:
            result = ack_blocker_and_resume(key, blocker_key, operator=operator)
        except Exception as exc:
            raise DispositionError(str(exc), code="ack_failed") from exc
        state = result.get("state") or {}
        return {
            "ok": bool(result.get("ok")),
            "ord_line_no": key,
            "action_key": action_key,
            "blocker_key": blocker_key,
            "pipeline_step": state.get("pipeline_step"),
            "blockers": state.get("blockers"),
            "ord_line_stat_after": state.get("ord_line_stat"),
        }

    if action_key == "resume_pipeline":
        from app.services.orders.procurement_pipeline import resume_pipeline

        try:
            result = resume_pipeline(key, operator=operator)
        except Exception as exc:
            raise DispositionError(str(exc), code="pipeline_failed") from exc
        state = result.get("state") or {}
        return {
            "ok": bool(result.get("ok")),
            "ord_line_no": key,
            "action_key": action_key,
            "pipeline_step": state.get("pipeline_step"),
            "blockers": state.get("blockers"),
            "ord_line_stat_after": state.get("ord_line_stat"),
        }

    if action_key == "note_defer_1688":
        return _note_clear_and_resume(
            key,
            row,
            action_key=action_key,
            action_label=action_label or "后续 1688 改规格",
            operator=operator,
            signal_type=signal_type,
            feedback_type=feedback_type,
            verify=None,
        )

    if action_key == "urge_ship":
        from app.services.agent.followup import (
            DEFAULT_QUESTION,
            execute_order_followup_send,
            normalize_followup_order_id,
        )

        platform_oid = normalize_followup_order_id(
            str(row.get("pur_no") or row.get("plt_ord_id") or "").strip()
        )
        if not platform_oid:
            raise DispositionError("缺少 1688 订单号，无法催单", code="missing_platform_order")

        question = (action_label or "").strip()
        if len(question) < 4 or question in ("催发货", "催单", "联系卖家催发货"):
            question = DEFAULT_QUESTION

        sent = execute_order_followup_send(platform_oid, question)
        if not sent.get("success"):
            raise DispositionError(
                str(sent.get("error") or "催单失败"),
                code="urge_ship_failed",
            )

        now = datetime.now(timezone.utc).isoformat()
        disposition_store.merge_override(
            key,
            {
                "ship_urged_at": now,
                "ship_urge_task_id": sent.get("taskId"),
                "ship_urge_platform_oid": platform_oid,
                "action_key": "urge_ship",
                "signal_type": signal_type,
                "operator": operator,
                "ord_no": row.get("ord_no"),
            },
        )
        disposition_store.append_audit(
            {
                "ord_line_no": key,
                "ord_no": row.get("ord_no"),
                "action_key": action_key,
                "action_label": action_label or "催发货",
                "signal_type": signal_type,
                "feedback_type": feedback_type,
                "operator": operator,
                "admin_write": "urge_ship",
                "platform_order_id": platform_oid,
                "task_id": sent.get("taskId"),
                "at": now,
            }
        )
        try:
            from app.services.command_center.scan_cache import invalidate_command_center_scan

            invalidate_command_center_scan()
        except Exception:
            pass
        return {
            "ok": True,
            "ord_line_no": key,
            "stage_before": effective_stage or queue,
            "stage_after": effective_stage or queue,
            "admin_write": "urge_ship",
            "action_key": action_key,
            "task_id": sent.get("taskId"),
            "platform_order_id": platform_oid,
            "code": "urged",
        }

    if action_key == "note_spec_modified":
        from app.services.orders import order_line_sync, pipeline_store
        from app.services.orders.note_spec_verify import verify_note_spec_alignment
        from app.services.orders.service import get_ord_line as reload_line

        try:
            order_line_sync.refresh_ord_lines([key])
        except Exception as exc:
            raise DispositionError(f"刷新子单失败：{exc}", code="refresh_failed") from exc
        refreshed = reload_line(key) or row
        verify = verify_note_spec_alignment(
            refreshed,
            allow_llm=True,
            spec_before=spec_before,
        )
        if not verify.aligned:
            now = datetime.now(timezone.utc).isoformat()
            code = "note_spec_unchanged" if verify.changed is False else "note_spec_mismatch"
            blockers = [
                {
                    "key": "NOTE_BLOCK",
                    "label": "备注待核",
                    "stage": "prepare",
                    "auto_resolvable": False,
                    "requires_ack": True,
                    "detail": verify.mismatch_summary,
                    "expected_specs": verify.expected_specs,
                    "actual_specs": verify.actual_specs,
                    "spec_before": verify.spec_before or None,
                    "changed": verify.changed,
                    "at": now,
                }
            ]
            state = pipeline_store.save_pipeline_state(
                {
                    "ord_line_no": key,
                    "pipeline_step": "blocked",
                    "ord_line_stat": refreshed.get("ord_line_stat"),
                    "blockers": blockers,
                    "last_run_at": now,
                    "last_error": verify.mismatch_summary,
                }
            )
            disposition_store.append_audit(
                {
                    "ord_line_no": key,
                    "ord_no": refreshed.get("ord_no"),
                    "action_key": action_key,
                    "action_label": action_label or "已修改",
                    "signal_type": signal_type,
                    "feedback_type": feedback_type,
                    "operator": operator,
                    "admin_write": "verify_failed",
                    "error": verify.mismatch_summary,
                    "verify": verify.to_dict(),
                    "at": now,
                }
            )
            raise DispositionError(verify.mismatch_summary, code=code)
        return _note_clear_and_resume(
            key,
            refreshed,
            action_key=action_key,
            action_label=action_label or "已修改",
            operator=operator,
            signal_type=signal_type,
            feedback_type=feedback_type,
            verify=verify.to_dict(),
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

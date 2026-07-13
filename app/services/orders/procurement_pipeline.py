"""采购履约流水线编排：接单 → 准备 → 预订购 → 下单。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from app.config.business_config import normalize_business_config
from app.config.store import get_business_config
from app.services.orders import line_cache, pipeline_store
from app.services.orders.procurement_accept import (
    ProcurementAcceptError,
    accept_order_for_line,
    scan_and_accept_pool,
)
from app.services.orders.procurement_place_order import (
    ProcurementPlaceOrderError,
    is_1688_place_order_eligible,
    submit_1688_place_order,
)
from app.services.orders.procurement_release import (
    ProcurementReleaseError,
    evaluate_prepare_stage,
    submit_1688_pre_purchase,
)
from app.services.orders.queue_filters import resolve_order_queue
from app.services.orders.service import get_ord_line
from app.services.orders.pipeline_blocker_copy import summarize_admin_blocker_detail
from app.services.products.store import find_product_for_ord_line

PipelineTrigger = Literal["sync", "manual", "ack", "category", "switch_supplier"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _int_stat(row: dict[str, Any]) -> Optional[int]:
    raw = row.get("ord_line_stat")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def parse_admin_error_blockers(message: str, *, stage: str = "place_order") -> list[dict[str, Any]]:
    msg = str(message or "").strip()
    if not msg:
        return []
    lower = msg.lower()
    blockers: list[dict[str, Any]] = []
    now = _now_iso()

    def add(key: str, label: str, **kwargs: Any) -> None:
        detail = kwargs.get("detail")
        if detail is None:
            detail = summarize_admin_blocker_detail(key, label, msg)
        blockers.append(
            {
                "key": key,
                "label": label,
                "stage": stage,
                "auto_resolvable": kwargs.get("auto_resolvable", False),
                "requires_ack": kwargs.get("requires_ack", False),
                "detail": detail,
                "raw_detail": msg,
                "at": now,
            }
        )

    if any(k in msg for k in ("库存", "缺货", "stock")) or "out of stock" in lower:
        add("ADMIN_STOCK", "库存不足", requires_ack=True)
    if any(k in msg for k in ("起订", "起批量", "MOQ", "moq", "最小起")):
        add("ADMIN_MOQ", "起批量不满足", requires_ack=True)
    if any(k in msg for k in ("规格", "SKU", "sku", "属性")):
        add("ADMIN_SKU", "规格不符", requires_ack=True)
    if any(k in msg for k in ("毛利", "价格", "金额", "运费", "差价")):
        add("ADMIN_MARGIN", "价格/毛利异常", requires_ack=True)
    if not blockers:
        add("ADMIN_ERROR", "平台下单失败", requires_ack=True)
    return blockers


def _try_auto_category_map(row: dict[str, Any]) -> dict[str, Any]:
    cfg = normalize_business_config(get_business_config())
    if not cfg.get("rules", {}).get("auto_category_mapping", True):
        return row
    category = str(row.get("lvl1_ctgy_nm") or "").strip()
    from app.services.orders.procurement_release import GENERIC_CATEGORIES

    if category and category not in GENERIC_CATEGORIES:
        return row
    product = find_product_for_ord_line(row)
    if not product:
        return row
    pid = str(product.get("tangbuy_product_id") or product.get("id") or "").strip()
    if not pid:
        return row
    try:
        from app.services.products.service import map_product_category_by_id
        from app.services.orders import order_line_sync

        map_product_category_by_id(pid, ord_row=row)
        key = str(row.get("ord_line_no") or "").strip()
        if key:
            order_line_sync.refresh_ord_lines([key])
            return get_ord_line(key) or row
    except Exception:
        pass
    return row


def _save_state(
    ord_line_no: str,
    *,
    pipeline_step: str,
    ord_line_stat: Optional[int],
    blockers: list[dict[str, Any]],
    last_error: Optional[str] = None,
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    state = {
        "ord_line_no": ord_line_no,
        "pipeline_step": pipeline_step if blockers else pipeline_step,
        "ord_line_stat": ord_line_stat,
        "blockers": blockers,
        "last_run_at": _now_iso(),
        "last_error": last_error,
        **(extra or {}),
    }
    if blockers:
        state["pipeline_step"] = "blocked"
    return pipeline_store.save_pipeline_state(state)


def _step_payment_monitor(row: dict[str, Any]) -> dict[str, Any]:
    """待支付：负毛利补款监控；付款自动化见 procurement_payment（二期）。"""
    from app.services.orders.procurement_payment import evaluate_payment_gap

    gap_info = evaluate_payment_gap(row)
    blockers: list[dict[str, Any]] = []
    if gap_info.get("needs_topup"):
        blockers.append(
            {
                "key": "PAY_AMOUNT_GAP",
                "label": "待补款",
                "stage": "payment",
                "auto_resolvable": False,
                "requires_ack": True,
                "detail": (
                    f"实付 ¥{gap_info['customer_paid']:.2f} "
                    f"< 应付 ¥{gap_info['purchase_payable']:.2f}"
                ),
                "at": _now_iso(),
            }
        )
    key = str(row.get("ord_line_no") or "")
    if blockers:
        return {"ok": False, "step": "payment", "blockers": blockers, "state": _save_state(
            key, pipeline_step="payment", ord_line_stat=_int_stat(row), blockers=blockers
        )}
    return {
        "ok": True,
        "step": "payment",
        "state": _save_state(key, pipeline_step="payment", ord_line_stat=_int_stat(row), blockers=[]),
    }


def _step_followup_monitor(row: dict[str, Any]) -> dict[str, Any]:
    """已订购：退换货 / 超期不发货监控。"""
    from app.services.orders.exception_rules import classify_exception_reason

    key = str(row.get("ord_line_no") or "")
    blockers: list[dict[str, Any]] = []
    reason = classify_exception_reason(row)
    if reason:
        band, label = reason
        blockers.append(
            {
                "key": "ORDER_EXCEPTION",
                "label": label,
                "stage": "followup",
                "auto_resolvable": band != "action",
                "requires_ack": band == "action",
                "detail": label,
                "at": _now_iso(),
            }
        )
    cfg = normalize_business_config(get_business_config())
    if cfg.get("rules", {}).get("auto_order_followup") and _int_stat(row) == 22:
        pur_time = str(row.get("pur_time") or "").strip()
        if pur_time:
            try:
                from datetime import timedelta

                pt = datetime.fromisoformat(pur_time.replace("Z", "+00:00"))
                hours = (datetime.now(timezone.utc) - pt).total_seconds() / 3600.0
                limit = float(cfg.get("unshipped_timeout_hours") or 48)
                if hours > limit:
                    blockers.append(
                        {
                            "key": "SHIP_OVERDUE",
                            "label": "超期未发货",
                            "stage": "followup",
                            "auto_resolvable": True,
                            "requires_ack": False,
                            "detail": f"已订购 {hours:.0f}h 未发货",
                            "at": _now_iso(),
                        }
                    )
                    _try_auto_followup_task(row)
            except Exception:
                pass
    if blockers:
        return {
            "ok": False,
            "step": "followup",
            "blockers": blockers,
            "state": _save_state(
                key, pipeline_step="followup", ord_line_stat=_int_stat(row), blockers=blockers
            ),
        }
    return {
        "ok": True,
        "step": "followup",
        "state": _save_state(key, pipeline_step="done", ord_line_stat=_int_stat(row), blockers=[]),
    }


def _try_auto_followup_task(row: dict[str, Any]) -> None:
    """超期未发货时自动发起催单（需 rules.auto_order_followup）。"""
    from app.services.agent.followup import execute_order_followup_send, normalize_followup_order_id

    for field in ("pur_no", "out_ord_no", "plugin_order_no"):
        raw = str(row.get(field) or "").strip()
        order_id = normalize_followup_order_id(raw) if raw else None
        if order_id:
            try:
                execute_order_followup_send(order_id, "请尽快发货，并告知预计发货时间")
            except Exception:
                pass
            return


def run_pipeline_for_line(
    ord_line_no: str,
    *,
    trigger: PipelineTrigger = "sync",
    operator: Optional[str] = None,
) -> dict[str, Any]:
    key = ord_line_no.strip()
    if not key:
        raise ValueError("ord_line_no required")

    row = get_ord_line(key)
    if not row:
        return {"ok": False, "code": "not_found", "ord_line_no": key}

    stat = _int_stat(row)
    results: list[dict[str, Any]] = []

    if stat == 0:
        try:
            accept_result = accept_order_for_line(key, operator=operator or "system")
            results.append({"step": "accept", **accept_result})
            if accept_result.get("accepted"):
                from app.services.orders import order_line_sync

                order_line_sync.refresh_ord_lines([key])
                row = get_ord_line(key) or row
                stat = _int_stat(row)
        except ProcurementAcceptError as exc:
            state = _save_state(
                key,
                pipeline_step="accept",
                ord_line_stat=0,
                blockers=parse_admin_error_blockers(str(exc), stage="accept"),
                last_error=str(exc),
            )
            return {"ok": False, "ord_line_no": key, "step": "accept", "error": str(exc), "state": state}

    if stat == 23:
        row = _try_auto_category_map(row)
        prep = evaluate_prepare_stage(row)
        if not prep.get("all_clear"):
            state = _save_state(
                key,
                pipeline_step="prepare",
                ord_line_stat=23,
                blockers=prep.get("blockers") or [],
            )
            return {"ok": False, "ord_line_no": key, "step": "prepare", "blockers": prep.get("blockers"), "state": state}

        try:
            pre_result = submit_1688_pre_purchase(key, operator=operator or "system", trigger="pipeline")
            results.append({"step": "pre_purchase", **pre_result})
            if pre_result.get("ok"):
                from app.services.orders import order_line_sync

                order_line_sync.refresh_ord_lines([key])
                row = get_ord_line(key) or row
                stat = _int_stat(row)
            elif pre_result.get("code") == "blocked_prepare":
                state = _save_state(
                    key,
                    pipeline_step="prepare",
                    ord_line_stat=23,
                    blockers=pre_result.get("blockers") or [],
                )
                return {"ok": False, "ord_line_no": key, "step": "prepare", "state": state, **pre_result}
        except ProcurementReleaseError as exc:
            blockers = parse_admin_error_blockers(str(exc), stage="pre_purchase")
            state = _save_state(key, pipeline_step="pre_purchase", ord_line_stat=23, blockers=blockers, last_error=str(exc))
            return {"ok": False, "ord_line_no": key, "step": "pre_purchase", "error": str(exc), "state": state}

    if stat == 54:
        eligible, _ = is_1688_place_order_eligible(row)
        if not eligible:
            state = _save_state(key, pipeline_step="place_order", ord_line_stat=54, blockers=[])
            return {"ok": True, "ord_line_no": key, "step": "place_order", "code": "skip", "state": state}
        try:
            place_result = submit_1688_place_order(
                [key],
                operator=operator or "system",
                trigger="auto_place",
                merge_same_store=True,
            )
            results.append({"step": "place_order", **place_result})
            if place_result.get("errors"):
                err_msg = (place_result.get("errors") or [{}])[0].get("error", "")
                blockers = parse_admin_error_blockers(err_msg, stage="place_order")
                state = _save_state(key, pipeline_step="place_order", ord_line_stat=54, blockers=blockers, last_error=err_msg)
                return {"ok": False, "ord_line_no": key, "step": "place_order", "state": state, **place_result}
            from app.services.orders import order_line_sync

            order_line_sync.refresh_ord_lines([key])
            row = get_ord_line(key) or row
            stat = _int_stat(row)
        except ProcurementPlaceOrderError as exc:
            blockers = parse_admin_error_blockers(str(exc), stage="place_order")
            state = _save_state(key, pipeline_step="place_order", ord_line_stat=54, blockers=blockers, last_error=str(exc))
            return {"ok": False, "ord_line_no": key, "step": "place_order", "error": str(exc), "state": state}

    if stat in (55, -1, -2, 2):
        pay = _step_payment_monitor(row)
        results.append(pay)
        if not pay.get("ok"):
            return {"ok": False, "ord_line_no": key, **pay}

    if stat == 22:
        follow = _step_followup_monitor(row)
        results.append(follow)
        if not follow.get("ok"):
            return {"ok": False, "ord_line_no": key, **follow}

    final_stat = _int_stat(row)
    step = "done"
    if final_stat in (0,):
        step = "accept"
    elif final_stat == 23:
        step = "prepare"
    elif final_stat == 54:
        step = "place_order"
    elif final_stat in (55, -1, -2, 2):
        step = "payment"
    elif final_stat == 22:
        step = "followup"

    state = _save_state(key, pipeline_step=step, ord_line_stat=final_stat, blockers=[])
    return {"ok": True, "ord_line_no": key, "pipeline_step": step, "ord_line_stat": final_stat, "results": results, "state": state}


def run_pipeline_batch(
    ord_line_nos: Optional[list[str]] = None,
    *,
    trigger: PipelineTrigger = "sync",
) -> dict[str, Any]:
    cfg = normalize_business_config(get_business_config())
    pool_accept: dict[str, Any] = {"accepted": []}
    if cfg.get("auto_accept_orders_enabled", True) and trigger == "sync":
        try:
            pool_accept = scan_and_accept_pool(operator="system")
        except ProcurementAcceptError as exc:
            pool_accept = {"error": str(exc)}

    if ord_line_nos:
        keys = [str(k).strip() for k in ord_line_nos if str(k).strip()]
    else:
        rows = line_cache.list_cached_lines(queue="pending_procurement")
        keys = [str(r.get("ord_line_no") or "").strip() for r in rows if r.get("ord_line_no")]

    advanced: list[str] = []
    blocked: list[str] = []
    errors: list[dict[str, str]] = []
    for key in keys:
        try:
            result = run_pipeline_for_line(key, trigger=trigger)
            if result.get("ok") and not result.get("blockers"):
                advanced.append(key)
            elif result.get("blockers") or result.get("state", {}).get("blockers"):
                blocked.append(key)
            elif not result.get("ok"):
                errors.append({"ord_line_no": key, "error": result.get("error") or result.get("code", "failed")})
        except Exception as exc:
            errors.append({"ord_line_no": key, "error": str(exc)})

    return {
        "pool_accept": pool_accept,
        "candidates": len(keys),
        "advanced": advanced,
        "blocked": blocked,
        "errors": errors or None,
    }


def resume_pipeline(ord_line_no: str, *, operator: Optional[str] = None) -> dict[str, Any]:
    return run_pipeline_for_line(ord_line_no, trigger="manual", operator=operator)


def ack_blocker_and_resume(
    ord_line_no: str,
    blocker_key: str,
    *,
    operator: Optional[str] = None,
) -> dict[str, Any]:
    pipeline_store.ack_blocker(ord_line_no, blocker_key, operator=operator)
    return resume_pipeline(ord_line_no, operator=operator)


def get_pipeline_view(ord_line_no: str) -> dict[str, Any]:
    key = ord_line_no.strip()
    state = pipeline_store.get_pipeline_state(key)
    row = get_ord_line(key)
    if not state and row:
        stat = _int_stat(row)
        step = "prepare"
        if stat == 0:
            step = "accept"
        elif stat == 54:
            step = "place_order"
        elif stat in (55, -1, -2, 2):
            step = "payment"
        elif stat == 22:
            step = "followup"
        elif stat and stat >= 5:
            step = "done"
        prep = evaluate_prepare_stage(row) if stat == 23 else None
        return {
            "ord_line_no": key,
            "pipeline_step": step,
            "ord_line_stat": stat,
            "blockers": (prep or {}).get("blockers") or [],
            "prepare": prep,
        }
    return state or {"ord_line_no": key, "pipeline_step": "unknown", "blockers": []}

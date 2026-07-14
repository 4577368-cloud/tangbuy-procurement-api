"""采购履约流水线编排：接单 → 准备 → 预订购 → 下单。"""

from __future__ import annotations

import contextvars
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from app.config.business_config import normalize_business_config
from app.config.store import get_business_config
from app.services.orders import line_cache, pipeline_store
from app.services.orders.procurement_accept import (
    ProcurementAcceptError,
    accept_order_for_line,
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
from app.services.orders.procurement_release import GENERIC_CATEGORIES
from app.services.products.store import find_product_for_ord_line, is_valid_hs_mapping

PipelineTrigger = Literal["sync", "manual", "ack", "category", "switch_supplier"]

_PIPELINE_ACTIVE: contextvars.ContextVar[bool] = contextvars.ContextVar("pipeline_active", default=False)
_READY_CATEGORY_STATUSES = frozenset({"auto_passed", "confirmed"})


def is_pipeline_active() -> bool:
    """当前线程是否正在 run_pipeline_for_line 内（映射副作用勿再 resume）。"""
    return bool(_PIPELINE_ACTIVE.get())


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
    from app.services.orders.admin_error_classify import classify_admin_error

    classified = classify_admin_error(msg, allow_llm=True)
    detail = summarize_admin_blocker_detail(classified.key, classified.label, msg)
    return [
        {
            "key": classified.key,
            "label": classified.label,
            "stage": stage,
            "auto_resolvable": False,
            "requires_ack": True,
            "detail": detail,
            "raw_detail": msg,
            "classify_reason": classified.reason,
            "classify_source": classified.source,
            "classify_confidence": classified.confidence,
            "at": _now_iso(),
        }
    ]


def _row_category_ok(row: dict[str, Any]) -> bool:
    category = str(row.get("lvl1_ctgy_nm") or "").strip()
    return bool(category) and category not in GENERIC_CATEGORIES


def _product_mapping_ready(product: Optional[dict[str, Any]]) -> bool:
    if not product:
        return False
    if str(product.get("category_status") or "") not in _READY_CATEGORY_STATUSES:
        return False
    return is_valid_hs_mapping(product.get("hs_mapping"))


def _category_blockers(row: dict[str, Any], *, stage: str = "accept") -> list[dict[str, Any]]:
    category = str(row.get("lvl1_ctgy_nm") or "").strip()
    cfg = normalize_business_config(get_business_config())
    auto_map = bool(cfg.get("rules", {}).get("auto_category_mapping", True))
    return [
        {
            "key": "CATEGORY_OTHER",
            "label": "品类未映射",
            "stage": stage,
            "auto_resolvable": auto_map,
            "requires_ack": False,
            "detail": category or "其他",
            "at": _now_iso(),
        }
    ]


def _overlay_product_category(row: dict[str, Any], product: dict[str, Any]) -> dict[str, Any]:
    """把商品中心已确认 HS 立即写到子单快照，不依赖 Admin 回写完成。"""
    if not _product_mapping_ready(product):
        return row
    key = str(row.get("ord_line_no") or "").strip()
    hs = product.get("hs_mapping")
    if not key or not isinstance(hs, dict):
        return row
    try:
        line_cache.apply_category_overlay_to_lines([key], hs, source="category_ensure")
        refreshed = get_ord_line(key)
        return refreshed or row
    except Exception:
        return row


def _ensure_product_for_line(row: dict[str, Any]) -> Optional[dict[str, Any]]:
    product = find_product_for_ord_line(row)
    if product:
        return product
    try:
        from app.services.products.order_sync import upsert_product_from_ord_line
        from app.services.products.service import _new_product_id

        product, _ = upsert_product_from_ord_line(row, new_id_fn=_new_product_id)
        return product
    except Exception:
        return None


def ensure_category_before_accept(row: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """接单前品类闸门：无商品则先从子单建档，尝试自动映射；仍失败则阻塞。

    不再以「商品中心已有货」为前提跳过。识别通过 / Admin 品类已合规 → 无 blocker。
    """
    if _row_category_ok(row):
        return row, []

    cfg = normalize_business_config(get_business_config())
    auto_map = bool(cfg.get("rules", {}).get("auto_category_mapping", True))
    product = _ensure_product_for_line(row)
    if product:
        row = _overlay_product_category(row, product)
        if _row_category_ok(row):
            return row, []

    if auto_map and product:
        pid = str(product.get("tangbuy_product_id") or product.get("id") or "").strip()
        if pid:
            try:
                from app.services.products.service import map_product_category_by_id
                from app.services.orders import order_line_sync

                mapped = map_product_category_by_id(pid, ord_row=row)
                if mapped:
                    product = mapped
                    row = _overlay_product_category(row, product)
                key = str(row.get("ord_line_no") or "").strip()
                if key:
                    try:
                        order_line_sync.refresh_ord_lines([key])
                    except Exception:
                        pass
                    row = get_ord_line(key) or row
                    if product:
                        row = _overlay_product_category(row, product)
            except Exception:
                pass

    if _row_category_ok(row):
        return row, []
    return row, _category_blockers(row, stage="accept")


def _try_auto_category_map(row: dict[str, Any]) -> dict[str, Any]:
    """兼容旧调用：尝试映射后返回行；阻塞由调用方 evaluate。"""
    updated, _ = ensure_category_before_accept(row)
    return updated


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

    token = _PIPELINE_ACTIVE.set(True)
    try:
        return _run_pipeline_for_line_inner(key, trigger=trigger, operator=operator)
    finally:
        _PIPELINE_ACTIVE.reset(token)


def _run_pipeline_for_line_inner(
    key: str,
    *,
    trigger: PipelineTrigger = "sync",
    operator: Optional[str] = None,
) -> dict[str, Any]:
    row = get_ord_line(key)
    if not row:
        return {"ok": False, "code": "not_found", "ord_line_no": key}

    stat = _int_stat(row)
    results: list[dict[str, Any]] = []

    if stat == 0:
        row, category_blockers = ensure_category_before_accept(row)
        if category_blockers:
            state = _save_state(
                key,
                pipeline_step="accept",
                ord_line_stat=0,
                blockers=category_blockers,
            )
            return {
                "ok": False,
                "ord_line_no": key,
                "step": "accept",
                "blockers": category_blockers,
                "state": state,
            }
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
        eligible, code = is_1688_place_order_eligible(row)
        if not eligible:
            # 勿在此函数内再 import evaluate_prepare_stage，会遮蔽模块级绑定并触发
            # UnboundLocalError（stat=23 预订购路径直接失败，品类回写后 resume 也会挂）。
            prep = evaluate_prepare_stage(row)
            blockers = prep.get("blockers") if isinstance(prep.get("blockers"), list) else []
            state = _save_state(
                key,
                pipeline_step="place_order" if not blockers else "blocked",
                ord_line_stat=54,
                blockers=blockers,
            )
            if blockers:
                return {
                    "ok": False,
                    "ord_line_no": key,
                    "step": "place_order",
                    "code": code,
                    "blockers": blockers,
                    "state": state,
                }
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
    """批量推进履约。接单一律走 run_pipeline_for_line（含品类闸门），不再盲扫接单池。"""
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
        "pool_accept": {"accepted": [], "skipped": "gated_by_line_pipeline"},
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
    key = blocker_key.strip()
    # 备注类：软确认写 NOTE_HANDLED，否则仍会拦预订购/下单
    if key in ("NOTE_BLOCK", "NOTE_REVIEW", "NOTE_HANDLED"):
        pipeline_store.ack_blocker(ord_line_no, "NOTE_HANDLED", operator=operator)
    pipeline_store.ack_blocker(ord_line_no, key, operator=operator)
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
        blockers = (prep or {}).get("blockers") or []
        if stat == 0 and not _row_category_ok(row):
            blockers = _category_blockers(row, stage="accept")
        return {
            "ord_line_no": key,
            "pipeline_step": step,
            "ord_line_stat": stat,
            "blockers": blockers,
            "prepare": prep,
        }
    return state or {"ord_line_no": key, "pipeline_step": "unknown", "blockers": []}

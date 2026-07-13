"""数据中心指标（对齐 procurement-demo data-center/route.ts 契约）。"""

from __future__ import annotations

import logging
import time as _time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

_log = logging.getLogger(__name__)

from app.services.channel_stats import compute_channel_distribution
from app.services.orders import service as order_service
from app.services.orders.exception_rules import (
    scan_aftersale_breakdown,
    scan_exception_reasons,
    scan_exception_summary,
)
from app.services.products.service import list_products
from app.services.stage_dwell import compute_stage_durations
from app.services.tasks.store import get_agent_operation_stats

HOTSPOT_TOP_N = 8

IN_TRANSIT_QUEUES = ("shipped", "in_warehouse", "dispatched")
FORWARD_QUEUES = (
    "pending_procurement",
    "pending_payment",
    "ordered",
    "shipped",
    "in_warehouse",
    "dispatched",
    "exception",
)

QUEUE_LABELS: dict[str, str] = {
    "pending_procurement": "待下单",
    "pending_payment": "待支付",
    "ordered": "已订购",
    "shipped": "已发货",
    "in_warehouse": "已到仓",
    "dispatched": "已发出",
    "exception": "异常",
    "reverse": "逆向",
}

EXCEPTION_SCAN_QUEUES = ("pending_procurement", "exception", "reverse")
EXCEPTION_SCAN_PAGE_SIZE = 80
_SNAPSHOT_TTL_SECONDS = 60
_snapshot_cache: dict[str, Any] = {"at": 0.0, "value": None}


def _empty_metrics() -> dict[str, Any]:
    return {
        "today_auto_pass": 0,
        "today_intercept": 0,
        "ai_adoption_rate": 0,
        "ai_override_rate": 0,
        "exception_close_rate": 0,
        "avg_stage_durations": [],
        "hotspot_reasons": [],
    }


def _empty_ai_quality(*, pending_review: int = 0) -> dict[str, Any]:
    return {
        "today_suggestions": 0,
        "adoption_rate": 0,
        "override_rate": 0,
        "pending_review": pending_review,
    }


def _empty_agent_ops() -> dict[str, Any]:
    return {
        "total": 0,
        "active": 0,
        "completed": 0,
        "failed": 0,
        "by_type": [],
        "by_status": {
            "in_progress": 0,
            "ready": 0,
            "needs_review": 0,
            "completed": 0,
            "failed": 0,
            "killed": 0,
        },
    }


def _empty_fulfillment() -> dict[str, Any]:
    return {
        "total": 0,
        "forward": 0,
        "inTransit": 0,
        "reverse": 0,
        "blocking": 0,
        "overdue": 0,
    }


def _empty_queue() -> dict[str, Any]:
    return {
        "action_required": 0,
        "needs_attention": 0,
        "watch_list": 0,
    }


def _scan_order_exception_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    def _fetch(queue: str) -> list[dict[str, Any]]:
        result = order_service.list_ord_lines(
            queue=queue, page=1, page_size=EXCEPTION_SCAN_PAGE_SIZE
        )
        items = result.get("items") if isinstance(result.get("items"), list) else []
        return items

    with ThreadPoolExecutor(max_workers=len(EXCEPTION_SCAN_QUEUES)) as pool:
        futures = [pool.submit(_fetch, q) for q in EXCEPTION_SCAN_QUEUES]
        for fut in futures:
            rows.extend(fut.result())
    return rows


def get_data_center_snapshot() -> dict[str, Any]:
    nowmono = _time.monotonic()
    cached = _snapshot_cache.get("value")
    if (
        cached
        and nowmono - float(_snapshot_cache["at"] or 0.0) < _SNAPSHOT_TTL_SECONDS
        and not cached.get("ordersError")
    ):
        return cached  # type: ignore[return-value]

    snapshot = _build_data_center_snapshot()
    _snapshot_cache["value"] = snapshot
    _snapshot_cache["at"] = nowmono
    return snapshot


def _build_data_center_snapshot() -> dict[str, Any]:
    """聚合多源指标；单块失败时降级为空数据，避免整页 500。"""
    partial_errors: list[str] = []

    try:
        agent_ops = get_agent_operation_stats()
    except Exception as exc:
        _log.warning("data center agent_ops failed: %s", exc)
        partial_errors.append("Agent 统计不可用")
        agent_ops = _empty_agent_ops()

    pending_mapping = 0
    try:
        products = list_products()
        pending_mapping = sum(
            1
            for p in products
            if p.get("category_status") in ("pending", "mapping", "failed")
        )
    except Exception as exc:
        _log.warning("data center products failed: %s", exc)
        partial_errors.append("商品统计不可用")

    counts: dict[str, Any] = {}
    orders_source = "unknown"
    orders_error: Optional[str] = None
    try:
        summary = order_service.cached_queue_summary()
        counts = summary.get("counts") if isinstance(summary.get("counts"), dict) else {}
        orders_source = str(summary.get("source") or "unknown")
        orders_error = summary.get("error")
        if orders_error is not None:
            orders_error = str(orders_error)
    except Exception as exc:
        _log.warning("data center order summary failed: %s", exc)
        partial_errors.append("订单汇总不可用")
        orders_error = str(exc)

    forward_total = sum(int(counts.get(q) or 0) for q in FORWARD_QUEUES)
    reverse_total = int(counts.get("reverse") or 0)
    in_transit = sum(int(counts.get(q) or 0) for q in IN_TRANSIT_QUEUES)

    exception_summary = _empty_queue()
    hotspot_reasons: list[dict[str, Any]] = []
    aftersale_breakdown: list[dict[str, Any]] = []
    try:
        exception_rows = _scan_order_exception_rows()
        exception_summary = scan_exception_summary(exception_rows)
        hotspot_reasons = scan_exception_reasons(exception_rows)[:HOTSPOT_TOP_N]
        aftersale_breakdown = scan_aftersale_breakdown(exception_rows)
    except Exception as exc:
        _log.warning("data center exception scan failed: %s", exc)
        partial_errors.append("异常扫描不可用")

    metrics = _empty_metrics()
    metrics["hotspot_reasons"] = hotspot_reasons
    channel_distribution: list[dict[str, Any]] = []
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            stage_future = pool.submit(compute_stage_durations)
            channel_future = pool.submit(compute_channel_distribution)
            metrics["avg_stage_durations"] = stage_future.result(timeout=45)
            channel_distribution = channel_future.result(timeout=45)
    except Exception as exc:
        _log.warning("data center stage/channel stats failed: %s", exc)
        partial_errors.append("阶段/渠道统计超时或不可用")
        metrics["avg_stage_durations"] = []

    order_distribution = [
        {"queue": q, "label": QUEUE_LABELS[q], "count": int(counts.get(q) or 0)}
        for q in QUEUE_LABELS
    ]

    load_error = "；".join(partial_errors) if partial_errors else None

    return {
        "agentOps": agent_ops,
        "metrics": metrics,
        "aiQuality": _empty_ai_quality(pending_review=pending_mapping),
        "fulfillment": {
            "total": forward_total + reverse_total,
            "forward": forward_total,
            "inTransit": in_transit,
            "reverse": reverse_total,
            "blocking": int(exception_summary.get("blocking") or 0),
            "overdue": 0,
        },
        "orderCounts": counts,
        "ordersSource": orders_source,
        "ordersError": orders_error,
        "orderDistribution": order_distribution,
        "aftersaleBreakdown": aftersale_breakdown,
        "channelDistribution": channel_distribution,
        "queue": {
            "action_required": int(exception_summary.get("action_required") or 0),
            "needs_attention": int(exception_summary.get("needs_attention") or 0),
            "watch_list": int(exception_summary.get("watch_list") or 0),
        },
        "loadError": load_error,
    }

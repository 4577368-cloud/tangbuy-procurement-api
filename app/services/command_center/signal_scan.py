"""指挥中心信号扫描 — 与履约简报共用口径。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any

from app.services.command_center.board_signals import (
    BOARD_SIGNAL_KEYS,
    aggregate_board_signal_stats,
    row_to_board_signal,
)
from app.services.orders import service as order_service
from app.services.orders.exception_rules import (
    classify_exception_reason,
    scan_exception_summary,
)
from app.services.orders.procurement_scope import is_in_procurement_scope
from app.services.products.service import list_products

SIGNAL_SCAN_QUEUES = (
    "pending_procurement",
    "pending_payment",
    "ordered",
    "shipped",
    "exception",
    "reverse",
)
MAX_PER_QUEUE = 150
BRIEFING_MAX_PER_QUEUE = 40
_BRIEFING_SCAN_TIMEOUT_SEC = 45
_SHIP_OVERDUE_HOURS = 48

REASON_TO_SIGNAL: dict[str, str] = {
    "负毛利": "PAY_AMOUNT_GAP",
    "成本倒挂": "PAY_AMOUNT_GAP",
    "利润为负": "PAY_AMOUNT_GAP",
    "零毛利": "ZERO_MARGIN",
    "低毛利": "LOW_MARGIN",
    "规格不符": "SKU_MISMATCH",
    "备注-规格变更": "SKU_MISMATCH",
    "备注待核": "NOTE_REVIEW",
    "采购价高于建议": "SUGGESTED_PRICE_GAP",
}


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _page_size_for_queue(
    queue: str, queue_counts: dict[str, int], *, max_per_queue: int = MAX_PER_QUEUE
) -> int:
    total = int(queue_counts.get(queue) or 0)
    if total <= 0:
        return 0
    return min(total, max_per_queue)


def scan_ord_lines_for_signals(
    queue_counts: dict[str, int] | None = None,
    *,
    max_per_queue: int = MAX_PER_QUEUE,
    admin_timeout_sec: float | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """按队列总量拉取样本（每队列最多 MAX_PER_QUEUE），去重后返回行与每队列实扫条数。"""
    from app.services.orders.line_cache import list_cached_lines, load_all_lines

    counts = queue_counts or {}
    per_queue_scan: dict[str, int] = {}
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    if load_all_lines():
        for queue in SIGNAL_SCAN_QUEUES:
            size = _page_size_for_queue(queue, counts, max_per_queue=max_per_queue)
            if size <= 0:
                per_queue_scan[queue] = 0
                continue
            batch = list_cached_lines(queue=queue)[:size]
            per_queue_scan[queue] = len(batch)
            for row in batch:
                if not is_in_procurement_scope(row):
                    continue
                key = str(row.get("ord_line_no") or "")
                if key and key in seen:
                    continue
                if key:
                    seen.add(key)
                rows.append(row)
        return rows, per_queue_scan

    def _fetch(queue: str) -> tuple[str, list[dict[str, Any]]]:
        size = _page_size_for_queue(queue, counts, max_per_queue=max_per_queue)
        if size <= 0:
            return queue, []
        result = order_service.list_ord_lines(queue=queue, page=1, page_size=size)
        items = result.get("items") if isinstance(result.get("items"), list) else []
        return queue, [r for r in items if isinstance(r, dict)]

    with ThreadPoolExecutor(max_workers=len(SIGNAL_SCAN_QUEUES)) as pool:
        futures = [pool.submit(_fetch, q) for q in SIGNAL_SCAN_QUEUES]
        try:
            iterator = as_completed(futures, timeout=admin_timeout_sec)
        except TypeError:
            iterator = as_completed(futures)
        for fut in iterator:
            try:
                queue, batch = fut.result(timeout=admin_timeout_sec or 120)
            except Exception:
                continue
            per_queue_scan[queue] = len(batch)
            for row in batch:
                if not is_in_procurement_scope(row):
                    continue
                key = str(row.get("ord_line_no") or "")
                if key and key in seen:
                    continue
                if key:
                    seen.add(key)
                rows.append(row)

    return rows, per_queue_scan


def enrich_row_for_board(
    row: dict[str, Any],
    *,
    pipeline_states: dict[str, dict[str, Any]] | None = None,
    skip_disposition: bool = False,
) -> dict[str, Any]:
    """看板/简报信号所需 enrich（跳过品类映射 enrich，批量扫描不调备注 LLM）。"""
    from app.services.orders import disposition_store
    from app.services.orders.order_note_classify import enrich_row_note_fields
    from app.services.orders.order_sku_check import enrich_row_sku_fields
    from app.services.orders.pipeline_store import enrich_row_pipeline_fields
    from app.services.orders.platform_order_enrich import enrich_row_platform_order_fields
    from app.services.orders.purchase_cost import enrich_row_purchase_cost_fields

    base = dict(row) if skip_disposition else disposition_store.apply_row_override(dict(row))
    return enrich_row_pipeline_fields(
        enrich_row_purchase_cost_fields(
            enrich_row_platform_order_fields(
                enrich_row_sku_fields(enrich_row_note_fields(base, allow_llm=False))
            )
        ),
        states=pipeline_states,
    )


def enrich_scan_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """批量 enrich：预加载 pipeline / disposition，串行处理（避免文件锁争用）。"""
    if not rows:
        return []

    from app.services.orders import disposition_store
    from app.services.orders.pipeline_store import _pipeline_state_map

    pipeline_states = _pipeline_state_map()
    overrides = disposition_store.load_all_overrides()

    out: list[dict[str, Any]] = []
    for row in rows:
        base = dict(row)
        ord_line_no = str(base.get("ord_line_no") or "").strip()
        if ord_line_no:
            override = overrides.get(ord_line_no)
            if override:
                for key, value in override.items():
                    if key in ("queue_override", "passed_at", "action_key", "signal_type"):
                        continue
                    base[key] = value
        out.append(
            enrich_row_for_board(
                base,
                pipeline_states=pipeline_states,
                skip_disposition=True,
            )
        )
    return out


def load_all_scan_rows() -> list[dict[str, Any]]:
    """从本地缓存加载指挥中心相关队列的全部子单（非抽样）。"""
    from app.services.orders.line_cache import load_all_lines
    from app.services.orders.queue_filters import resolve_order_queue

    all_lines = load_all_lines()
    if not all_lines:
        return []

    rows: list[dict[str, Any]] = []
    for row in all_lines.values():
        if not isinstance(row, dict) or not is_in_procurement_scope(row):
            continue
        queue = resolve_order_queue(row)
        if queue not in SIGNAL_SCAN_QUEUES:
            continue
        rows.append(row)
    return rows


def _count_signal_types(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        if not is_in_procurement_scope(row):
            continue
        result = classify_exception_reason(row)
        if not result:
            continue
        signal = REASON_TO_SIGNAL.get(result[1], "OTHER")
        counts[signal] = counts.get(signal, 0) + 1
    return counts


def _count_ship_overdue(rows: list[dict[str, Any]]) -> int:
    now = datetime.now(timezone.utc)
    threshold = now - timedelta(hours=_SHIP_OVERDUE_HOURS)
    count = 0
    for row in rows:
        if not is_in_procurement_scope(row):
            continue
        if row.get("ord_line_stat") not in (22, "22"):
            continue
        ref = _parse_iso(row.get("pur_time")) or _parse_iso(row.get("pay_time"))
        if ref and ref < threshold:
            count += 1
    return count


def aggregate_signal_stats(
    rows: list[dict[str, Any]],
    *,
    products: list[dict[str, Any]] | None = None,
    enriched_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    enriched = enriched_rows if enriched_rows is not None else enrich_scan_rows(rows)
    product_list = products if products is not None else list_products()
    board = aggregate_board_signal_stats(enriched, product_list)

    exception_summary = scan_exception_summary(enriched)
    legacy_counts = _count_signal_types(enriched)
    ship_overdue = int(board["board_signal_counts"].get("SHIP_OVERDUE") or 0)
    if ship_overdue <= 0:
        ship_overdue = _count_ship_overdue(enriched)

    return {
        "signal_counts": legacy_counts,
        "board_signal_counts": board["board_signal_counts"],
        "board_signal_counts_action": board["board_signal_counts_action"],
        "board_band_counts": board["board_band_counts"],
        "exception_bands": {
            "action_required": exception_summary["action_required"],
            "needs_attention": exception_summary["needs_attention"],
            "watch_list": exception_summary["watch_list"],
            "blocking": exception_summary["blocking"],
        },
        "ship_overdue_estimated": ship_overdue,
    }


def list_command_center_signal_rows() -> list[dict[str, Any]]:
    """全量缓存扫描，返回带看板信号的子单行（与 stats 卡片同源）。"""
    rows = load_all_scan_rows()
    if not rows:
        return []
    products = list_products()
    enriched = enrich_scan_rows(rows)
    out: list[dict[str, Any]] = []
    for row in enriched:
        signal = row_to_board_signal(row, products)
        if not signal:
            continue
        signal_type = str(signal.get("signal_type") or "")
        if signal_type not in BOARD_SIGNAL_KEYS:
            continue
        out.append(row)
    return out


def build_signal_scan_payload(queue_counts: dict[str, int]) -> dict[str, Any]:
    rows, per_queue_scan = scan_ord_lines_for_signals(queue_counts)
    stats = aggregate_signal_stats(rows)
    return {
        **stats,
        "scanned_rows": len(rows),
        "per_queue_scan": per_queue_scan,
    }

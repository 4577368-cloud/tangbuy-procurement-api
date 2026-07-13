"""指挥中心扫描物化缓存 — stats / signals / briefing 共用一次 enrich。"""

from __future__ import annotations

import time as _time
from datetime import datetime, timezone
from typing import Any

from app.services.command_center.board_signals import BOARD_SIGNAL_KEYS, row_to_board_signal
from app.services.command_center.signal_scan import (
    aggregate_signal_stats,
    enrich_scan_rows,
    load_all_scan_rows,
)
from app.services.products.service import list_products

_SCAN_TTL_SEC = 180.0
_scan_cache: dict[str, Any] = {"at": 0.0, "version": "", "payload": None}


def _cache_version() -> str:
    from app.services.orders.line_cache import load_sync_state

    state = load_sync_state()
    parts = [
        str(state.get("last_incremental_at") or ""),
        str(state.get("last_full_at") or ""),
        str(state.get("cached_total") or ""),
    ]
    return "|".join(parts)


def invalidate_command_center_scan() -> None:
    """订单快照或商品变更后调用，使 stats/signals/briefing 下次重建。"""
    _scan_cache["at"] = 0.0
    _scan_cache["version"] = ""
    _scan_cache["payload"] = None


def _build_scan_payload() -> dict[str, Any]:
    rows = load_all_scan_rows()
    if not rows:
        empty_stats = aggregate_signal_stats([], products=[], enriched_rows=[])
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "rows": [],
            "enriched_rows": [],
            "stats": empty_stats,
            "signal_items": [],
            "per_queue_scan": {},
        }

    products = list_products()
    enriched_rows = enrich_scan_rows(rows)
    stats = aggregate_signal_stats(
        rows, products=products, enriched_rows=enriched_rows
    )

    signal_items: list[dict[str, Any]] = []
    for row in enriched_rows:
        signal = row_to_board_signal(row, products)
        if not signal:
            continue
        signal_type = str(signal.get("signal_type") or "")
        if signal_type not in BOARD_SIGNAL_KEYS:
            continue
        signal_items.append(row)

    from app.services.orders.queue_filters import resolve_order_queue

    per_queue_scan: dict[str, int] = {}
    for row in rows:
        queue = resolve_order_queue(row) or "unknown"
        per_queue_scan[queue] = per_queue_scan.get(queue, 0) + 1

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rows": rows,
        "enriched_rows": enriched_rows,
        "stats": stats,
        "signal_items": signal_items,
        "per_queue_scan": per_queue_scan,
    }


def get_command_center_scan(*, force: bool = False) -> dict[str, Any]:
    """返回一次全量扫描结果（带 TTL + 同步版本号）。"""
    version = _cache_version()
    if not force:
        payload = _scan_cache.get("payload")
        if (
            payload
            and _scan_cache.get("version") == version
            and _time.monotonic() - float(_scan_cache.get("at") or 0.0) < _SCAN_TTL_SEC
        ):
            return payload  # type: ignore[return-value]

    payload = _build_scan_payload()
    _scan_cache["payload"] = payload
    _scan_cache["version"] = version
    _scan_cache["at"] = _time.monotonic()
    return payload

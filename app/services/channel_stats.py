"""渠道 / 平台分布（按下单渠道聚合近期订单）。

按队列各取一页近期订单，聚合 shop_pltf_cd（storeSource 平台）分布。
属采样口径，带 TTL + 过期兜底缓存，并行拉取各队列。
"""

from __future__ import annotations

import threading
import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from app.services.orders import service as order_service

_SCAN_QUEUES = (
    "pending_procurement",
    "pending_payment",
    "ordered",
    "shipped",
    "reverse",
)
_SCAN_PAGE_SIZE = 30
_CACHE_TTL_SECONDS = 600
_STALE_SERVE_SECONDS = 3600

_cache: dict[str, Any] = {"at": 0.0, "value": []}
_refresh_lock = threading.Lock()
_refreshing = False


def _channel_label(row: dict[str, Any]) -> str:
    raw = row.get("shop_pltf_cd") or row.get("data_src")
    if not raw:
        return "其他"
    return str(raw).strip() or "其他"


def _fetch_queue_rows(queue: str) -> list[dict[str, Any]]:
    try:
        result = order_service.list_ord_lines(
            queue=queue, page=1, page_size=_SCAN_PAGE_SIZE
        )
    except Exception:  # noqa: BLE001
        return []
    items = result.get("items") if isinstance(result.get("items"), list) else []
    return [row for row in items if isinstance(row, dict)]


def _recompute_channel_distribution() -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    seen: set[str] = set()

    with ThreadPoolExecutor(max_workers=len(_SCAN_QUEUES)) as pool:
        futures = [pool.submit(_fetch_queue_rows, q) for q in _SCAN_QUEUES]
        for fut in as_completed(futures):
            for row in fut.result():
                ord_line_no = str(row.get("ord_line_no") or "")
                if ord_line_no and ord_line_no in seen:
                    continue
                if ord_line_no:
                    seen.add(ord_line_no)
                label = _channel_label(row)
                counts[label] = counts.get(label, 0) + 1

    distribution = [
        {"label": label, "count": count}
        for label, count in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    ]
    _cache["value"] = distribution
    _cache["at"] = _time.monotonic()
    return distribution


def _refresh_in_background() -> None:
    global _refreshing
    with _refresh_lock:
        if _refreshing:
            return
        _refreshing = True
    try:
        _recompute_channel_distribution()
    finally:
        with _refresh_lock:
            _refreshing = False


def compute_channel_distribution(force: bool = False) -> list[dict[str, Any]]:
    """返回 [{label, count}]，按数量降序。带 TTL + 过期兜底缓存。"""
    nowmono = _time.monotonic()
    cached = _cache["value"]
    age = nowmono - float(_cache["at"] or 0.0)

    if not force and cached and age < _CACHE_TTL_SECONDS:
        return cached  # type: ignore[return-value]

    if not force and cached and age < _STALE_SERVE_SECONDS:
        threading.Thread(target=_refresh_in_background, daemon=True).start()
        return cached  # type: ignore[return-value]

    return _recompute_channel_distribution()

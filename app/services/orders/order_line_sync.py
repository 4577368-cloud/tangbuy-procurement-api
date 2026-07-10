"""订单分批同步：增量拉最近页 + 后台回填历史页。"""

from __future__ import annotations

from typing import Any, Optional

from datetime import datetime, timezone

from app.services.orders import line_cache
from app.services.orders import service as order_service

DEFAULT_PAGE_SIZE = 200
INCREMENTAL_PAGES = 2
BACKFILL_QUEUES = ("all",)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _fetch_page(queue: Optional[str], page: int, page_size: int) -> dict[str, Any]:
    q = None if queue == "all" else queue
    return order_service.list_ord_lines(queue=q, page=page, page_size=page_size)


def sync_orders_incremental(
    *,
    queue: Optional[str] = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    pages: int = INCREMENTAL_PAGES,
) -> dict[str, Any]:
    """拉各队列最近若干页，只合并新增与状态变更。"""
    targets = [queue] if queue and queue != "all" else list(line_cache.SYNC_QUEUES)
    totals = {"added": 0, "updated": 0, "unchanged": 0, "scanned": 0, "pages": 0}
    errors: list[str] = []

    for q in targets:
        for page in range(1, max(1, pages) + 1):
            res = _fetch_page(q, page, page_size)
            if res.get("error"):
                errors.append(f"{q}: {res['error']}")
                break
            items = res.get("items") or []
            stats = line_cache.merge_lines(items)
            for key in ("added", "updated", "unchanged"):
                totals[key] += int(stats.get(key) or 0)
            totals["scanned"] += len(items)
            totals["pages"] += 1
            if len(items) < page_size:
                break

    state = line_cache.load_sync_state()
    state["last_incremental_at"] = _now_iso()
    state["cached_total"] = len(line_cache.load_all_lines())
    if not state.get("backfill_complete"):
        state["backfill_complete"] = False
    line_cache.save_sync_state(state)

    return {
        "ok": not errors,
        "mode": "incremental",
        "stats": totals,
        "cache_total": state["cached_total"],
        "errors": errors or None,
        "items": line_cache.list_cached_lines(queue=queue),
    }


def sync_orders_backfill_batch(
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
    batches: int = 1,
) -> dict[str, Any]:
    """继续历史回填，每次处理若干页（跨队列轮转）。"""
    state = line_cache.load_sync_state()
    queues = list(BACKFILL_QUEUES)
    qi = int(state.get("backfill_queue_index") or 0) % len(queues)
    page = max(1, int(state.get("backfill_page") or 1))

    totals = {"added": 0, "updated": 0, "unchanged": 0, "scanned": 0, "pages": 0}
    errors: list[str] = []
    done_batches = 0
    current_queue = queues[qi]

    while done_batches < max(1, batches):
        res = _fetch_page(current_queue, page, page_size)
        if res.get("error"):
            errors.append(f"{current_queue} p{page}: {res['error']}")
            break

        items = res.get("items") or []
        stats = line_cache.merge_lines(items)
        for key in ("added", "updated", "unchanged"):
            totals[key] += int(stats.get(key) or 0)
        totals["scanned"] += len(items)
        totals["pages"] += 1
        done_batches += 1

        admin_total = int(res.get("total") or 0)
        if len(items) < page_size or page * page_size >= admin_total:
            qi = (qi + 1) % len(queues)
            page = 1
            if qi == 0:
                state["backfill_complete"] = True
        else:
            page += 1
            state["backfill_complete"] = False

        current_queue = queues[qi]
        if state.get("backfill_complete"):
            break

    state["backfill_queue_index"] = qi
    state["backfill_page"] = page
    state["last_backfill_at"] = _now_iso()
    state["cached_total"] = len(line_cache.load_all_lines())
    line_cache.save_sync_state(state)

    return {
        "ok": not errors,
        "mode": "backfill",
        "stats": totals,
        "cache_total": state["cached_total"],
        "backfill": {
            "complete": bool(state.get("backfill_complete")),
            "queue": current_queue,
            "next_page": page,
        },
        "errors": errors or None,
    }

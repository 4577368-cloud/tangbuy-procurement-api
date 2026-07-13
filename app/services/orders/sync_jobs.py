"""订单同步任务执行体。"""

from __future__ import annotations

from typing import Any, Optional


def _run_order_sync(
    *,
    mode: str,
    queue: Optional[str],
    page_size: int,
    pages: int,
    batches: int,
    pipeline_inline: bool,
) -> dict[str, Any]:
    from app.services.orders import order_line_sync

    mode_key = (mode or "incremental").strip().lower()
    if mode_key == "full":
        return order_line_sync.sync_orders_full(
            queue=queue, page_size=page_size, pipeline_inline=pipeline_inline
        )
    if mode_key == "backfill":
        return order_line_sync.sync_orders_backfill_batch(
            page_size=page_size,
            batches=batches,
            pipeline_inline=pipeline_inline,
        )
    return order_line_sync.sync_orders_incremental(
        queue=queue,
        page_size=page_size,
        pages=pages,
        pipeline_inline=pipeline_inline,
    )


def execute_order_sync(
    *,
    mode: str = "incremental",
    queue: Optional[str] = None,
    page_size: int = 200,
    pages: int = 2,
    batches: int = 1,
    pipeline_inline: bool = False,
    source: str = "api",
) -> dict[str, Any]:
    from app.services.orders.sync_coordinator import run_exclusive_sync

    page_size = max(1, min(200, int(page_size or 200)))
    pages = max(1, min(10, int(pages or 2)))
    batches = max(1, min(20, int(batches or 1)))
    mode_key = (mode or "incremental").strip().lower()
    label = f"{source}:{mode_key}"

    return run_exclusive_sync(
        lambda: _run_order_sync(
            mode=mode_key,
            queue=queue,
            page_size=page_size,
            pages=pages,
            batches=batches,
            pipeline_inline=pipeline_inline,
        ),
        source=label,
    )

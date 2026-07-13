"""商品从订单同步 — 后台任务执行体。"""

from __future__ import annotations

from typing import Any, Optional

from app.services.products import service as product_service


def execute_products_sync_from_orders(
    *,
    queue: str = "pending_procurement",
    page: int = 1,
    page_size: int = 200,
    auto_map: bool = True,
    all_pages: bool = False,
    max_pages: int = 100,
) -> dict[str, Any]:
    from app.services.products.order_sync import sync_products_from_orders

    result = sync_products_from_orders(
        queue=queue.strip() or "pending_procurement",
        page=max(1, int(page or 1)),
        page_size=max(1, min(500, int(page_size or 200))),
        auto_map=auto_map is not False,
        all_pages=all_pages is True,
        max_pages=max(1, min(200, int(max_pages or 100))),
    )
    stats = dict(result.get("stats") or {})
    stats["products_total"] = product_service.get_product_stats().get("total", 0)
    return {**result, "stats": stats}

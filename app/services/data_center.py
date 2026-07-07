"""数据中心指标（对齐 data-center/route.ts 契约）。"""

from __future__ import annotations

from typing import Any

from app.services.products.service import list_products
from app.services.tasks.store import get_task_stats

IN_TRANSIT_STAGES = {"shipped", "in_warehouse", "dispatched"}


def get_data_center_snapshot() -> dict[str, Any]:
    stats = get_task_stats()
    agent_ops = {
        "total": stats["total"],
        "in_progress": stats["in_progress"],
        "ready": stats["ready"],
        "needs_review": stats["needs_review"],
        "completed": stats["completed"],
        "failed": stats.get("failed", 0),
        "killed": stats.get("killed", 0),
    }
    products = list_products()
    pending_mapping = sum(1 for p in products if p.get("category_status") in ("pending", "mapping", "failed"))
    return {
        "agentOps": agent_ops,
        "metrics": {
            "product_total": len(products),
            "pending_category_mapping": pending_mapping,
        },
        "aiQuality": {
            "skill_audit_pending": 0,
            "category_review_pending": pending_mapping,
        },
        "fulfillment": {
            "total": 0,
            "forward": 0,
            "inTransit": 0,
            "reverse": 0,
            "blocking": 0,
            "overdue": 0,
        },
        "queue": {
            "action_required": pending_mapping,
            "needs_attention": stats["needs_review"],
            "watch_list": stats["in_progress"],
        },
    }

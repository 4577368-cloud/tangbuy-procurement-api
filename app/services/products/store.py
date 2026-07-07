"""商品中心读写（品类映射确认回写）。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from app.core.paths import data_dir

_CENTER_PATH = data_dir() / "products" / "center.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def load_products() -> list[dict[str, Any]]:
    if not _CENTER_PATH.exists():
        return []
    try:
        data = json.loads(_CENTER_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def save_products(items: list[dict[str, Any]]) -> None:
    _CENTER_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CENTER_PATH.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def find_by_source_product_id(source_id: str) -> Optional[dict[str, Any]]:
    if not source_id:
        return None
    return next((p for p in load_products() if p.get("source_product_id") == source_id), None)


def update_product(
    tangbuy_id: str,
    updater: Callable[[dict[str, Any]], dict[str, Any]],
) -> Optional[dict[str, Any]]:
    items = load_products()
    idx = next((i for i, p in enumerate(items) if p.get("tangbuy_product_id") == tangbuy_id), -1)
    if idx < 0:
        return None
    items[idx] = updater(items[idx])
    save_products(items)
    return items[idx]


def confirm_product_mapping(
    product: dict[str, Any],
    hs: dict[str, Any],
    reviewer_note: Optional[str] = None,
    *,
    manual: bool = False,
) -> dict[str, Any]:
    now = _now_iso()
    category_path = f"{hs.get('category_cn_name', '')} / {hs.get('declare_cn_name', '')} · HS {hs.get('hs_code', '')}"
    mapping = product.get("mapping_record") or {}
    mapping.update(
        {
            "suggested_category_path": category_path,
            "suggested_hs": hs,
            "review_status": "corrected" if manual else "confirmed",
            "reviewed_at": now,
            "reviewer_note": reviewer_note,
        }
    )
    product["mapping_record"] = mapping
    product["category"] = hs.get("category_cn_name") or product.get("category")
    product["category_status"] = "auto_passed"
    product["mapping_confidence"] = 1.0 if manual else product.get("mapping_confidence", 0.9)
    return product

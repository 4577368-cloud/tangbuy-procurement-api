"""商品中心读写（品类映射确认回写）。"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from app.core.paths import data_dir
from app.services.category_mapping.local_mappings import upsert_local_mapping

_CENTER_PATH = data_dir() / "products" / "center.json"
_io_lock = threading.Lock()

RESOLVED_MAPPING_STATUSES = frozenset({"auto_passed", "confirmed", "needs_review"})
GENERIC_PLATFORM_HINTS = frozenset({"其它", "其他", "待映射", "—", "-", ""})


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _parse_products_text(raw: str) -> list[dict[str, Any]]:
    raw = raw.strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        # 并发写残留下尾时，尽量取首个完整数组
        try:
            data, _end = json.JSONDecoder().raw_decode(raw)
            return data if isinstance(data, list) else []
        except json.JSONDecodeError:
            return []


def load_products() -> list[dict[str, Any]]:
    with _io_lock:
        if not _CENTER_PATH.exists():
            return []
        try:
            return _parse_products_text(_CENTER_PATH.read_text(encoding="utf-8"))
        except OSError:
            return []


def save_products(items: list[dict[str, Any]]) -> None:
    payload = json.dumps(items, ensure_ascii=False, indent=2) + "\n"
    with _io_lock:
        _CENTER_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix="center-",
            suffix=".json.tmp",
            dir=str(_CENTER_PATH.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, _CENTER_PATH)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise


def find_by_source_product_id(source_id: str) -> Optional[dict[str, Any]]:
    if not source_id:
        return None
    return next((p for p in load_products() if p.get("source_product_id") == source_id), None)


def get_product_by_id(tangbuy_id: str) -> Optional[dict[str, Any]]:
    if not tangbuy_id:
        return None
    return next((p for p in load_products() if p.get("tangbuy_product_id") == tangbuy_id), None)


def find_product_for_ord_line(row: dict[str, Any]) -> Optional[dict[str, Any]]:
    ord_line = str(row.get("ord_line_no") or "").strip()
    if ord_line:
        for p in load_products():
            lines = p.get("linked_ord_lines") or []
            if ord_line in lines and not p.get("replaced_by_product_id"):
                return p
    item_id = str(row.get("item_id") or "").strip()
    if item_id:
        found = get_product_by_id(item_id)
        if found and not found.get("replaced_by_product_id"):
            return found
    splr_id = str(row.get("splr_item_id") or "").strip()
    if splr_id:
        found = find_by_source_product_id(splr_id)
        if found and not found.get("replaced_by_product_id"):
            return found
    return None


def is_valid_hs_mapping(hs: Any) -> bool:
    if not isinstance(hs, dict):
        return False
    try:
        cid = int(hs.get("category_id") or 0)
    except (TypeError, ValueError):
        return False
    if cid <= 0:
        return False
    name = str(hs.get("category_cn_name") or "").strip()
    return bool(name and name.lower() != "nan")


def mapping_hint_for_product(product: dict[str, Any]) -> Optional[str]:
    hint = str(product.get("platform_category_hint") or "").strip()
    if hint in GENERIC_PLATFORM_HINTS:
        return None
    return hint or None


def find_reusable_hs_mapping(
    product: dict[str, Any],
    *,
    exclude_id: Optional[str] = None,
) -> Optional[tuple[dict[str, Any], str]]:
    """同 SKU 复用已有映射，避免重复跑 Agent。"""
    from app.services.category_mapping.local_mappings import get_by_item_id, get_by_splr_item_id

    pid = str(product.get("tangbuy_product_id") or "").strip()
    splr = str(product.get("source_product_id") or "").strip()

    if splr:
        local = get_by_splr_item_id(splr)
        if local and is_valid_hs_mapping(local.get("hs")):
            return local["hs"], f"1688 offer {splr} 已有本地映射"

    if pid:
        local = get_by_item_id(pid)
        if local and is_valid_hs_mapping(local.get("hs")):
            return local["hs"], f"Tangbuy 商品 {pid} 已有本地映射"

    for sibling in load_products():
        sid = str(sibling.get("tangbuy_product_id") or "")
        if exclude_id and sid == exclude_id:
            continue
        if sibling.get("category_status") not in RESOLVED_MAPPING_STATUSES:
            continue
        hs = sibling.get("hs_mapping")
        if not is_valid_hs_mapping(hs):
            continue
        s_splr = str(sibling.get("source_product_id") or "").strip()
        if splr and s_splr == splr:
            return hs, f"商品中心同款 offer {splr}"
        if pid and sid == pid:
            return hs, f"商品中心同款 item {pid}"

    return None


def update_product(
    tangbuy_id: str,
    updater: Callable[[dict[str, Any]], dict[str, Any]],
) -> Optional[dict[str, Any]]:
    with _io_lock:
        if not _CENTER_PATH.exists():
            return None
        try:
            items = _parse_products_text(_CENTER_PATH.read_text(encoding="utf-8"))
        except OSError:
            return None
        idx = next((i for i, p in enumerate(items) if p.get("tangbuy_product_id") == tangbuy_id), -1)
        if idx < 0:
            return None
        items[idx] = updater(items[idx])
        payload = json.dumps(items, ensure_ascii=False, indent=2) + "\n"
        _CENTER_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix="center-",
            suffix=".json.tmp",
            dir=str(_CENTER_PATH.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, _CENTER_PATH)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        return items[idx]


def _ensure_catalog_cid(hs: dict[str, Any]) -> dict[str, Any]:
    """确认前若为权威兜底候选（无本地 cid），新增写回主目录以获得 cid。"""
    try:
        cid = int(hs.get("category_id") or 0)
    except (TypeError, ValueError):
        cid = 0
    if cid > 0:
        return hs
    hs_code = str(hs.get("hs_code") or "").strip()
    cn_name = str(hs.get("category_cn_name") or hs.get("declare_cn_name") or "").strip()
    if not (hs_code and cn_name):
        return hs
    from app.services.category_mapping.catalog_admin import append_catalog_entry

    try:
        mapping = append_catalog_entry(
            cn_name=cn_name,
            hs_code=hs_code,
            en_name=str(hs.get("category_en_name") or ""),
            declare_cn_name=str(hs.get("declare_cn_name") or ""),
            declare_en_name=str(hs.get("declare_en_name") or ""),
            tariff=hs.get("tariff"),
        )
    except ValueError:
        return hs
    merged = dict(hs)
    merged.update(mapping)
    return merged


def confirm_product_mapping(
    product: dict[str, Any],
    hs: dict[str, Any],
    reviewer_note: Optional[str] = None,
    *,
    manual: bool = False,
) -> dict[str, Any]:
    hs = _ensure_catalog_cid(hs)
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
    product["hs_mapping"] = hs
    product["category"] = hs.get("category_cn_name") or product.get("category")
    product["category_status"] = "auto_passed"
    product["mapping_confidence"] = 1.0 if manual else product.get("mapping_confidence", 0.9)
    product["mapping_mapped_at"] = now
    upsert_local_mapping(
        item_id=str(product.get("tangbuy_product_id") or "") or None,
        splr_item_id=str(product.get("source_product_id") or "") or None,
        hs=hs,
        source="correct" if manual else "confirm",
    )
    return product

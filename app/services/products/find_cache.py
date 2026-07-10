"""采购助手 1688 搜品结果缓存（服务端 JSON 持久化，含线上 data 目录）。"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from app.core.paths import data_dir

_CACHE_PATH = data_dir() / "products" / "find-cache.json"
_io_lock = threading.Lock()
_MAX_ENTRIES = 3000


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _normalize_product(raw: dict[str, Any]) -> Optional[dict[str, Any]]:
    pid = str(raw.get("product_id") or "").strip()
    if not pid:
        return None
    return {
        "product_id": pid,
        "title": str(raw.get("title") or "（无标题）").strip(),
        "image_url": str(raw.get("image_url") or "").strip(),
        "detail_url": str(raw.get("detail_url") or "").strip(),
        "price": raw.get("price"),
        "supplier": str(raw.get("supplier") or "").strip(),
        "sold_count": int(raw.get("sold_count") or 0),
        "similarity_score": raw.get("similarity_score"),
        "compare_label": raw.get("compare_label"),
    }


def load_find_cache() -> list[dict[str, Any]]:
    with _io_lock:
        if not _CACHE_PATH.exists():
            return []
        try:
            data = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, json.JSONDecodeError):
            return []


def _save_find_cache(entries: list[dict[str, Any]]) -> None:
    payload = json.dumps(entries, ensure_ascii=False, indent=2) + "\n"
    with _io_lock:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix="find-cache-",
            suffix=".json.tmp",
            dir=str(_CACHE_PATH.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, _CACHE_PATH)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise


def upsert_find_cache_items(
    products: list[dict[str, Any]],
    *,
    tool: str,
    query: Optional[str] = None,
    image_url: Optional[str] = None,
    source_url: Optional[str] = None,
    search_type: Optional[str] = None,
) -> int:
    """写入/更新缓存，按 1688 offer_id 去重；返回本次写入条数。"""
    if not products:
        return 0

    now = _now_iso()
    entries = load_find_cache()
    by_id = {str(e.get("product_id") or ""): e for e in entries if e.get("product_id")}

    written = 0
    for raw in products:
        if not isinstance(raw, dict):
            continue
        product = _normalize_product(raw)
        if not product:
            continue
        pid = product["product_id"]
        prev = by_id.get(pid) or {}
        by_id[pid] = {
            **prev,
            **product,
            "tool": tool,
            "search_type": search_type or prev.get("search_type"),
            "query": query or prev.get("query"),
            "image_url": image_url or prev.get("image_url"),
            "source_url": source_url or prev.get("source_url"),
            "searched_at": now,
            "promoted_at": prev.get("promoted_at"),
            "in_center": bool(prev.get("in_center")),
        }
        written += 1

    merged = sorted(
        by_id.values(),
        key=lambda e: str(e.get("searched_at") or ""),
        reverse=True,
    )[:_MAX_ENTRIES]
    _save_find_cache(merged)
    return written


def mark_find_cache_promoted(source_product_id: str) -> None:
    pid = source_product_id.strip()
    if not pid:
        return
    entries = load_find_cache()
    changed = False
    for entry in entries:
        if str(entry.get("product_id") or "") == pid:
            entry["in_center"] = True
            entry["promoted_at"] = _now_iso()
            changed = True
            break
    if changed:
        _save_find_cache(entries)


def list_find_cache(*, limit: int = 100, q: Optional[str] = None) -> list[dict[str, Any]]:
    items = load_find_cache()
    if q:
        needle = q.strip().lower()
        if needle:
            items = [
                e
                for e in items
                if needle in str(e.get("title") or "").lower()
                or needle in str(e.get("product_id") or "")
                or needle in str(e.get("supplier") or "")
            ]
    return items[: max(1, min(500, limit))]


def extract_products_from_tool_result(result: dict[str, Any]) -> list[dict[str, Any]]:
    data = result.get("data")
    if not isinstance(data, dict):
        return []
    for key in ("similar_products", "compare_products", "products"):
        raw = data.get(key)
        if isinstance(raw, list) and raw:
            return [p for p in raw if isinstance(p, dict)]
    return []

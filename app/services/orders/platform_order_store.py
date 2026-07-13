"""1688 订单回传快照（按 ord_line_no 索引）。"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app.core.paths import data_dir

_STORE_PATH = data_dir() / "orders" / "platform-orders.json"
_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_unlocked() -> dict[str, dict[str, Any]]:
    if not _STORE_PATH.exists():
        return {}
    try:
        raw = json.loads(_STORE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    lines = raw.get("lines") if isinstance(raw, dict) else None
    if not isinstance(lines, dict):
        return {}
    return {str(k): v for k, v in lines.items() if isinstance(v, dict) and k}


def _save_unlocked(lines: dict[str, dict[str, Any]]) -> None:
    _STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "updated_at": _now_iso(), "lines": lines}
    _STORE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def get_platform_order(ord_line_no: str) -> Optional[dict[str, Any]]:
    key = (ord_line_no or "").strip()
    if not key:
        return None
    with _lock:
        return _load_unlocked().get(key)


def upsert_platform_orders(records: dict[str, dict[str, Any]]) -> int:
    if not records:
        return 0
    now = _now_iso()
    with _lock:
        lines = _load_unlocked()
        updated = 0
        for key, record in records.items():
            key = str(key or "").strip()
            if not key or not isinstance(record, dict):
                continue
            prev = lines.get(key) or {}
            merged = {**prev, **record, "ord_line_no": key, "_platform_synced_at": now}
            lines[key] = merged
            updated += 1
        if updated:
            _save_unlocked(lines)
            try:
                from app.services.command_center.scan_cache import invalidate_command_center_scan

                invalidate_command_center_scan()
            except Exception:
                pass
        return updated


def list_platform_orders(*, limit: int = 200) -> list[dict[str, Any]]:
    with _lock:
        items = list(_load_unlocked().values())
    items.sort(key=lambda x: str(x.get("_platform_synced_at") or ""), reverse=True)
    return items[: max(1, limit)]

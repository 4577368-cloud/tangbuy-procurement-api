"""订单子单本地缓存（服务端持久化，支持分批增量合并）。"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from app.core.paths import data_dir
from app.services.orders.queue_filters import resolve_order_queue

_CACHE_PATH = data_dir() / "orders" / "line-cache.json"
_STATE_PATH = data_dir() / "orders" / "sync-state.json"
_io_lock = threading.Lock()

SYNC_QUEUES = (
    "pending_procurement",
    "pending_payment",
    "ordered",
    "shipped",
    "in_warehouse",
    "dispatched",
    "exception",
    "reverse",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _default_state() -> dict[str, Any]:
    return {
        "version": 1,
        "last_incremental_at": None,
        "last_backfill_at": None,
        "backfill_queue_index": 0,
        "backfill_page": 1,
        "backfill_complete": False,
        "cached_total": 0,
    }


def _load_json(path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_json(path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=f"{path.stem}-", suffix=".json.tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def load_sync_state() -> dict[str, Any]:
    with _io_lock:
        raw = _load_json(_STATE_PATH)
        state = _default_state()
        state.update({k: v for k, v in raw.items() if k in state or k == "version"})
        state["cached_total"] = len(_load_cache_lines_unlocked())
        return state


def save_sync_state(state: dict[str, Any]) -> None:
    with _io_lock:
        state = {**state, "cached_total": len(_load_cache_lines_unlocked())}
        _save_json(_STATE_PATH, state)


def _load_cache_lines_unlocked() -> dict[str, dict[str, Any]]:
    raw = _load_json(_CACHE_PATH)
    lines = raw.get("lines")
    if not isinstance(lines, dict):
        return {}
    return {str(k): v for k, v in lines.items() if isinstance(v, dict) and k}


def load_all_lines() -> dict[str, dict[str, Any]]:
    with _io_lock:
        return _load_cache_lines_unlocked()


def list_cached_lines(*, queue: Optional[str] = None) -> list[dict[str, Any]]:
    lines = load_all_lines().values()
    if queue and queue != "all":
        lines = [ln for ln in lines if resolve_order_queue(ln) == queue]
    return sorted(
        lines,
        key=lambda r: str(r.get("pay_time") or ""),
        reverse=True,
    )


def _status_fingerprint(row: dict[str, Any]) -> str:
    keys = (
        "ord_line_stat",
        "ord_stat",
        "rtn_stat",
        "abn_type_cd",
        "cfm_stat",
        "pur_prc",
        "post_fee",
        "ds_ord_amt",
        "ti_status",
        "to_status",
        "splr_item_id",
        "item_nm",
    )
    return "|".join(f"{k}={row.get(k)!r}" for k in keys)


def merge_lines(incoming: list[dict[str, Any]]) -> dict[str, int]:
    """合并一批子单；返回 added / updated / unchanged。"""
    stats = {"added": 0, "updated": 0, "unchanged": 0}
    if not incoming:
        return stats

    now = _now_iso()
    with _io_lock:
        lines = _load_cache_lines_unlocked()
        for row in incoming:
            if not isinstance(row, dict):
                continue
            key = str(row.get("ord_line_no") or "").strip()
            if not key:
                continue
            prev = lines.get(key)
            fp = _status_fingerprint(row)
            if not prev:
                lines[key] = {**row, "_cache_updated_at": now}
                stats["added"] += 1
                continue
            if _status_fingerprint(prev) == fp:
                stats["unchanged"] += 1
                continue
            lines[key] = {**row, "_cache_updated_at": now}
            stats["updated"] += 1

        _save_json(
            _CACHE_PATH,
            {"version": 1, "updated_at": now, "lines": lines},
        )
    return stats

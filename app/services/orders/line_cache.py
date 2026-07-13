"""订单子单本地缓存（DB 或 JSON 文件）。"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from app.core.paths import data_dir
from app.db.session import db_session, is_db_enabled
from app.services.orders.queue_filters import resolve_order_queue
from app.services.orders.snapshot_merge import merge_admin_snapshot, status_fingerprint

_CACHE_PATH = data_dir() / "orders" / "line-cache.json"
_STATE_PATH = data_dir() / "orders" / "sync-state.json"
_io_lock = threading.Lock()
_merge_lock = threading.Lock()

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
        "backfill_filter_index": 0,
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


def _snapshot_count() -> int:
    from app.db.ops_repos import SnapshotReadRepository

    with db_session() as session:
        return SnapshotReadRepository(session).count()


def _load_sync_state_db(cached_total: Optional[int] = None) -> dict[str, Any]:
    from app.db.catalog_repos import ConfigRepository

    with db_session() as session:
        raw = ConfigRepository(session).load_document(ConfigRepository.SYNC_STATE_KEY) or {}
    state = _default_state()
    state.update({k: v for k, v in raw.items() if k in state or k == "version" or k == "backfill_done_queues"})
    state["cached_total"] = cached_total if cached_total is not None else _snapshot_count()
    return state


def _save_sync_state_db(state: dict[str, Any]) -> None:
    from app.db.catalog_repos import ConfigRepository

    with db_session() as session:
        ConfigRepository(session).save_document(ConfigRepository.SYNC_STATE_KEY, state)


def load_sync_state(cached_total: Optional[int] = None) -> dict[str, Any]:
    """cached_total 已知时传入，避免为计数再次全表 load。"""
    if is_db_enabled():
        return _load_sync_state_db(cached_total)
    with _io_lock:
        raw = _load_json(_STATE_PATH)
        state = _default_state()
        state.update({k: v for k, v in raw.items() if k in state or k == "version"})
        state["cached_total"] = (
            cached_total if cached_total is not None else len(_load_cache_lines_unlocked())
        )
        return state


def save_sync_state(state: dict[str, Any]) -> None:
    state = {**state, "cached_total": len(load_all_lines())}
    if is_db_enabled():
        _save_sync_state_db(state)
        return
    with _io_lock:
        _save_json(_STATE_PATH, state)


def _load_cache_lines_unlocked() -> dict[str, dict[str, Any]]:
    raw = _load_json(_CACHE_PATH)
    lines = raw.get("lines")
    if not isinstance(lines, dict):
        return {}
    return {str(k): v for k, v in lines.items() if isinstance(v, dict) and k}


def load_all_lines() -> dict[str, dict[str, Any]]:
    if is_db_enabled():
        from app.db.ops_repos import SnapshotReadRepository

        with db_session() as session:
            return SnapshotReadRepository(session).load_all_dict()
    with _io_lock:
        return _load_cache_lines_unlocked()


def get_line(ord_line_no: str) -> Optional[dict[str, Any]]:
    """读取单条子单快照，避免为点查加载全表。"""
    key = (ord_line_no or "").strip()
    if not key:
        return None
    if is_db_enabled():
        from app.db.repositories import SnapshotRepository

        with db_session() as session:
            return SnapshotRepository(session).get_row(key)
    with _io_lock:
        return _load_cache_lines_unlocked().get(key)


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
    return status_fingerprint(row)


def apply_category_overlay_to_lines(
    ord_line_nos: list[str],
    hs: dict[str, Any],
    *,
    source: str = "category_writeback",
) -> int:
    """品类写回 Admin 后同步锁定到本地子单快照。"""
    from app.services.orders.snapshot_merge import apply_category_overlay, build_category_overlay

    if not ord_line_nos or not hs:
        return 0
    overlay = build_category_overlay(hs, source=source, at=_now_iso())
    updated = 0
    if is_db_enabled():
        from app.db.repositories import SnapshotRepository

        with db_session() as session:
            repo = SnapshotRepository(session)
            for key in ord_line_nos:
                key = str(key or "").strip()
                if not key:
                    continue
                row = repo.get_row(key)
                if not row:
                    continue
                patched = apply_category_overlay(row, overlay)
                repo.upsert(
                    patched,
                    fingerprint=_status_fingerprint(patched),
                    queue=resolve_order_queue(patched),
                )
                updated += 1
        return updated

    with _io_lock:
        lines = _load_cache_lines_unlocked()
        for key in ord_line_nos:
            key = str(key or "").strip()
            if not key or key not in lines:
                continue
            lines[key] = apply_category_overlay(lines[key], overlay)
            updated += 1
        if updated:
            _save_json(_CACHE_PATH, {"version": 1, "updated_at": _now_iso(), "lines": lines})
    return updated


def merge_lines(incoming: list[dict[str, Any]]) -> dict[str, int]:
    """合并一批子单；返回 added / updated / unchanged。"""
    stats = {"added": 0, "updated": 0, "unchanged": 0}
    if not incoming:
        return stats

    now = _now_iso()
    if is_db_enabled():
        from app.db.repositories import SnapshotRepository

        with _merge_lock:
            with db_session() as session:
                from app.db.ops_repos import SnapshotReadRepository

                repo = SnapshotRepository(session)
                read_repo = SnapshotReadRepository(session)
                keys = [
                    str(row.get("ord_line_no") or "").strip()
                    for row in incoming
                    if isinstance(row, dict) and str(row.get("ord_line_no") or "").strip()
                ]
                existing = read_repo.load_by_keys(keys)
                for row in incoming:
                    if not isinstance(row, dict):
                        continue
                    key = str(row.get("ord_line_no") or "").strip()
                    if not key:
                        continue
                    prev = existing.get(key)
                    fp = _status_fingerprint(row)
                    merged_row = merge_admin_snapshot(prev, row)
                    merged_row["_admin_synced_at"] = now
                    if not prev:
                        stats["added"] += 1
                    elif _status_fingerprint(prev) == fp:
                        stats["unchanged"] += 1
                    else:
                        stats["updated"] += 1
                    merged_row = {**merged_row, "_cache_updated_at": now}
                    repo.upsert(merged_row, fingerprint=fp, queue=resolve_order_queue(merged_row))
                    existing[key] = merged_row
        if stats["added"] or stats["updated"]:
            try:
                from app.services.command_center.scan_cache import invalidate_command_center_scan

                invalidate_command_center_scan()
            except Exception:
                pass
        return stats

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
            merged_row = merge_admin_snapshot(prev, row)
            merged_row["_admin_synced_at"] = now
            if not prev:
                stats["added"] += 1
            elif _status_fingerprint(prev) == fp:
                stats["unchanged"] += 1
            else:
                stats["updated"] += 1
            lines[key] = {**merged_row, "_cache_updated_at": now}

        _save_json(
            _CACHE_PATH,
            {"version": 1, "updated_at": now, "lines": lines},
        )
        if stats["added"] or stats["updated"]:
            try:
                from app.services.command_center.scan_cache import invalidate_command_center_scan

                invalidate_command_center_scan()
            except Exception:
                pass
    return stats

"""后台订单同步：启动增量 + 全量扫描保证完整性。"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from app.db.session import is_db_enabled

_log = logging.getLogger(__name__)

_thread: Optional[threading.Thread] = None
_stop = threading.Event()

INCREMENTAL_INTERVAL_SEC = 300
STARTUP_FULL_SCAN_PAGE_SIZE = 200


def _run_scheduled_sync(mode: str, **kwargs) -> None:
    from app.services.orders.sync_jobs import execute_order_sync

    try:
        result = execute_order_sync(mode=mode, source="scheduler", **kwargs)
        if result.get("skipped"):
            _log.info("order sync %s skipped: %s", mode, result.get("error"))
            return
        stats = result.get("stats") or {}
        _log.info(
            "order sync %s: cache=%s scanned=%s pages=%s ok=%s",
            mode,
            result.get("cache_total"),
            stats.get("scanned"),
            stats.get("pages"),
            result.get("ok"),
        )
        errors = result.get("errors")
        if errors:
            _log.warning("order sync %s errors: %s", mode, errors[:5])
    except Exception as exc:
        _log.warning("order sync %s failed: %s", mode, exc)


def _sync_loop() -> None:
    from app.services.orders import line_cache

    # 1) 启动先增量同步（快速获取最近数据）
    _run_scheduled_sync("incremental")

    # 2) 未完成 backfill 时才全量扫描，避免与 API 读请求争用 SQLite
    state = line_cache.load_sync_state()
    if not state.get("backfill_complete"):
        _run_scheduled_sync("full", page_size=STARTUP_FULL_SCAN_PAGE_SIZE)

    # 3) 定期增量同步
    while not _stop.is_set():
        _stop.wait(INCREMENTAL_INTERVAL_SEC)
        if _stop.is_set():
            break
        _run_scheduled_sync("incremental")


def start_order_sync_background() -> None:
    global _thread
    if not is_db_enabled():
        return
    from app.core.config import get_settings

    if not get_settings().tangbuy_admin_configured:
        return
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_sync_loop, daemon=True, name="order-sync")
    _thread.start()


def stop_order_sync_background() -> None:
    _stop.set()

"""订单同步全局互斥 — scheduler 与手动 POST /sync 共用。"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Optional

_log = logging.getLogger(__name__)

_sync_lock = threading.Lock()
_meta_lock = threading.Lock()
_holder: Optional[str] = None


def sync_in_progress() -> bool:
    with _meta_lock:
        return _holder is not None


def current_sync_holder() -> Optional[str]:
    with _meta_lock:
        return _holder


def run_exclusive_sync(
    fn: Callable[[], dict[str, Any]],
    *,
    source: str,
) -> dict[str, Any]:
    if not _sync_lock.acquire(blocking=False):
        holder = current_sync_holder()
        _log.info("order sync skipped (%s): already running (%s)", source, holder)
        return {
            "ok": False,
            "skipped": True,
            "error": f"订单同步进行中（{holder or 'unknown'}）",
        }
    with _meta_lock:
        global _holder
        _holder = source
    try:
        return fn()
    finally:
        with _meta_lock:
            _holder = None
        _sync_lock.release()

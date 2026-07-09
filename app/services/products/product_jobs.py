"""商品备选扫描队列：手动批次 + 每日配额。"""

from __future__ import annotations

import json
import logging
import threading
from collections import deque
from datetime import date
from pathlib import Path
from typing import Any, Optional

from apscheduler.schedulers.background import BackgroundScheduler

from app.core.config import get_settings
from app.core.paths import data_dir
from app.services.products.alternative_scan import scan_product_alternatives
from app.services.products.enrichment import enrich_product_by_id
from app.services.products.store import load_products

_log = logging.getLogger(__name__)

# (product_id, do_enrich, do_scan, refresh)
_queue: deque[tuple[str, bool, bool, bool]] = deque()
_queued: set[str] = set()
_lock = threading.Lock()
_active = 0
_MAX_WORKERS = 2
_scheduler: Optional[BackgroundScheduler] = None

_QUOTA_PATH = data_dir() / "products" / "alt-scan-daily.json"
DEFAULT_DAILY_LIMIT = 15
DEFAULT_BATCH_SIZE = 3


def _today() -> str:
    return date.today().isoformat()


def _load_quota() -> dict[str, Any]:
    today = _today()
    if _QUOTA_PATH.exists():
        try:
            data = json.loads(_QUOTA_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("date") == today:
                return {
                    "date": today,
                    "count": int(data.get("count") or 0),
                    "product_ids": list(data.get("product_ids") or []),
                }
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass
    return {"date": today, "count": 0, "product_ids": []}


def _save_quota(data: dict[str, Any]) -> None:
    _QUOTA_PATH.parent.mkdir(parents=True, exist_ok=True)
    _QUOTA_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def get_alt_scan_quota_status() -> dict[str, Any]:
    settings = get_settings()
    daily = settings.product_alt_scan_daily_limit
    q = _load_quota()
    used = int(q.get("count") or 0)
    unlimited = daily <= 0
    return {
        "date": q["date"],
        "used": used,
        "daily_limit": daily,
        "unlimited": unlimited,
        "remaining": 10**9 if unlimited else max(0, daily - used),
        "batch_size": settings.product_alt_scan_batch_size,
        "queue_size": len(_queue) + _active,
    }


def _consume_quota(product_id: str) -> bool:
    """占用 1 个日配额；daily_limit<=0 时不限制。已扫过同日同商品不重复计。"""
    settings = get_settings()
    daily = settings.product_alt_scan_daily_limit
    with _lock:
        if daily <= 0:
            return True
        q = _load_quota()
        ids: list[str] = list(q.get("product_ids") or [])
        if product_id in ids:
            return True
        if int(q.get("count") or 0) >= daily:
            return False
        ids.append(product_id)
        q["count"] = len(ids)
        q["product_ids"] = ids
        q["date"] = _today()
        _save_quota(q)
        return True


def _enqueue(
    product_id: str,
    *,
    do_enrich: bool = False,
    do_scan: bool = True,
    refresh: bool = False,
) -> bool:
    pid = (product_id or "").strip()
    if not pid or not (do_enrich or do_scan):
        return False
    with _lock:
        if pid in _queued:
            return False
        _queued.add(pid)
        _queue.append((pid, do_enrich, do_scan, refresh))
    _pump()
    return True


def _pump() -> None:
    global _active
    while True:
        with _lock:
            if _active >= _MAX_WORKERS or not _queue:
                return
            pid, do_enrich, do_scan, refresh = _queue.popleft()
            _active += 1
        threading.Thread(
            target=_run_one,
            args=(pid,),
            kwargs={"do_enrich": do_enrich, "do_scan": do_scan, "refresh": refresh},
            daemon=True,
            name=f"product-pipeline-{pid[:8]}",
        ).start()


def _run_one(
    pid: str,
    *,
    do_enrich: bool = True,
    do_scan: bool = True,
    refresh: bool = False,
) -> None:
    global _active
    try:
        if do_enrich:
            try:
                enrich_product_by_id(pid)
            except Exception as exc:
                _log.warning("product enrich failed %s: %s", pid, exc)
        if do_scan:
            try:
                scan_product_alternatives(pid, refresh=refresh)
            except Exception as exc:
                _log.warning("product alt scan failed %s: %s", pid, exc)
    finally:
        with _lock:
            _queued.discard(pid)
            _active = max(0, _active - 1)
        _pump()


def schedule_product_pipeline(product_id: str) -> None:
    """入库后默认 enrich（Portal 自动匹配）；备选扫描按配置可选。"""
    settings = get_settings()
    if not settings.product_auto_pipeline:
        return
    pid = (product_id or "").strip()
    if not pid:
        return

    from app.services.products.store import update_product

    update_product(pid, lambda p: {**p, "enrichment_status": "running"})

    def _run() -> None:
        try:
            enrich_product_by_id(pid)
        except Exception as exc:
            _log.warning("product enrich failed %s: %s", pid, exc)
            try:
                from app.services.products.enrichment import mark_pending_match

                mark_pending_match(pid, reason=str(exc))
            except Exception:
                pass
        if settings.product_auto_scan_on_create:
            status = get_alt_scan_quota_status()
            if status["remaining"] <= 0:
                return
            if _consume_quota(pid):
                _enqueue(pid)

    threading.Thread(target=_run, daemon=True, name=f"product-enrich-{pid[:8]}").start()


def resume_stale_enrichments(*, limit: int = 40) -> int:
    """把卡住的匹配中 / 未匹配订单商品重新入队自动匹配。"""
    settings = get_settings()
    if not settings.product_auto_pipeline:
        return 0
    started = 0
    for p in load_products():
        if started >= limit:
            break
        if p.get("source") == "find":
            continue
        pid = str(p.get("tangbuy_product_id") or "").strip()
        if not pid:
            continue
        st = p.get("enrichment_status")
        if st in ("matched", "done", "partial"):
            continue
        if st not in (None, "running", "pending_match", "failed", "pending"):
            continue
        from app.services.products.store import update_product

        update_product(pid, lambda row: {**row, "enrichment_status": "running"})
        if _enqueue(pid, do_enrich=True, do_scan=False):
            started += 1
    if started:
        _log.info("[product-enrich] resumed %s stale products", started)
    return started


def _pending_scan_candidates() -> list[str]:
    out: list[str] = []
    for p in load_products():
        pid = str(p.get("tangbuy_product_id") or "").strip()
        if not pid:
            continue
        status = p.get("alt_supplier_scan_status")
        alts = p.get("alternative_suppliers") or []
        if status == "running":
            continue
        if status == "done" and isinstance(alts, list) and len(alts) > 0:
            continue
        with _lock:
            if pid in _queued:
                continue
        out.append(pid)
    return out


def enqueue_alt_scan_batch(*, batch_size: Optional[int] = None) -> dict[str, Any]:
    """
    批量入队待扫备选：本轮最多 batch_size（默认配置）。
    daily_limit<=0 时不限制当日次数。
    """
    settings = get_settings()
    batch = batch_size if batch_size is not None else settings.product_alt_scan_batch_size
    batch = max(1, min(batch, 10))
    status = get_alt_scan_quota_status()
    remaining = status["remaining"]
    if remaining <= 0:
        return {
            "ok": False,
            "enqueued": 0,
            "product_ids": [],
            "message": f"今日配额已用尽（{status['used']}/{status['daily_limit']}）",
            "quota": status,
        }

    take = min(batch, remaining)
    candidates = _pending_scan_candidates()
    if not candidates:
        return {
            "ok": True,
            "enqueued": 0,
            "product_ids": [],
            "message": "暂无可扫",
            "quota": status,
        }

    enqueued_ids: list[str] = []
    for pid in candidates:
        if len(enqueued_ids) >= take:
            break
        if not _consume_quota(pid):
            break
        if _enqueue(pid, do_enrich=False, do_scan=True):
            enqueued_ids.append(pid)

    new_status = get_alt_scan_quota_status()
    if new_status.get("unlimited"):
        msg = f"已入队 {len(enqueued_ids)} 个"
    else:
        msg = f"已入队 {len(enqueued_ids)} 个 · 今日 {new_status['used']}/{new_status['daily_limit']}"
    return {
        "ok": True,
        "enqueued": len(enqueued_ids),
        "product_ids": enqueued_ids,
        "message": msg,
        "quota": new_status,
    }


def enqueue_alt_scan_product(
    product_id: str,
    *,
    refresh: bool = False,
) -> dict[str, Any]:
    """单商品备选扫描：仅扫该商品。refresh=屏蔽上一轮并重取。"""
    from app.services.products.store import get_product_by_id, update_product

    settings = get_settings()
    pid = (product_id or "").strip()
    if not pid:
        return {"ok": False, "enqueued": 0, "message": "缺少商品 ID"}
    product = get_product_by_id(pid)
    if not product:
        return {"ok": False, "enqueued": 0, "message": "商品不存在", "product_id": pid}

    status = get_alt_scan_quota_status()
    if (
        not status.get("unlimited")
        and status["remaining"] <= 0
        and pid not in (_load_quota().get("product_ids") or [])
    ):
        return {
            "ok": False,
            "enqueued": 0,
            "product_ids": [],
            "message": f"今日配额已用尽（{status['used']}/{status['daily_limit']}）",
            "quota": status,
            "product_id": pid,
        }

    if product.get("alt_supplier_scan_status") == "running":
        return {
            "ok": True,
            "enqueued": 0,
            "product_ids": [pid],
            "message": "扫描中",
            "quota": status,
            "product_id": pid,
        }

    if not _consume_quota(pid):
        new_status = get_alt_scan_quota_status()
        return {
            "ok": False,
            "enqueued": 0,
            "product_ids": [],
            "message": f"今日配额已用尽（{new_status['used']}/{new_status['daily_limit']}）",
            "quota": new_status,
            "product_id": pid,
        }

    with _lock:
        if pid in _queued:
            return {
                "ok": True,
                "enqueued": 0,
                "product_ids": [pid],
                "message": "扫描中",
                "quota": get_alt_scan_quota_status(),
                "product_id": pid,
            }
        _queued.add(pid)

    update_product(
        pid,
        lambda p: {
            **p,
            "alt_supplier_scan_status": "running",
            "alt_supplier_scan_refresh": bool(refresh),
        },
    )
    new_status = get_alt_scan_quota_status()

    if not settings.product_alt_scan_sync:
        with _lock:
            _queued.discard(pid)
        if _enqueue(pid, do_enrich=False, do_scan=True, refresh=refresh):
            msg = "已刷新入队" if refresh else "已入队"
            if not new_status.get("unlimited"):
                msg = f"{msg} · 今日 {new_status['used']}/{new_status['daily_limit']}"
            return {
                "ok": True,
                "enqueued": 1,
                "product_ids": [pid],
                "message": msg,
                "quota": new_status,
                "product_id": pid,
                "refresh": bool(refresh),
            }
        return {
            "ok": True,
            "enqueued": 0,
            "product_ids": [pid],
            "message": "扫描中",
            "quota": get_alt_scan_quota_status(),
            "product_id": pid,
        }

    try:
        scan_result = scan_product_alternatives(pid, refresh=refresh)
    finally:
        with _lock:
            _queued.discard(pid)

    msg = "扫描完成" if scan_result.get("ok") else (scan_result.get("error") or "扫描失败")
    if scan_result.get("ok") and not new_status.get("unlimited"):
        msg = f"已找到 {scan_result.get('alternatives', 0)} 个 · 今日 {new_status['used']}/{new_status['daily_limit']}"
    return {
        "ok": bool(scan_result.get("ok")),
        "enqueued": 1,
        "product_ids": [pid],
        "message": msg,
        "quota": new_status,
        "product_id": pid,
        "refresh": bool(refresh),
        "product": scan_result.get("product"),
        "alternatives": scan_result.get("alternatives"),
        "error": scan_result.get("error"),
    }


def sweep_pending_alt_scans(*, limit: int = 20) -> int:
    """周期任务：仍受日配额限制。"""
    settings = get_settings()
    if not settings.product_auto_pipeline:
        return 0
    result = enqueue_alt_scan_batch(batch_size=min(limit, settings.product_alt_scan_batch_size))
    return int(result.get("enqueued") or 0)


def start_product_auto_scan() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    settings = get_settings()
    interval_ms = settings.product_auto_scan_ms
    if interval_ms <= 0:
        _log.info("[product-auto-scan] 周期扫描已关闭（用「更新备选」手动触发）")
        return

    def tick() -> None:
        try:
            n = sweep_pending_alt_scans(limit=settings.product_auto_scan_batch)
            if n:
                _log.info("[product-auto-scan] 入队 %s 条待扫商品", n)
        except Exception:
            _log.exception("[product-auto-scan] 周期扫描失败")

    _scheduler = BackgroundScheduler()
    _scheduler.add_job(
        tick,
        "interval",
        seconds=max(interval_ms / 1000, 30),
        id="product-alt-scan",
        max_instances=1,
    )
    _scheduler.start()
    _log.info("[product-auto-scan] 已启动，每 %sms", interval_ms)


def stop_product_auto_scan() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None

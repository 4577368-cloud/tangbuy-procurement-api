"""任务中心后台轮询。"""

from __future__ import annotations

import logging
import os

from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler

from app.services.tasks import store

logger = logging.getLogger(__name__)
_scheduler: Optional[BackgroundScheduler] = None


def start_task_auto_refresh() -> None:
    global _scheduler
    if _scheduler is not None:
        return

    raw = os.environ.get("TASK_AUTO_REFRESH_MS", "180000")
    try:
        interval = int(raw)
    except ValueError:
        interval = 180_000

    if interval <= 0:
        logger.info("[task-auto-refresh] 已禁用（TASK_AUTO_REFRESH_MS<=0）")
        return

    def tick() -> None:
        try:
            updated = store.refresh_all_active_newton_tasks()
            if updated:
                logger.info("[task-auto-refresh] 刷新 %s 条进行中长程任务", len(updated))
        except Exception:
            logger.exception("[task-auto-refresh] 刷新失败")

    _scheduler = BackgroundScheduler()
    _scheduler.add_job(tick, "interval", seconds=max(interval / 1000, 1), id="newton-task-refresh")
    _scheduler.start()
    logger.info("[task-auto-refresh] 已启动，每 %sms 刷新一次", interval)


def stop_task_auto_refresh() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None

"""订单同步互斥锁测试。"""

from __future__ import annotations

import threading

from app.services.orders.sync_coordinator import (
    current_sync_holder,
    run_exclusive_sync,
    sync_in_progress,
)


def test_run_exclusive_sync_serializes():
    order: list[str] = []
    started = threading.Event()
    release = threading.Event()

    def slow_sync() -> dict:
        order.append("start")
        started.set()
        release.wait(timeout=2)
        order.append("end")
        return {"ok": True}

    def contender() -> dict:
        started.wait(timeout=2)
        return run_exclusive_sync(lambda: {"ok": True}, source="contender")

    t = threading.Thread(
        target=lambda: run_exclusive_sync(slow_sync, source="primary"),
    )
    t.start()
    started.wait(timeout=2)
    assert sync_in_progress() is True
    assert current_sync_holder() == "primary"

    result = contender()
    assert result.get("skipped") is True
    release.set()
    t.join(timeout=2)
    assert order == ["start", "end"]
    assert sync_in_progress() is False


def test_run_exclusive_sync_allows_sequential():
    calls = {"n": 0}

    def once() -> dict:
        calls["n"] += 1
        return {"ok": True}

    a = run_exclusive_sync(once, source="a")
    b = run_exclusive_sync(once, source="b")
    assert a.get("ok") is True
    assert b.get("ok") is True
    assert calls["n"] == 2

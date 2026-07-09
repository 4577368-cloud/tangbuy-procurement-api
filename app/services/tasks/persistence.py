"""任务持久化（共用 data/agent/tasks.json）。"""

from __future__ import annotations

import fcntl
import json
import re
from pathlib import Path
from typing import Any, Callable, Optional

from app.core.paths import data_dir

RUNTIME_TASK_ID_RE = re.compile(r"^task-(newton|inq|src|ord|sc)-\d{10,}-[a-z0-9]+$")
STORE_PATH = data_dir() / "agent" / "tasks.json"

ORDER_FOLLOWUP_GATEWAY_SUMMARY = "已直发商家，回复请在旺旺或订单页查看"


def is_runtime_task(task: dict[str, Any]) -> bool:
    return bool(RUNTIME_TASK_ID_RE.match(task.get("id", "")))


def _ensure_dir() -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)


def _parse_tasks(raw: str) -> list[dict[str, Any]]:
    if not raw.strip():
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    tasks = [t for t in parsed if isinstance(t, dict) and is_runtime_task(t)]
    tasks.sort(key=lambda t: t.get("created_at", ""), reverse=True)
    return tasks


def _write_tasks_locked(handle, tasks: list[dict[str, Any]]) -> None:
    runtime = [t for t in tasks if is_runtime_task(t)]
    runtime.sort(key=lambda t: t.get("created_at", ""), reverse=True)
    payload = json.dumps(runtime, ensure_ascii=False, indent=2) + "\n"
    handle.seek(0)
    handle.write(payload)
    handle.truncate()
    handle.flush()


def with_runtime_tasks(
    mutator: Optional[Callable[[list[dict[str, Any]]], None]] = None,
    *,
    repair: bool = True,
) -> list[dict[str, Any]]:
    """在文件锁内读取/修复/修改/写回 runtime 任务，避免并发覆盖丢数据。"""
    _ensure_dir()
    STORE_PATH.touch(exist_ok=True)
    with open(STORE_PATH, "r+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.seek(0)
            tasks = _parse_tasks(handle.read())
            dirty = False
            if repair:
                dirty = any(repair_order_followup_task(t) for t in tasks)
            if mutator is not None:
                mutator(tasks)
                dirty = True
            if dirty or mutator is not None:
                _write_tasks_locked(handle, tasks)
            return tasks
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_runtime_tasks() -> list[dict[str, Any]]:
    return with_runtime_tasks(repair=False)


def save_runtime_tasks(tasks: list[dict[str, Any]]) -> None:
    """按 id 合并写回，避免 list/refresh 的过期快照冲掉刚 append 的任务。"""
    snapshot_by_id = {
        str(t.get("id")): t
        for t in tasks
        if isinstance(t, dict) and is_runtime_task(t) and t.get("id")
    }

    def mutator(current: list[dict[str, Any]]) -> None:
        merged = {str(t.get("id")): t for t in current if t.get("id")}
        merged.update(snapshot_by_id)
        current.clear()
        current.extend(merged.values())

    with_runtime_tasks(mutator, repair=False)


def repair_order_followup_task(task: dict[str, Any]) -> bool:
    if task.get("type") != "order_followup":
        return False
    payload = task.setdefault("payload", {})
    if not payload.get("gateway_sent"):
        return False

    changed = False
    if task.get("status") != "completed":
        task["status"] = "completed"
        changed = True

    timeline = task.get("timeline") or []
    gateway_evt = next((e for e in timeline if e.get("label") == "已改走网关"), None)
    filtered = [e for e in timeline if e.get("label") != "平台终止"]
    if len(filtered) != len(timeline):
        task["timeline"] = filtered
        changed = True

    summary = task.get("result_summary") or ""
    if not summary or "未联系到商家" in summary or "RuntimeExecutor" in summary:
        task["result_summary"] = ORDER_FOLLOWUP_GATEWAY_SUMMARY
        changed = True

    if payload.get("error_message"):
        payload.pop("error_message", None)
        changed = True

    if not task.get("completed_at"):
        task["completed_at"] = (gateway_evt or {}).get("at") or task.get("updated_at")
        changed = True

    return changed


def load_and_repair() -> list[dict[str, Any]]:
    return with_runtime_tasks()


def append_runtime_task(task: dict[str, Any]) -> list[dict[str, Any]]:
    if not is_runtime_task(task):
        return load_and_repair()

    def mutator(tasks: list[dict[str, Any]]) -> None:
        tasks.insert(0, task)

    return with_runtime_tasks(mutator)

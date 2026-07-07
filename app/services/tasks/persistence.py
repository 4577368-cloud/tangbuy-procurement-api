"""任务持久化（共用 data/agent/tasks.json）。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.core.paths import data_dir

RUNTIME_TASK_ID_RE = re.compile(r"^task-(newton|inq|src|ord|sc)-\d{10,}-[a-z0-9]+$")
STORE_PATH = data_dir() / "agent" / "tasks.json"

ORDER_FOLLOWUP_GATEWAY_SUMMARY = "已直发商家，回复请在旺旺或订单页查看"


def is_runtime_task(task: dict[str, Any]) -> bool:
    return bool(RUNTIME_TASK_ID_RE.match(task.get("id", "")))


def _ensure_dir() -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)


def load_runtime_tasks() -> list[dict[str, Any]]:
    _ensure_dir()
    if not STORE_PATH.exists():
        return []
    try:
        parsed = json.loads(STORE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(parsed, list):
        return []
    tasks = [t for t in parsed if isinstance(t, dict) and is_runtime_task(t)]
    tasks.sort(key=lambda t: t.get("created_at", ""), reverse=True)
    return tasks


def save_runtime_tasks(tasks: list[dict[str, Any]]) -> None:
    _ensure_dir()
    runtime = [t for t in tasks if is_runtime_task(t)]
    tmp = STORE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(runtime, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(STORE_PATH)


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
    tasks = load_runtime_tasks()
    dirty = any(repair_order_followup_task(t) for t in tasks)
    if dirty:
        save_runtime_tasks(tasks)
    return tasks

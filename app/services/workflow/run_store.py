"""WorkflowRun 持久化（DB 或 JSONL）。"""

from __future__ import annotations

import fcntl
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app.core.paths import data_dir
from app.db.session import db_session, is_db_enabled

_RUN_PATH = data_dir() / "workflow" / "workflow-runs.jsonl"


def _ensure_dir() -> None:
    _RUN_PATH.parent.mkdir(parents=True, exist_ok=True)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []


def _append_line(path: Path, record: dict[str, Any]) -> None:
    _ensure_dir()
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.write(line)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _latest_by_ord_line(path: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for raw in _read_lines(path):
        raw = raw.strip()
        if not raw:
            continue
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            key = str(item.get("ord_line_no") or "").strip()
            if key:
                out[key] = item
    return out


def get_workflow_run(ord_line_no: str) -> Optional[dict[str, Any]]:
    key = ord_line_no.strip()
    if not key:
        return None
    if is_db_enabled():
        from app.db.workflow_repos import WorkflowRunRepository

        with db_session() as session:
            return WorkflowRunRepository(session).get(key)
    return _latest_by_ord_line(_RUN_PATH).get(key)


def save_workflow_run(run: dict[str, Any]) -> dict[str, Any]:
    key = str(run.get("ord_line_no") or "").strip()
    if not key:
        raise ValueError("ord_line_no required")
    run = {**run, "ord_line_no": key, "updated_at": _now_iso()}
    if not run.get("created_at"):
        run["created_at"] = run["updated_at"]
    if is_db_enabled():
        from app.db.workflow_repos import WorkflowRunRepository

        with db_session() as session:
            return WorkflowRunRepository(session).save(run)
    _append_line(_RUN_PATH, run)
    return run


def list_workflow_runs(*, limit: int = 200, status: Optional[str] = None) -> list[dict[str, Any]]:
    if is_db_enabled():
        from app.db.workflow_repos import WorkflowRunRepository

        with db_session() as session:
            return WorkflowRunRepository(session).list_runs(limit=limit, status=status)
    items = list(_latest_by_ord_line(_RUN_PATH).values())
    if status:
        items = [r for r in items if r.get("status") == status]
    items.sort(key=lambda x: str(x.get("updated_at") or ""), reverse=True)
    return items[: max(1, limit)]

"""轻量后台任务（进程内；重启后丢失）。"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional

_jobs: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()
_MAX_JOBS = 200


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _trim_jobs() -> None:
    if len(_jobs) <= _MAX_JOBS:
        return
    ordered = sorted(_jobs.values(), key=lambda j: str(j.get("created_at") or ""))
    for row in ordered[: len(_jobs) - _MAX_JOBS]:
        _jobs.pop(str(row.get("job_id") or ""), None)


def create_job(kind: str, *, label: str = "") -> str:
    job_id = f"job-{uuid.uuid4().hex[:12]}"
    with _lock:
        _trim_jobs()
        _jobs[job_id] = {
            "job_id": job_id,
            "kind": kind,
            "label": label,
            "status": "pending",
            "created_at": _now_iso(),
            "started_at": None,
            "finished_at": None,
            "result": None,
            "error": None,
        }
    return job_id


def get_job(job_id: str) -> Optional[dict[str, Any]]:
    key = (job_id or "").strip()
    if not key:
        return None
    with _lock:
        row = _jobs.get(key)
        return dict(row) if row else None


def run_job(job_id: str, fn: Callable[[], dict[str, Any]]) -> None:
    def _worker() -> None:
        with _lock:
            row = _jobs.get(job_id)
            if not row:
                return
            row["status"] = "running"
            row["started_at"] = _now_iso()
        try:
            result = fn()
            with _lock:
                row = _jobs.get(job_id)
                if not row:
                    return
                row["status"] = "done"
                row["result"] = result
                row["finished_at"] = _now_iso()
        except Exception as exc:
            with _lock:
                row = _jobs.get(job_id)
                if not row:
                    return
                row["status"] = "failed"
                row["error"] = str(exc)[:500]
                row["finished_at"] = _now_iso()

    threading.Thread(target=_worker, daemon=True, name=f"bg-{job_id}").start()

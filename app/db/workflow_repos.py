"""WorkflowRun 仓储。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.db.models import WorkflowRunRecord


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class WorkflowRunRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, ord_line_no: str) -> Optional[dict[str, Any]]:
        key = ord_line_no.strip()
        if not key:
            return None
        row = self.session.get(WorkflowRunRecord, key)
        if not row:
            return None
        data = dict(row.run_json or {})
        data.setdefault("ord_line_no", key)
        return data

    def save(self, run: dict[str, Any]) -> dict[str, Any]:
        key = str(run.get("ord_line_no") or "").strip()
        if not key:
            raise ValueError("ord_line_no required")
        now = _utcnow()
        row = self.session.get(WorkflowRunRecord, key)
        payload = {**run, "ord_line_no": key}
        if row is None:
            row = WorkflowRunRecord(
                ord_line_no=key,
                run_json=payload,
                crt_time=now,
                upd_time=now,
            )
            self.session.add(row)
        else:
            row.run_json = payload
            row.upd_time = now
        self.session.flush()
        return dict(row.run_json or {})

    def list_runs(self, *, limit: int = 200, status: Optional[str] = None) -> list[dict[str, Any]]:
        stmt = select(WorkflowRunRecord).order_by(desc(WorkflowRunRecord.upd_time)).limit(max(1, limit))
        rows = self.session.scalars(stmt).all()
        out: list[dict[str, Any]] = []
        for row in rows:
            data = dict(row.run_json or {})
            data.setdefault("ord_line_no", row.ord_line_no)
            if status and data.get("status") != status:
                continue
            out.append(data)
        return out

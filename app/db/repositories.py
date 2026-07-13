"""数据库仓储 — pipeline / 审计 / 快照。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.db.models import (
    OrdLineSnapshot,
    ProcurementAuditLog,
    ProcurementBlockerAck,
    ProcurementPipelineState,
    SyncCursor,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _pipeline_to_dict(row: ProcurementPipelineState) -> dict[str, Any]:
    return {
        "ord_line_no": row.ord_line_no,
        "pipeline_step": row.pipeline_step,
        "ord_line_stat": row.ord_line_stat,
        "blockers": row.pipeline_blockers if isinstance(row.pipeline_blockers, list) else [],
        "last_error": row.last_error,
        "last_run_at": row.last_run_at.isoformat() if row.last_run_at else None,
        "updated_at": row.upd_time.isoformat() if row.upd_time else None,
    }


class PipelineRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, ord_line_no: str) -> Optional[dict[str, Any]]:
        key = ord_line_no.strip()
        if not key:
            return None
        row = self.session.get(ProcurementPipelineState, key)
        return _pipeline_to_dict(row) if row else None

    def save(self, state: dict[str, Any]) -> dict[str, Any]:
        key = str(state.get("ord_line_no") or "").strip()
        if not key:
            raise ValueError("ord_line_no required")
        now = _utcnow()
        row = self.session.get(ProcurementPipelineState, key)
        blockers = state.get("blockers") if isinstance(state.get("blockers"), list) else []
        if row is None:
            row = ProcurementPipelineState(
                ord_line_no=key,
                pipeline_step=str(state.get("pipeline_step") or "prepare"),
                ord_line_stat=state.get("ord_line_stat"),
                pipeline_blockers=blockers,
                last_error=state.get("last_error"),
                last_run_at=_parse_dt(state.get("last_run_at")),
                upd_time=now,
            )
            self.session.add(row)
        else:
            row.pipeline_step = str(state.get("pipeline_step") or row.pipeline_step)
            row.ord_line_stat = state.get("ord_line_stat", row.ord_line_stat)
            row.pipeline_blockers = blockers
            row.last_error = state.get("last_error")
            row.last_run_at = _parse_dt(state.get("last_run_at")) or row.last_run_at
            row.upd_time = now
        self.session.flush()
        return _pipeline_to_dict(row)

    def list_states(self, *, limit: int = 500) -> list[dict[str, Any]]:
        stmt = (
            select(ProcurementPipelineState)
            .order_by(desc(ProcurementPipelineState.upd_time))
            .limit(max(1, limit))
        )
        return [_pipeline_to_dict(r) for r in self.session.scalars(stmt).all()]

    def latest_map(self) -> dict[str, dict[str, Any]]:
        rows = self.session.scalars(select(ProcurementPipelineState)).all()
        return {r.ord_line_no: _pipeline_to_dict(r) for r in rows}

    def ack_blocker(
        self,
        ord_line_no: str,
        blocker_key: str,
        *,
        operator: Optional[str] = None,
    ) -> dict[str, Any]:
        key = ord_line_no.strip()
        bkey = blocker_key.strip()
        if not key or not bkey:
            raise ValueError("ord_line_no and blocker_key required")
        now = _utcnow()
        existing = self.session.scalar(
            select(ProcurementBlockerAck).where(
                ProcurementBlockerAck.ord_line_no == key,
                ProcurementBlockerAck.blocker_key == bkey,
            )
        )
        if existing is None:
            self.session.add(
                ProcurementBlockerAck(
                    ord_line_no=key,
                    blocker_key=bkey,
                    operator=operator,
                    acked_at=now,
                )
            )
        return {
            "ord_line_no": key,
            "blocker_key": bkey,
            "operator": operator,
            "acked_at": now.isoformat(),
        }

    def is_blocker_acked(self, ord_line_no: str, blocker_key: str) -> bool:
        key = ord_line_no.strip()
        bkey = blocker_key.strip()
        row = self.session.scalar(
            select(ProcurementBlockerAck).where(
                ProcurementBlockerAck.ord_line_no == key,
                ProcurementBlockerAck.blocker_key == bkey,
            )
        )
        return row is not None

    def list_acked_keys(self, ord_line_no: str) -> set[str]:
        key = ord_line_no.strip()
        rows = self.session.scalars(
            select(ProcurementBlockerAck.blocker_key).where(
                ProcurementBlockerAck.ord_line_no == key
            )
        ).all()
        return {str(b).strip() for b in rows if str(b).strip()}


class AuditRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def append(self, record: dict[str, Any]) -> None:
        detail = {k: v for k, v in record.items() if k not in {
            "ord_line_no", "ord_no", "action_key", "action_label", "trigger",
            "operator", "admin_write", "ord_line_stat_before", "ord_line_stat_after", "at",
        }}
        created = _parse_dt(record.get("at")) or _parse_dt(record.get("created_at")) or _utcnow()
        self.session.add(
            ProcurementAuditLog(
                ord_line_no=str(record.get("ord_line_no") or "").strip(),
                ord_no=record.get("ord_no"),
                action_key=str(record.get("action_key") or "").strip(),
                action_label=record.get("action_label"),
                trigger=record.get("trigger"),
                operator=record.get("operator"),
                admin_write=record.get("admin_write"),
                ord_line_stat_before=record.get("ord_line_stat_before"),
                ord_line_stat_after=record.get("ord_line_stat_after"),
                audit_detail=detail or None,
                crt_time=created,
            )
        )

    def list_audits(
        self,
        *,
        action_key: Optional[str] = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        stmt = select(ProcurementAuditLog).order_by(desc(ProcurementAuditLog.crt_time))
        if action_key:
            stmt = stmt.where(ProcurementAuditLog.action_key == action_key)
        stmt = stmt.limit(max(1, limit))
        out: list[dict[str, Any]] = []
        for row in self.session.scalars(stmt).all():
            item: dict[str, Any] = {
                "ord_line_no": row.ord_line_no,
                "ord_no": row.ord_no,
                "action_key": row.action_key,
                "action_label": row.action_label,
                "trigger": row.trigger,
                "operator": row.operator,
                "admin_write": row.admin_write,
                "ord_line_stat_before": row.ord_line_stat_before,
                "ord_line_stat_after": row.ord_line_stat_after,
                "at": row.crt_time.isoformat() if row.crt_time else None,
            }
            if isinstance(row.audit_detail, dict):
                item.update(row.audit_detail)
            out.append(item)
        return out

    def count(self) -> int:
        return len(self.session.scalars(select(ProcurementAuditLog.id)).all())


class SnapshotRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_row(self, ord_line_no: str) -> Optional[dict[str, Any]]:
        key = ord_line_no.strip()
        if not key:
            return None
        row = self.session.get(OrdLineSnapshot, key)
        if not row or not isinstance(row.ord_line_row, dict):
            return None
        return dict(row.ord_line_row)

    def upsert(self, row: dict[str, Any], *, fingerprint: str, queue: Optional[str]) -> None:
        key = str(row.get("ord_line_no") or "").strip()
        if not key:
            return
        now = _utcnow()
        existing = self.session.get(OrdLineSnapshot, key)
        payload = dict(row)
        fields = {
            "ord_no": str(row.get("ord_no") or ""),
            "ord_line_stat": row.get("ord_line_stat"),
            "pay_time": _parse_dt(row.get("pay_time")),
            "item_id": row.get("item_id"),
            "splr_item_id": row.get("splr_item_id"),
            "item_nm": row.get("item_nm"),
            "proc_queue": queue,
            "admin_fingerprint": fingerprint,
            "ord_line_row": payload,
            "upd_time": now,
        }
        if existing is None:
            self.session.add(OrdLineSnapshot(ord_line_no=key, **fields))
        else:
            for k, v in fields.items():
                setattr(existing, k, v)

    def count(self) -> int:
        return len(self.session.scalars(select(OrdLineSnapshot.ord_line_no)).all())


class SyncCursorRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_global_state(self, state: dict[str, Any]) -> None:
        """将 sync-state.json 的全局字段存到 queue_name='__global__'。"""
        row = self.session.get(SyncCursor, "__global__")
        if row is None:
            row = SyncCursor(queue_name="__global__")
            self.session.add(row)
        row.backfill_page = int(state.get("backfill_page") or row.backfill_page or 1)
        row.backfill_complete = bool(state.get("backfill_complete"))
        row.last_incremental_at = _parse_dt(state.get("last_incremental_at"))
        row.last_backfill_at = _parse_dt(state.get("last_backfill_at"))

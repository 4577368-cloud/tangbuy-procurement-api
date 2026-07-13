"""Agent 任务 / 事件流 / 品类本地映射 / 订单快照批量读。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import delete, desc, func, or_, select
from sqlalchemy.orm import Session

from app.db.models import (
    AgentTaskRecord,
    CategoryLocalMapping,
    EventLog,
    OrdLineSnapshot,
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


class SnapshotReadRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def load_all_dict(self) -> dict[str, dict[str, Any]]:
        rows = self.session.scalars(select(OrdLineSnapshot)).all()
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            payload = row.ord_line_row if isinstance(row.ord_line_row, dict) else {}
            out[row.ord_line_no] = dict(payload)
        return out

    def load_by_keys(self, keys: list[str]) -> dict[str, dict[str, Any]]:
        ids = [str(k or "").strip() for k in keys if str(k or "").strip()]
        if not ids:
            return {}
        rows = self.session.scalars(
            select(OrdLineSnapshot).where(OrdLineSnapshot.ord_line_no.in_(ids))
        ).all()
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            payload = row.ord_line_row if isinstance(row.ord_line_row, dict) else {}
            out[row.ord_line_no] = dict(payload)
        return out

    def count(self) -> int:
        return int(self.session.scalar(select(func.count()).select_from(OrdLineSnapshot)) or 0)


class AgentTaskRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def load_all(self) -> list[dict[str, Any]]:
        rows = self.session.scalars(
            select(AgentTaskRecord).order_by(desc(AgentTaskRecord.crt_time))
        ).all()
        return [dict(r.task_json) for r in rows if isinstance(r.task_json, dict)]

    def count(self) -> int:
        return len(self.session.scalars(select(AgentTaskRecord.task_id)).all())

    def save_all(self, tasks: list[dict[str, Any]]) -> None:
        """按 id 合并写回，不删除未出现在本次快照中的任务（避免与 append 竞态丢单）。"""
        now = _utcnow()
        for task in tasks:
            if not isinstance(task, dict):
                continue
            tid = str(task.get("id") or "").strip()
            if not tid:
                continue
            row = self.session.get(AgentTaskRecord, tid)
            fields = {
                "task_json": task,
                "crt_time": _parse_dt(task.get("created_at")) or now,
                "upd_time": _parse_dt(task.get("updated_at")) or now,
            }
            if row is None:
                self.session.add(AgentTaskRecord(task_id=tid, **fields))
            else:
                for k, v in fields.items():
                    setattr(row, k, v)

    def replace_all(self, tasks: list[dict[str, Any]]) -> None:
        """全量替换（仅 JSON 导入等场景）。"""
        keep_ids = {str(t.get("id") or "").strip() for t in tasks if str(t.get("id") or "").strip()}
        if keep_ids:
            self.session.execute(
                delete(AgentTaskRecord).where(AgentTaskRecord.task_id.not_in(keep_ids))
            )
        else:
            self.session.execute(delete(AgentTaskRecord))
        self.save_all(tasks)

    def upsert_one(self, task: dict[str, Any]) -> dict[str, Any]:
        tasks = self.load_all()
        tid = str(task.get("id") or "").strip()
        merged = {str(t.get("id")): t for t in tasks if t.get("id")}
        merged[tid] = task
        self.save_all(list(merged.values()))
        return task


class EventLogRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def append(self, stream: str, payload: dict[str, Any], *, event_key: Optional[str] = None) -> None:
        created = _parse_dt(payload.get("at")) or _parse_dt(payload.get("created_at")) or _utcnow()
        self.session.add(
            EventLog(
                stream=stream,
                event_key=event_key or str(payload.get("id") or payload.get("release_id") or "") or None,
                event_json=payload,
                crt_time=created,
            )
        )

    def replace_stream(self, stream: str, items: list[dict[str, Any]]) -> int:
        self.session.execute(delete(EventLog).where(EventLog.stream == stream))
        count = 0
        for item in items:
            if isinstance(item, dict):
                self.append(stream, item)
                count += 1
        return count

    def list_stream(self, stream: str, *, limit: int = 200) -> list[dict[str, Any]]:
        stmt = (
            select(EventLog)
            .where(EventLog.stream == stream)
            .order_by(desc(EventLog.crt_time))
            .limit(max(1, limit))
        )
        return [dict(r.event_json) for r in self.session.scalars(stmt).all() if isinstance(r.event_json, dict)]

    def count_stream(self, stream: str) -> int:
        return len(
            self.session.scalars(select(EventLog.id).where(EventLog.stream == stream)).all()
        )

    def latest_matching(
        self,
        stream: str,
        *,
        ord_line_no: Optional[str] = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        items = self.list_stream(stream, limit=limit)
        if not ord_line_no:
            return items
        key = ord_line_no.strip()
        matched: list[dict[str, Any]] = []
        for item in items:
            if str(item.get("ord_line_no") or "") == key:
                matched.append(item)
                continue
            nos = item.get("ord_line_nos") or []
            if key in nos:
                matched.append(item)
        return matched


class CategoryMappingRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def load_all(self) -> list[dict[str, Any]]:
        rows = self.session.scalars(
            select(CategoryLocalMapping).order_by(desc(CategoryLocalMapping.upd_time))
        ).all()
        return [dict(r.mapping_json) for r in rows if isinstance(r.mapping_json, dict)]

    def count(self) -> int:
        return len(self.session.scalars(select(CategoryLocalMapping.id)).all())

    def get_by_item_id(self, item_id: str) -> Optional[dict[str, Any]]:
        key = item_id.strip()
        if not key:
            return None
        row = self.session.scalar(
            select(CategoryLocalMapping)
            .where(CategoryLocalMapping.item_id == key)
            .order_by(desc(CategoryLocalMapping.upd_time))
            .limit(1)
        )
        return dict(row.mapping_json) if row and isinstance(row.mapping_json, dict) else None

    def get_by_splr_item_id(self, splr_item_id: str) -> Optional[dict[str, Any]]:
        key = splr_item_id.strip()
        if not key:
            return None
        row = self.session.scalar(
            select(CategoryLocalMapping)
            .where(CategoryLocalMapping.splr_item_id == key)
            .order_by(desc(CategoryLocalMapping.upd_time))
            .limit(1)
        )
        return dict(row.mapping_json) if row and isinstance(row.mapping_json, dict) else None

    def replace_all(self, entries: list[dict[str, Any]]) -> int:
        self.session.execute(delete(CategoryLocalMapping))
        now = _utcnow()
        count = 0
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            self.session.add(
                CategoryLocalMapping(
                    item_id=str(entry.get("item_id") or "") or None,
                    splr_item_id=str(entry.get("splr_item_id") or "") or None,
                    mapping_json=entry,
                    upd_time=now,
                )
            )
            count += 1
        return count

    def upsert_local_mapping(
        self,
        *,
        item_id: Optional[str] = None,
        splr_item_id: Optional[str] = None,
        hs: dict[str, Any],
        source: str = "auto",
    ) -> None:
        item_key = (item_id or "").strip()
        splr_key = (splr_item_id or "").strip()
        entries = self.load_all()
        found_idx = -1
        for i, e in enumerate(entries):
            if item_key and e.get("item_id") == item_key:
                found_idx = i
                break
            if splr_key and e.get("splr_item_id") == splr_key:
                found_idx = i
                break
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        entry = {
            "item_id": item_key or None,
            "splr_item_id": splr_key or None,
            "hs": hs,
            "source": source,
            "mapped_at": now,
        }
        if found_idx >= 0:
            entries[found_idx] = {**entries[found_idx], **entry}
        else:
            entries.insert(0, entry)
        self.replace_all(entries[:5000])


def read_event_stream(stream: str, *, limit: int = 5000) -> list[dict[str, Any]]:
    from app.db.session import db_session, is_db_enabled

    if not is_db_enabled():
        return []
    with db_session() as session:
        return EventLogRepository(session).list_stream(stream, limit=limit)


def write_event_stream(stream: str, items: list[dict[str, Any]]) -> int:
    from app.db.session import db_session, is_db_enabled

    if not is_db_enabled():
        return 0
    with db_session() as session:
        return EventLogRepository(session).replace_stream(stream, items)


def append_event_stream(stream: str, item: dict[str, Any]) -> None:
    from app.db.session import db_session, is_db_enabled

    if not is_db_enabled():
        return
    with db_session() as session:
        EventLogRepository(session).append(
            stream,
            item,
            event_key=str(item.get("id") or item.get("release_id") or "") or None,
        )


"""自动放行 / 1688 预订购审计。"""

from __future__ import annotations

import fcntl
import json
from pathlib import Path
from typing import Any, Optional

from app.core.paths import data_dir
from app.db.session import db_session, is_db_enabled

_RELEASES_PATH = data_dir() / "orders" / "auto-releases.jsonl"
_STREAM = "auto_release"


def _ensure_dir() -> None:
    _RELEASES_PATH.parent.mkdir(parents=True, exist_ok=True)


def append_release(record: dict[str, Any]) -> dict[str, Any]:
    if is_db_enabled():
        from app.db.ops_repos import EventLogRepository

        with db_session() as session:
            EventLogRepository(session).append(
                _STREAM,
                record,
                event_key=str(record.get("release_id") or record.get("ord_line_no") or ""),
            )
        return record
    _ensure_dir()
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with open(_RELEASES_PATH, "a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.write(line)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return record


def list_releases(*, limit: int = 200) -> list[dict[str, Any]]:
    if is_db_enabled():
        from app.db.ops_repos import EventLogRepository

        with db_session() as session:
            return EventLogRepository(session).list_stream(_STREAM, limit=limit)
    _ensure_dir()
    if not _RELEASES_PATH.exists():
        return []
    try:
        lines = _RELEASES_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for raw in lines[-max(1, limit) :]:
        raw = raw.strip()
        if not raw:
            continue
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            out.append(item)
    out.reverse()
    return out


def latest_release(ord_line_no: str) -> Optional[dict[str, Any]]:
    key = ord_line_no.strip()
    if not key:
        return None
    if is_db_enabled():
        from app.db.ops_repos import EventLogRepository

        with db_session() as session:
            matched = EventLogRepository(session).latest_matching(_STREAM, ord_line_no=key, limit=500)
            return matched[0] if matched else None
    for item in list_releases(limit=500):
        if str(item.get("ord_line_no") or "") == key:
            return item
    return None


def has_successful_release(ord_line_no: str) -> bool:
    latest = latest_release(ord_line_no)
    if not latest:
        return False
    return latest.get("result") in ("confirmed", "auto_confirmed", "already_submitted")

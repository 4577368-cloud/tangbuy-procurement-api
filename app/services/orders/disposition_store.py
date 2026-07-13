"""订单处置审计与本地队列覆盖（DispositionWritePort 文件实现，接 Admin 写接口前生效）。"""

from __future__ import annotations

import fcntl
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app.core.paths import data_dir
from app.db.session import is_db_enabled

_OVERRIDES_PATH = data_dir() / "orders" / "disposition-overrides.json"
_AUDIT_PATH = data_dir() / "orders" / "dispositions.jsonl"


def _ensure_dir() -> None:
    _OVERRIDES_PATH.parent.mkdir(parents=True, exist_ok=True)


def _read_overrides() -> dict[str, dict[str, Any]]:
    if is_db_enabled():
        from app.db.catalog_repos import DispositionOverrideRepository
        from app.db.session import db_session

        with db_session() as session:
            return DispositionOverrideRepository(session).load_all()
    _ensure_dir()
    if not _OVERRIDES_PATH.exists():
        return {}
    try:
        raw = json.loads(_OVERRIDES_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_overrides(data: dict[str, dict[str, Any]]) -> None:
    _ensure_dir()
    _OVERRIDES_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def load_all_overrides() -> dict[str, dict[str, Any]]:
    """一次性加载全部 disposition 覆盖（批量 enrich 用）。"""
    return _read_overrides()


def get_override(ord_line_no: str) -> Optional[dict[str, Any]]:
    return _read_overrides().get(ord_line_no.strip())


def list_overrides_for_queue(queue: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for key, value in _read_overrides().items():
        if value.get("queue_override") == queue:
            out[key] = value
    return out


def summary_adjustments() -> dict[str, int]:
    """本地放行覆盖 → 汇总 Tab 计数修正（Admin 尚未写库时）。"""
    passed = len(list_overrides_for_queue("pending_payment"))
    if passed <= 0:
        return {}
    return {
        "pending_payment": passed,
        "pending_procurement": -passed,
    }


def apply_row_override(row: dict[str, Any]) -> dict[str, Any]:
    ord_line_no = str(row.get("ord_line_no") or "").strip()
    if not ord_line_no:
        return row
    override = get_override(ord_line_no)
    if not override:
        return row
    merged = {**row}
    for key, value in override.items():
        if key in ("queue_override", "passed_at", "action_key", "signal_type"):
            continue
        merged[key] = value
    return merged


def revert_procurement_pass(
    ord_line_no: str,
    *,
    ord_no: Optional[str] = None,
    note: Optional[str] = None,
    operator: Optional[str] = None,
) -> dict[str, Any]:
    """不认可自动放行：本地退回待下单队列（不改 Admin 已提交的 1688 预订购）。"""
    key = ord_line_no.strip()
    now = datetime.now(timezone.utc).isoformat()
    prev = get_override(key) or {}
    patch = {
        **prev,
        "ord_line_stat": 23,
        "ord_line_stat_nm": "处理中",
        "queue_override": "pending_procurement",
        "rejected_at": now,
        "rejected_note": note,
        "action_key": "reject_auto_release",
        "operator": operator,
        "ord_no": ord_no or prev.get("ord_no"),
    }
    patch.pop("passed_at", None)
    return merge_override(key, patch)


def set_procurement_passed(
    ord_line_no: str,
    *,
    ord_no: Optional[str] = None,
    action_key: str,
    signal_type: Optional[str] = None,
    operator: Optional[str] = None,
    note: Optional[str] = None,
) -> dict[str, Any]:
    key = ord_line_no.strip()
    now = datetime.now(timezone.utc).isoformat()
    patch = {
        "ord_line_stat": -1,
        "ord_line_stat_nm": "待支付",
        "queue_override": "pending_payment",
        "passed_at": now,
        "action_key": action_key,
        "signal_type": signal_type,
        "operator": operator,
        "note": note,
        "ord_no": ord_no,
    }
    return merge_override(key, patch)


def merge_override(ord_line_no: str, patch: dict[str, Any]) -> dict[str, Any]:
    """合并写入子单本地覆盖（换供货源字段等，不整表覆盖）。"""
    key = ord_line_no.strip()
    if not key:
        raise ValueError("缺少 ord_line_no")
    if is_db_enabled():
        from app.db.catalog_repos import DispositionOverrideRepository
        from app.db.session import db_session

        with db_session() as session:
            return DispositionOverrideRepository(session).merge(key, patch)
    _ensure_dir()
    _OVERRIDES_PATH.touch(exist_ok=True)
    with open(_OVERRIDES_PATH, "r+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.seek(0)
            raw = handle.read()
            try:
                data = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError:
                data = {}
            if not isinstance(data, dict):
                data = {}
            prev = data.get(key) if isinstance(data.get(key), dict) else {}
            merged = {**prev, **patch}
            data[key] = merged
            handle.seek(0)
            handle.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
            handle.truncate()
            handle.flush()
            return merged
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def append_audit(record: dict[str, Any]) -> None:
    if is_db_enabled():
        from app.db.repositories import AuditRepository
        from app.db.session import db_session

        with db_session() as session:
            AuditRepository(session).append(record)
            return
    _ensure_dir()
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with open(_AUDIT_PATH, "a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.write(line)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def list_audits(
    *,
    action_key: Optional[str] = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    if is_db_enabled():
        from app.db.repositories import AuditRepository
        from app.db.session import db_session

        with db_session() as session:
            return AuditRepository(session).list_audits(action_key=action_key, limit=limit)
    if not _AUDIT_PATH.exists():
        return []
    rows: list[dict[str, Any]] = []
    for raw in reversed(_read_lines(_AUDIT_PATH)):
        raw = raw.strip()
        if not raw:
            continue
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue
        if action_key and str(item.get("action_key") or "") != action_key:
            continue
        rows.append(item)
        if len(rows) >= max(1, limit):
            break
    return rows


def _read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

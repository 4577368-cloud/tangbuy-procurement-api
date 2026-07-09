"""订单处置审计与本地队列覆盖（DispositionWritePort 文件实现，接 Admin 写接口前生效）。"""

from __future__ import annotations

import fcntl
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app.core.paths import data_dir

_OVERRIDES_PATH = data_dir() / "orders" / "disposition-overrides.json"
_AUDIT_PATH = data_dir() / "orders" / "dispositions.jsonl"


def _ensure_dir() -> None:
    _OVERRIDES_PATH.parent.mkdir(parents=True, exist_ok=True)


def _read_overrides() -> dict[str, dict[str, Any]]:
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
    _ensure_dir()
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with open(_AUDIT_PATH, "a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.write(line)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

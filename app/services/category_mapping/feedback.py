"""品类映射反馈归档（DB 或 JSONL）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.core.paths import data_dir
from app.db.ops_repos import append_event_stream, read_event_stream
from app.db.session import is_db_enabled

STREAM_FEEDBACK = "category_feedback"
STREAM_ARCHIVE = "category_archive"


def _feedback_path() -> Path:
    return data_dir() / "category" / "feedback.jsonl"


def _archive_path() -> Path:
    return data_dir() / "category" / "archive.jsonl"


def _append_jsonl(path: Path, entry: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def append_feedback(entry: dict[str, Any]) -> None:
    if is_db_enabled():
        append_event_stream(STREAM_FEEDBACK, entry)
    else:
        _append_jsonl(_feedback_path(), entry)
    # 二/三期：确认与纠错只加票进 pending-conventions，不单次污染主惯例
    try:
        from app.services.category_mapping.pending_conventions import ingest_feedback_entry

        ingest_feedback_entry(entry)
    except Exception:
        pass


def append_archive(entry: dict[str, Any]) -> None:
    if is_db_enabled():
        append_event_stream(STREAM_ARCHIVE, entry)
        return
    _append_jsonl(_archive_path(), entry)


def list_feedback(*, limit: int = 5000) -> list[dict[str, Any]]:
    if is_db_enabled():
        return read_event_stream(STREAM_FEEDBACK, limit=limit)
    if not _feedback_path().exists():
        return []
    out: list[dict[str, Any]] = []
    for line in _feedback_path().read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
            if isinstance(item, dict):
                out.append(item)
        except json.JSONDecodeError:
            continue
    return out[-limit:]

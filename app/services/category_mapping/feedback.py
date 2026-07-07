"""品类映射反馈归档。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.core.paths import data_dir


def _feedback_path() -> Path:
    return data_dir() / "category" / "feedback.jsonl"


def _archive_path() -> Path:
    return data_dir() / "category" / "archive.jsonl"


def _append_jsonl(path: Path, entry: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def append_feedback(entry: dict[str, Any]) -> None:
    _append_jsonl(_feedback_path(), entry)


def append_archive(entry: dict[str, Any]) -> None:
    _append_jsonl(_archive_path(), entry)

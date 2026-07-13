"""本地 SKU → HS 映射缓存（DB 或 JSON 文件）。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app.core.paths import data_dir
from app.db.session import db_session, is_db_enabled

_FILE = data_dir() / "category" / "local-mappings.json"
_MAX_ENTRIES = 5000


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def load_local_mappings() -> list[dict[str, Any]]:
    if is_db_enabled():
        from app.db.ops_repos import CategoryMappingRepository

        with db_session() as session:
            return CategoryMappingRepository(session).load_all()
    if not _FILE.exists():
        return []
    try:
        data = json.loads(_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _write_all(entries: list[dict[str, Any]]) -> None:
    if is_db_enabled():
        from app.db.ops_repos import CategoryMappingRepository

        with db_session() as session:
            CategoryMappingRepository(session).replace_all(entries[:_MAX_ENTRIES])
        return
    _FILE.parent.mkdir(parents=True, exist_ok=True)
    _FILE.write_text(json.dumps(entries[:_MAX_ENTRIES], ensure_ascii=False, indent=2), encoding="utf-8")


def get_by_item_id(item_id: str) -> Optional[dict[str, Any]]:
    key = (item_id or "").strip()
    if not key:
        return None
    if is_db_enabled():
        from app.db.ops_repos import CategoryMappingRepository

        with db_session() as session:
            return CategoryMappingRepository(session).get_by_item_id(key)
    return next((e for e in load_local_mappings() if e.get("item_id") == key), None)


def get_by_splr_item_id(splr_item_id: str) -> Optional[dict[str, Any]]:
    key = (splr_item_id or "").strip()
    if not key:
        return None
    if is_db_enabled():
        from app.db.ops_repos import CategoryMappingRepository

        with db_session() as session:
            return CategoryMappingRepository(session).get_by_splr_item_id(key)
    return next((e for e in load_local_mappings() if e.get("splr_item_id") == key), None)


def upsert_local_mapping(
    *,
    item_id: Optional[str] = None,
    splr_item_id: Optional[str] = None,
    hs: dict[str, Any],
    source: str = "auto",
) -> None:
    item_key = (item_id or "").strip()
    splr_key = (splr_item_id or "").strip()
    if not item_key and not splr_key:
        return
    if is_db_enabled():
        from app.db.ops_repos import CategoryMappingRepository

        with db_session() as session:
            CategoryMappingRepository(session).upsert_local_mapping(
                item_id=item_key or None,
                splr_item_id=splr_key or None,
                hs=hs,
                source=source,
            )
        return
    entry = {
        "item_id": item_key or None,
        "splr_item_id": splr_key or None,
        "hs": hs,
        "mapped_at": _now_iso(),
        "source": source,
    }
    kept = [
        e
        for e in load_local_mappings()
        if not (
            (item_key and e.get("item_id") == item_key)
            or (splr_key and e.get("splr_item_id") == splr_key)
        )
    ]
    kept.insert(0, entry)
    _write_all(kept)

"""采购履约流水线状态（DB 或文件持久化）。"""

from __future__ import annotations

import fcntl
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from app.core.paths import data_dir
from app.db.session import db_session, is_db_enabled

PipelineStep = Literal["accept", "prepare", "pre_purchase", "place_order", "payment", "followup", "done", "blocked"]

_STATE_PATH = data_dir() / "orders" / "pipeline-state.jsonl"
_ACK_PATH = data_dir() / "orders" / "pipeline-acks.jsonl"


def _ensure_dir() -> None:
    _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []


def _append_line(path: Path, record: dict[str, Any]) -> None:
    _ensure_dir()
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.write(line)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _latest_by_key(path: Path, key_field: str = "ord_line_no") -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for raw in _read_lines(path):
        raw = raw.strip()
        if not raw:
            continue
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            key = str(item.get(key_field) or "").strip()
            if key:
                out[key] = item
    return out


def _pipeline_state_map() -> dict[str, dict[str, Any]]:
    if is_db_enabled():
        from app.db.repositories import PipelineRepository

        with db_session() as session:
            return PipelineRepository(session).latest_map()
    return _latest_by_key(_STATE_PATH)


def get_pipeline_state(ord_line_no: str) -> Optional[dict[str, Any]]:
    key = ord_line_no.strip()
    if not key:
        return None
    if is_db_enabled():
        from app.db.repositories import PipelineRepository

        with db_session() as session:
            return PipelineRepository(session).get(key)
    return _latest_by_key(_STATE_PATH).get(key)


def save_pipeline_state(state: dict[str, Any]) -> dict[str, Any]:
    key = str(state.get("ord_line_no") or "").strip()
    if not key:
        raise ValueError("ord_line_no required")
    state = {**state, "ord_line_no": key, "updated_at": _now_iso()}
    if is_db_enabled():
        from app.db.repositories import PipelineRepository

        with db_session() as session:
            saved = PipelineRepository(session).save(state)
    else:
        _append_line(_STATE_PATH, state)
        saved = state
    try:
        from app.services.workflow.hooks import trace_pipeline_advance

        blockers = saved.get("blockers") if isinstance(saved.get("blockers"), list) else saved.get("pipeline_blockers")
        trace_pipeline_advance(
            key,
            pipeline_step=str(saved.get("pipeline_step") or ""),
            ord_line_stat=saved.get("ord_line_stat"),
            blockers=blockers if isinstance(blockers, list) else None,
        )
    except Exception:
        pass
    return saved


def list_pipeline_states(*, limit: int = 500) -> list[dict[str, Any]]:
    if is_db_enabled():
        from app.db.repositories import PipelineRepository

        with db_session() as session:
            return PipelineRepository(session).list_states(limit=limit)
    items = list(_latest_by_key(_STATE_PATH).values())
    items.sort(key=lambda x: str(x.get("updated_at") or ""), reverse=True)
    return items[: max(1, limit)]


def ack_blocker(ord_line_no: str, blocker_key: str, *, operator: Optional[str] = None) -> dict[str, Any]:
    key = ord_line_no.strip()
    bkey = blocker_key.strip()
    if not key or not bkey:
        raise ValueError("ord_line_no and blocker_key required")
    if is_db_enabled():
        from app.db.repositories import PipelineRepository

        with db_session() as session:
            return PipelineRepository(session).ack_blocker(key, bkey, operator=operator)
    record = {
        "ord_line_no": key,
        "blocker_key": bkey,
        "operator": operator,
        "acked_at": _now_iso(),
    }
    _append_line(_ACK_PATH, record)
    return record


def is_blocker_acked(ord_line_no: str, blocker_key: str) -> bool:
    key = ord_line_no.strip()
    bkey = blocker_key.strip()
    if is_db_enabled():
        from app.db.repositories import PipelineRepository

        with db_session() as session:
            return PipelineRepository(session).is_blocker_acked(key, bkey)
    for raw in reversed(_read_lines(_ACK_PATH)):
        raw = raw.strip()
        if not raw:
            continue
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if str(item.get("ord_line_no") or "") == key and str(item.get("blocker_key") or "") == bkey:
            return True
    return False


def _derive_pipeline_step(row: dict[str, Any]) -> str:
    raw = row.get("ord_line_stat")
    try:
        stat = int(raw) if raw is not None else None
    except (TypeError, ValueError):
        stat = None
    if stat == 0:
        return "accept"
    if stat == 23:
        return "prepare"
    if stat == 54:
        return "place_order"
    if stat in (55, -1, -2, 2):
        return "payment"
    if stat == 22:
        return "followup"
    if stat is not None and stat >= 5:
        return "done"
    return "prepare"


def enrich_row_pipeline_fields(
    row: dict[str, Any],
    *,
    states: Optional[dict[str, dict[str, Any]]] = None,
) -> dict[str, Any]:
    """为宽表行附加 pipeline_step / pipeline_blockers。"""
    key = str(row.get("ord_line_no") or "").strip()
    if not key:
        return row
    state_map = states if states is not None else _pipeline_state_map()
    state = state_map.get(key)
    if state:
        blockers = state.get("blockers") if isinstance(state.get("blockers"), list) else []
        step = str(state.get("pipeline_step") or _derive_pipeline_step(row))
        return {**row, "pipeline_step": step, "pipeline_blockers": blockers}

    stat_raw = row.get("ord_line_stat")
    try:
        stat = int(stat_raw) if stat_raw is not None else None
    except (TypeError, ValueError):
        stat = None
    blockers: list[dict[str, Any]] = []
    if stat == 23:
        from app.services.orders.procurement_release import evaluate_prepare_stage

        prep = evaluate_prepare_stage(row)
        blockers = prep.get("blockers") if isinstance(prep.get("blockers"), list) else []
    elif stat == 0:
        from app.config.business_config import normalize_business_config
        from app.config.store import get_business_config
        from app.services.orders.procurement_release import GENERIC_CATEGORIES

        category = str(row.get("lvl1_ctgy_nm") or "").strip()
        if not category or category in GENERIC_CATEGORIES:
            auto_map = bool(
                normalize_business_config(get_business_config())
                .get("rules", {})
                .get("auto_category_mapping", True)
            )
            blockers = [
                {
                    "key": "CATEGORY_OTHER",
                    "label": "品类未映射",
                    "stage": "accept",
                    "auto_resolvable": auto_map,
                    "requires_ack": False,
                    "detail": category or "其他",
                    "at": _now_iso(),
                }
            ]
    return {
        **row,
        "pipeline_step": _derive_pipeline_step(row),
        "pipeline_blockers": blockers,
    }


def enrich_rows_pipeline(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from app.services.orders.product_category_enrich import enrich_row_mapped_category
    from app.services.products.store import build_ord_line_product_index

    states = _pipeline_state_map()
    product_index = build_ord_line_product_index()
    return [
        enrich_row_mapped_category(
            enrich_row_pipeline_fields(row, states=states),
            product_index=product_index,
        )
        for row in rows
    ]


def list_acked_keys(ord_line_no: str) -> set[str]:
    key = ord_line_no.strip()
    if is_db_enabled():
        from app.db.repositories import PipelineRepository

        with db_session() as session:
            return PipelineRepository(session).list_acked_keys(key)
    out: set[str] = set()
    for raw in _read_lines(_ACK_PATH):
        raw = raw.strip()
        if not raw:
            continue
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if str(item.get("ord_line_no") or "") == key:
            b = str(item.get("blocker_key") or "").strip()
            if b:
                out.add(b)
    return out

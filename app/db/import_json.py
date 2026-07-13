"""从 JSON/JSONL 一次性导入数据库。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.core.paths import data_dir
from app.db.catalog_repos import (
    ConfigRepository,
    DispositionOverrideRepository,
    ProductRepository,
)
from app.db.repositories import AuditRepository, PipelineRepository, SnapshotRepository, SyncCursorRepository
from app.db.session import db_session, is_db_enabled
from app.services.orders import line_cache
from app.services.orders.queue_filters import resolve_order_queue
from app.services.products.store import _parse_products_text, sanitize_product_mapping_state


def _read_jsonl_latest(path: Path, key_field: str = "ord_line_no") -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
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


def _read_jsonl_all(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def import_json_to_db(*, force: bool = False) -> dict[str, int]:
    """导入 pipeline / ack / audit / line-cache 到 DB。"""
    if not is_db_enabled():
        return {"skipped": 1}

    orders_dir = data_dir() / "orders"
    stats = {
        "pipeline_states": 0,
        "blocker_acks": 0,
        "audit_logs": 0,
        "snapshots": 0,
        "sync_cursor": 0,
    }

    with db_session() as session:
        pipeline_repo = PipelineRepository(session)
        audit_repo = AuditRepository(session)
        snapshot_repo = SnapshotRepository(session)
        cursor_repo = SyncCursorRepository(session)

        existing_pipeline = 0 if force else len(pipeline_repo.latest_map())
        if existing_pipeline == 0 or force:
            for state in _read_jsonl_latest(orders_dir / "pipeline-state.jsonl").values():
                pipeline_repo.save(state)
                stats["pipeline_states"] += 1

            seen_acks: set[tuple[str, str]] = set()
            for item in _read_jsonl_all(orders_dir / "pipeline-acks.jsonl"):
                key = str(item.get("ord_line_no") or "").strip()
                bkey = str(item.get("blocker_key") or "").strip()
                if not key or not bkey or (key, bkey) in seen_acks:
                    continue
                seen_acks.add((key, bkey))
                pipeline_repo.ack_blocker(key, bkey, operator=item.get("operator"))
                stats["blocker_acks"] += 1

        existing_snapshots = 0 if force else snapshot_repo.count()
        if existing_snapshots == 0 or force:
            lines = line_cache.load_all_lines()
            for key, row in lines.items():
                snapshot_repo.upsert(
                    row,
                    fingerprint=line_cache._status_fingerprint(row),
                    queue=resolve_order_queue(row),
                )
                stats["snapshots"] += 1

        if stats["audit_logs"] == 0 or force:
            existing_audits = 0 if force else AuditRepository(session).count()
            if existing_audits == 0 or force:
                for item in _read_jsonl_all(orders_dir / "dispositions.jsonl"):
                    audit_repo.append(item)
                    stats["audit_logs"] += 1

        state_path = orders_dir / "sync-state.json"
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                if isinstance(state, dict):
                    from app.db.catalog_repos import ConfigRepository

                    ConfigRepository(session).save_document(
                        ConfigRepository.SYNC_STATE_KEY,
                        state,
                    )
                    cursor_repo.upsert_global_state(state)
                    stats["sync_cursor"] = 1
            except (OSError, json.JSONDecodeError):
                pass

    return stats


def import_catalog_to_db(*, force: bool = False) -> dict[str, int]:
    """导入商品中心 / 配置中心 / 处置覆盖到 DB。"""
    if not is_db_enabled():
        return {"skipped": 1}

    stats = {"products": 0, "product_links": 0, "config": 0, "disposition_overrides": 0, "note_classify_cache": 0, "find_cache": 0, "briefing_snapshot": 0}
    products_path = data_dir() / "products" / "center.json"
    config_path = data_dir() / "config" / "config-center.json"
    overrides_path = data_dir() / "orders" / "disposition-overrides.json"

    with db_session() as session:
        product_repo = ProductRepository(session)
        config_repo = ConfigRepository(session)
        override_repo = DispositionOverrideRepository(session)

        if force or product_repo.count() == 0:
            if products_path.exists():
                try:
                    if force and product_repo.count() > 0:
                        from app.db.models import ProductOrdLineLink, ProductRecord
                        from sqlalchemy import delete

                        session.execute(delete(ProductOrdLineLink))
                        session.execute(delete(ProductRecord))
                        session.flush()
                    items = _parse_products_text(products_path.read_text(encoding="utf-8"))
                    items = [sanitize_product_mapping_state(p) for p in items if isinstance(p, dict)]
                    product_repo.save_all(items)
                    stats["products"] = len(items)
                    stats["product_links"] = sum(
                        len(p.get("linked_ord_lines") or []) for p in items
                    )
                except OSError:
                    pass

        if force or config_repo.count() == 0:
            if config_path.exists():
                try:
                    raw = json.loads(config_path.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        config_repo.save(raw)
                        stats["config"] = 1
                except (OSError, json.JSONDecodeError):
                    pass

        if force or override_repo.count() == 0:
            if overrides_path.exists():
                try:
                    raw = json.loads(overrides_path.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        for key, value in raw.items():
                            if isinstance(value, dict):
                                override_repo.merge(str(key), value)
                                stats["disposition_overrides"] += 1
                except (OSError, json.JSONDecodeError):
                    pass

        note_cache_path = data_dir() / "orders" / "note-classify-cache.json"
        if (force or not config_repo.load_document(ConfigRepository.NOTE_CLASSIFY_CACHE_KEY)) and note_cache_path.exists():
            try:
                raw = json.loads(note_cache_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and raw:
                    config_repo.save_document(ConfigRepository.NOTE_CLASSIFY_CACHE_KEY, raw)
                    stats["note_classify_cache"] = len(raw)
            except (OSError, json.JSONDecodeError):
                pass

        find_cache_path = data_dir() / "products" / "find-cache.json"
        if (force or not config_repo.load_document(ConfigRepository.FIND_CACHE_KEY)) and find_cache_path.exists():
            try:
                raw = json.loads(find_cache_path.read_text(encoding="utf-8"))
                entries = raw if isinstance(raw, list) else []
                if entries:
                    config_repo.save_document(
                        ConfigRepository.FIND_CACHE_KEY,
                        {"version": 1, "entries": entries},
                    )
                    stats["find_cache"] = len(entries)
            except (OSError, json.JSONDecodeError):
                pass

        briefing_path = data_dir() / "command-center" / "briefing-snapshot.json"
        if (force or not config_repo.load_document(ConfigRepository.BRIEFING_SNAPSHOT_KEY)) and briefing_path.exists():
            try:
                raw = json.loads(briefing_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and raw:
                    config_repo.save_document(ConfigRepository.BRIEFING_SNAPSHOT_KEY, raw)
                    stats["briefing_snapshot"] = 1
            except (OSError, json.JSONDecodeError):
                pass

    return stats


def import_ops_to_db(*, force: bool = False) -> dict[str, int]:
    """导入 Agent 任务 / 事件流 / 品类本地映射。"""
    if not is_db_enabled():
        return {"skipped": 1}

    from app.db.ops_repos import (
        AgentTaskRepository,
        CategoryMappingRepository,
        EventLogRepository,
    )
    from app.services.tasks.persistence import _parse_tasks

    stats = {
        "agent_tasks": 0,
        "skill_invocations": 0,
        "skill_tuning": 0,
        "evolution_feedback": 0,
        "evolution_analysis": 0,
        "evolution_patches": 0,
        "place_orders": 0,
        "auto_releases": 0,
        "category_mappings": 0,
        "category_feedback": 0,
        "category_archive": 0,
    }

    agent_dir = data_dir() / "agent"
    orders_dir = data_dir() / "orders"
    evolution_dir = data_dir() / "evolution"
    category_path = data_dir() / "category" / "local-mappings.json"

    with db_session() as session:
        task_repo = AgentTaskRepository(session)
        event_repo = EventLogRepository(session)
        map_repo = CategoryMappingRepository(session)

        tasks_path = agent_dir / "tasks.json"
        if (force or task_repo.count() == 0) and tasks_path.exists():
            try:
                tasks = _parse_tasks(tasks_path.read_text(encoding="utf-8"))
                task_repo.replace_all(tasks)
                stats["agent_tasks"] = len(tasks)
            except OSError:
                pass

        streams = [
            (agent_dir / "skill-invocations.jsonl", "skill_invocation", "skill_invocations", 5000),
            (agent_dir / "skill-tuning.jsonl", "skill_tuning", "skill_tuning", 5000),
            (evolution_dir / "feedback.jsonl", "evolution_feedback", "evolution_feedback", 10000),
            (evolution_dir / "analysis-reports.jsonl", "evolution_analysis", "evolution_analysis", 500),
            (evolution_dir / "patches.jsonl", "evolution_patches", "evolution_patches", 200),
            (orders_dir / "place-orders.jsonl", "place_order", "place_orders", 5000),
            (orders_dir / "auto-releases.jsonl", "auto_release", "auto_releases", 5000),
            (data_dir() / "category" / "feedback.jsonl", "category_feedback", "category_feedback", 5000),
            (data_dir() / "category" / "archive.jsonl", "category_archive", "category_archive", 5000),
        ]
        for path, stream, stat_key, cap in streams:
            if not path.exists():
                continue
            if not force and event_repo.count_stream(stream) > 0:
                continue
            items = _read_jsonl_all(path)[:cap]
            if items:
                event_repo.replace_stream(stream, items)
                stats[stat_key] = len(items)

        if (force or map_repo.count() == 0) and category_path.exists():
            try:
                raw = json.loads(category_path.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    stats["category_mappings"] = map_repo.replace_all(raw)
            except (OSError, json.JSONDecodeError):
                pass

    return stats


def import_all_to_db(*, force: bool = False) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    merged.update(import_json_to_db(force=force))
    merged.update(import_catalog_to_db(force=force))
    merged.update(import_ops_to_db(force=force))
    return merged


def run_if_empty() -> dict[str, Any]:
    if not is_db_enabled():
        return {"skipped": 1}
    with db_session() as session:
        from app.db.ops_repos import AgentTaskRepository

        has_pipeline = bool(PipelineRepository(session).latest_map())
        has_products = ProductRepository(session).count() > 0
        has_tasks = AgentTaskRepository(session).count() > 0
    if has_pipeline and has_products and has_tasks:
        return {"skipped": 1}
    return import_all_to_db()

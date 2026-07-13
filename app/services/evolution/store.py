"""AI 自进化引擎 · 数据存储（JSONL 持久化，对齐 skill_audit/store.py 模式）。"""

from __future__ import annotations

import json
import random
import time
from pathlib import Path
from typing import Any, Optional

from app.core.paths import data_dir
from app.db.ops_repos import append_event_stream, read_event_stream, write_event_stream
from app.db.session import is_db_enabled

# ─── 路径常量 ───

EVOLUTION_DIR = data_dir() / "evolution"
FEEDBACK_PATH = EVOLUTION_DIR / "feedback.jsonl"
ANALYSIS_PATH = EVOLUTION_DIR / "analysis-reports.jsonl"
PATCHES_PATH = EVOLUTION_DIR / "patches.jsonl"
STREAM_FEEDBACK = "evolution_feedback"
STREAM_ANALYSIS = "evolution_analysis"
STREAM_PATCHES = "evolution_patches"

MAX_FEEDBACK = 10000
MAX_REPORTS = 500
MAX_PATCHES = 200

# ─── 工具函数 ───


def _new_id(prefix: str) -> str:
    return f"{prefix}-{int(time.time() * 1000)}-{random.randint(0, 99999):05d}"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if is_db_enabled():
        stream_map = {
            FEEDBACK_PATH: STREAM_FEEDBACK,
            ANALYSIS_PATH: STREAM_ANALYSIS,
            PATCHES_PATH: STREAM_PATCHES,
        }
        stream = stream_map.get(path)
        if stream:
            limit = MAX_FEEDBACK if stream == STREAM_FEEDBACK else MAX_REPORTS if stream == STREAM_ANALYSIS else MAX_PATCHES
            return read_event_stream(stream, limit=limit)
    if not path.exists():
        return []
    items: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return items


def _write_jsonl(path: Path, items: list[dict[str, Any]]) -> None:
    if is_db_enabled():
        stream_map = {
            FEEDBACK_PATH: STREAM_FEEDBACK,
            ANALYSIS_PATH: STREAM_ANALYSIS,
            PATCHES_PATH: STREAM_PATCHES,
        }
        stream = stream_map.get(path)
        if stream:
            write_event_stream(stream, items)
            return
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(json.dumps(i, ensure_ascii=False) for i in items)
    path.write_text(text + ("\n" if text else ""), encoding="utf-8")


def _append_jsonl(path: Path, item: dict[str, Any]) -> None:
    if is_db_enabled():
        stream_map = {
            FEEDBACK_PATH: STREAM_FEEDBACK,
            ANALYSIS_PATH: STREAM_ANALYSIS,
            PATCHES_PATH: STREAM_PATCHES,
        }
        stream = stream_map.get(path)
        if stream:
            append_event_stream(stream, item)
            return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")


# ─── 反馈记录 ───

_feedback_cache: Optional[list[dict[str, Any]]] = None


def _load_feedback() -> list[dict[str, Any]]:
    global _feedback_cache
    if _feedback_cache is not None:
        return _feedback_cache
    records = _read_jsonl(FEEDBACK_PATH)
    records.sort(key=lambda r: str(r.get("at") or ""), reverse=True)
    _feedback_cache = records
    return records


def _persist_feedback(records: list[dict[str, Any]]) -> None:
    global _feedback_cache
    trimmed = records[:MAX_FEEDBACK]
    _write_jsonl(FEEDBACK_PATH, trimmed)
    _feedback_cache = trimmed


def append_feedback(item: dict[str, Any]) -> str:
    """追加一条反馈记录，返回记录 ID。"""
    if not item.get("id"):
        item["id"] = _new_id("fb")
    if not item.get("at"):
        from datetime import datetime, timezone
        item["at"] = datetime.now(timezone.utc).isoformat()
    item["analyzed"] = False
    _append_jsonl(FEEDBACK_PATH, item)
    global _feedback_cache
    _feedback_cache = None  # 清缓存，下次重新加载
    return item["id"]


def get_feedback_records(
    *,
    skill_id: Optional[str] = None,
    domain: Optional[str] = None,
    sentiment: Optional[str] = None,
    analyzed: Optional[bool] = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """查询反馈记录。"""
    records = _load_feedback()
    if skill_id:
        records = [r for r in records if r.get("skill_id") == skill_id]
    if domain:
        records = [r for r in records if r.get("domain") == domain]
    if sentiment:
        records = [r for r in records if r.get("sentiment") == sentiment]
    if analyzed is not None:
        records = [r for r in records if r.get("analyzed") == analyzed]
    return records[offset: offset + limit]


def get_unanalyzed_feedback() -> list[dict[str, Any]]:
    """获取未分析的反馈记录。"""
    return [r for r in _load_feedback() if not r.get("analyzed")]


def get_negative_unanalyzed_feedback() -> list[dict[str, Any]]:
    """获取未分析的负反馈记录。"""
    return [r for r in _load_feedback() if not r.get("analyzed") and r.get("sentiment") in ("negative", "neutral")]


def mark_feedback_analyzed(ids: list[str]) -> int:
    """标记反馈记录已分析。"""
    records = _load_feedback()
    count = 0
    for r in records:
        if r.get("id") in ids and not r.get("analyzed"):
            r["analyzed"] = True
            count += 1
    _persist_feedback(records)
    return count


def get_feedback_stats() -> dict[str, Any]:
    """反馈统计。"""
    records = _load_feedback()
    total = len(records)
    by_sentiment = {}
    by_domain = {}
    by_source = {}
    for r in records:
        s = r.get("sentiment") or "unknown"
        by_sentiment[s] = by_sentiment.get(s, 0) + 1
        d = r.get("domain") or "unknown"
        by_domain[d] = by_domain.get(d, 0) + 1
        src = r.get("source") or "unknown"
        by_source[src] = by_source.get(src, 0) + 1
    unanalyzed = sum(1 for r in records if not r.get("analyzed"))
    return {
        "total": total,
        "unanalyzed": unanalyzed,
        "by_sentiment": by_sentiment,
        "by_domain": by_domain,
        "by_source": by_source,
    }


# ─── 分析报告 ───

_analysis_cache: Optional[list[dict[str, Any]]] = None


def _load_reports() -> list[dict[str, Any]]:
    global _analysis_cache
    if _analysis_cache is not None:
        return _analysis_cache
    reports = _read_jsonl(ANALYSIS_PATH)
    reports.sort(key=lambda r: str(r.get("generated_at") or ""), reverse=True)
    _analysis_cache = reports
    return reports


def append_report(report: dict[str, Any]) -> None:
    """追加分析报告。"""
    _append_jsonl(ANALYSIS_PATH, report)
    global _analysis_cache
    _analysis_cache = None


def get_reports(limit: int = 20) -> list[dict[str, Any]]:
    """查询分析报告列表。"""
    reports = _load_reports()
    return reports[:limit]


def get_report_by_id(report_id: str) -> Optional[dict[str, Any]]:
    """按 ID 查询分析报告。"""
    for r in _load_reports():
        if r.get("id") == report_id:
            return r
    return None


# ─── 补丁 ───

_patches_cache: Optional[list[dict[str, Any]]] = None


def _load_patches() -> list[dict[str, Any]]:
    global _patches_cache
    if _patches_cache is not None:
        return _patches_cache
    patches = _read_jsonl(PATCHES_PATH)
    patches.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    _patches_cache = patches
    return patches


def _persist_patches(patches: list[dict[str, Any]]) -> None:
    global _patches_cache
    trimmed = patches[:MAX_PATCHES]
    _write_jsonl(PATCHES_PATH, trimmed)
    _patches_cache = trimmed


def append_patch(patch: dict[str, Any]) -> None:
    """追加补丁记录。"""
    _append_jsonl(PATCHES_PATH, patch)
    global _patches_cache
    _patches_cache = None


def get_patches(
    *,
    skill_id: Optional[str] = None,
    status: Optional[str] = None,
    patch_type: Optional[str] = None,
    active_only: bool = False,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """查询补丁记录。"""
    patches = _load_patches()
    if skill_id:
        patches = [p for p in patches if p.get("target_skill_id") == skill_id]
    if status:
        patches = [p for p in patches if p.get("status") == status]
    if patch_type:
        patches = [p for p in patches if p.get("type") == patch_type]
    if active_only:
        patches = [p for p in patches if p.get("active")]
    return patches[:limit]


def get_active_patches(skill_id: Optional[str] = None) -> list[dict[str, Any]]:
    """获取已部署且活跃的补丁。"""
    patches = [p for p in _load_patches() if p.get("active") and p.get("status") == "deployed"]
    if skill_id:
        patches = [p for p in patches if p.get("target_skill_id") == skill_id]
    return patches


def update_patch_status(
    patch_id: str,
    new_status: str,
    *,
    approved_by: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """更新补丁状态。"""
    from datetime import datetime, timezone
    patches = _load_patches()
    for p in patches:
        if p.get("id") == patch_id:
            p["status"] = new_status
            if approved_by and new_status == "approved":
                p["approved_by"] = approved_by
                p["approved_at"] = datetime.now(timezone.utc).isoformat()
            if new_status == "deployed":
                p["deployed_at"] = datetime.now(timezone.utc).isoformat()
                p["active"] = True
            if new_status in ("rolled_back", "discarded"):
                p["active"] = False
            _persist_patches(patches)
            return p
    return None


def update_patch_content(
    patch_id: str,
    content: str,
    *,
    payload: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """更新补丁正文；已生效的补丁修改后退回待生效，需重新部署。"""
    from datetime import datetime, timezone

    text = (content or "").strip()
    if not text:
        return None

    patches = _load_patches()
    for p in patches:
        if p.get("id") != patch_id:
            continue
        status = str(p.get("status") or "")
        if status in ("discarded", "rolled_back"):
            return None
        p["content"] = text
        if payload is not None:
            p["payload"] = payload
        p["updated_at"] = datetime.now(timezone.utc).isoformat()
        if status == "deployed":
            p["status"] = "approved"
            p["active"] = False
        _persist_patches(patches)
        return p
    return None


# ─── 进化总览 ───


def get_evolution_overview() -> dict[str, Any]:
    """进化引擎总览（对齐前端 overview API）。"""
    feedback_stats = get_feedback_stats()
    reports = get_reports(limit=5)
    patches = _load_patches()

    pending_patches = [p for p in patches if p.get("status") in ("draft", "pending")]
    approved_patches = [p for p in patches if p.get("status") == "approved"]
    active_patches = [p for p in patches if p.get("active")]
    deployed_patches = [p for p in patches if p.get("status") == "deployed"]

    # 各技能统计
    skill_ids = set(r.get("skill_id") for r in _load_feedback())
    skill_stats = {}
    for sid in skill_ids:
        skill_feedback = [r for r in _load_feedback() if r.get("skill_id") == sid]
        skill_patches = [p for p in patches if p.get("target_skill_id") == sid]
        skill_stats[sid] = {
            "total_feedback": len(skill_feedback),
            "negative_feedback": sum(1 for r in skill_feedback if r.get("sentiment") == "negative"),
            "active_patches": sum(1 for p in skill_patches if p.get("active")),
            "total_patches": len(skill_patches),
        }

    return {
        "feedback_stats": feedback_stats,
        "recent_reports": [r.get("id") for r in reports],
        "pending_patches": len(pending_patches),
        "approved_patches": len(approved_patches),
        "active_patches": len(active_patches),
        "deployed_patches": len(deployed_patches),
        "skill_stats": skill_stats,
    }

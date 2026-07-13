"""Skill 审计存储（读写 data/agent/*.jsonl）。"""

from __future__ import annotations

import json
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from app.core.paths import data_dir
from app.db.ops_repos import append_event_stream, read_event_stream, write_event_stream
from app.db.session import is_db_enabled
from app.services.skill_audit.evolution_bridge import bridge_badcase_to_evolution

INVOCATIONS_PATH = data_dir() / "agent" / "skill-invocations.jsonl"
TUNING_PATH = data_dir() / "agent" / "skill-tuning.jsonl"
STREAM_INVOCATIONS = "skill_invocation"
STREAM_TUNING = "skill_tuning"
MAX_INVOCATIONS = 5000

_cache: list[dict[str, Any]] | None = None


def _new_id(prefix: str) -> str:
    return f"{prefix}-{int(time.time() * 1000)}-{random.randint(0, 99999):05d}"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if is_db_enabled():
        stream = STREAM_INVOCATIONS if path == INVOCATIONS_PATH else STREAM_TUNING if path == TUNING_PATH else None
        if stream:
            return read_event_stream(stream, limit=MAX_INVOCATIONS if stream == STREAM_INVOCATIONS else 5000)
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
        stream = STREAM_INVOCATIONS if path == INVOCATIONS_PATH else STREAM_TUNING if path == TUNING_PATH else None
        if stream:
            write_event_stream(stream, items)
            return
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(json.dumps(i, ensure_ascii=False) for i in items)
    path.write_text(text + ("\n" if text else ""), encoding="utf-8")


def _append_jsonl(path: Path, item: dict[str, Any]) -> None:
    if is_db_enabled():
        stream = STREAM_INVOCATIONS if path == INVOCATIONS_PATH else STREAM_TUNING if path == TUNING_PATH else None
        if stream:
            append_event_stream(stream, item)
            return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")


def _load_invocations() -> list[dict[str, Any]]:
    if is_db_enabled():
        records = [_normalize_invocation(r) for r in _read_jsonl(INVOCATIONS_PATH)]
        records.sort(key=lambda r: r.get("at", ""), reverse=True)
        return records
    global _cache
    if _cache is not None:
        return _cache
    records = [_normalize_invocation(r) for r in _read_jsonl(INVOCATIONS_PATH)]
    records.sort(key=lambda r: r.get("at", ""), reverse=True)
    _cache = records
    return records


def _persist_invocations(records: list[dict[str, Any]]) -> None:
    global _cache
    trimmed = records[:MAX_INVOCATIONS]
    _write_jsonl(INVOCATIONS_PATH, trimmed)
    _cache = trimmed


def _normalize_invocation(raw: dict[str, Any]) -> dict[str, Any]:
    outcome = raw.get("outcome")
    if not outcome:
        outcome = "api_ok" if raw.get("success") else "api_fail"
    return {
        **raw,
        "audit_status": raw.get("audit_status") or "pending",
        "outcome": outcome,
        "success": bool(raw.get("success", outcome == "api_ok")),
    }


def _in_period(iso: str, days: int) -> bool:
    if days <= 0:
        return True
    if not iso:
        return True
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return True
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt >= cutoff


def _outcome(inv: dict[str, Any]) -> str:
    return str(inv.get("outcome") or ("api_ok" if inv.get("success") else "api_fail"))


def log_skill_invocation(
    *,
    skill_id: str,
    tool: str,
    outcome: str,
    error: Optional[str] = None,
    user_message_preview: Optional[str] = None,
    response_preview: Optional[str] = None,
    task_id: Optional[str] = None,
) -> dict[str, Any]:
    """记录一次 Skill 工具调用，供 Agent 审计页复核。"""
    record = _normalize_invocation(
        {
            "id": _new_id("inv"),
            "skill_id": skill_id,
            "tool": tool,
            "outcome": outcome,
            "success": outcome == "api_ok",
            "error": error,
            "user_message_preview": (user_message_preview or "")[:300] or None,
            "response_preview": (response_preview or "")[:600] or None,
            "task_id": task_id,
            "at": datetime.now(timezone.utc).isoformat(),
            "audit_status": "pending",
        }
    )
    records = _load_invocations()
    records.insert(0, record)
    _persist_invocations(records[:MAX_INVOCATIONS])
    return record


def _stat_for_skill(
    skill_id: str,
    invocations: list[dict[str, Any]],
    tuning: list[dict[str, Any]],
) -> dict[str, Any]:
    skill_inv = [i for i in invocations if i.get("skill_id") == skill_id]
    api_calls = [i for i in skill_inv if _outcome(i) != "no_tool"]
    ok = sum(1 for i in api_calls if _outcome(i) == "api_ok")
    api_fail = sum(1 for i in skill_inv if _outcome(i) == "api_fail")
    no_tool = sum(1 for i in skill_inv if _outcome(i) == "no_tool")
    skill_tuning = [t for t in tuning if t.get("skill_id") == skill_id]
    bad_tuning = [t for t in skill_tuning if t.get("rating") == "bad"]

    return {
        "skill_id": skill_id,
        "skill_name": skill_id,
        "status": "ready",
        "invocations": len(skill_inv),
        "pending_audit": sum(1 for i in skill_inv if i.get("audit_status") == "pending"),
        "tool_success_rate": (ok / len(api_calls)) if api_calls else None,
        "api_failures": api_fail,
        "no_tool_responses": no_tool,
        "tasks": 0,
        "task_completion_rate": None,
        "active_tuning": sum(1 for t in bad_tuning if t.get("active", True)),
        "patch_count": len(bad_tuning),
    }


def get_skill_audit_overview(days: int) -> dict[str, Any]:
    """对齐 Web SkillAuditOverview 契约。"""
    period = max(0, days)
    invocations = [r for r in _load_invocations() if _in_period(str(r.get("at") or ""), period)]
    tuning = sorted(
        _read_jsonl(TUNING_PATH),
        key=lambda t: str(t.get("created_at") or ""),
        reverse=True,
    )

    pending = [i for i in invocations if i.get("audit_status") == "pending"]
    badcase = [i for i in invocations if i.get("audit_status") == "badcase"]

    skill_ids = {str(i.get("skill_id") or "unknown") for i in invocations}
    skill_ids.update(str(t.get("skill_id") or "unknown") for t in tuning)

    skills = [
        _stat_for_skill(sid, invocations, tuning)
        for sid in skill_ids
        if sid != "unknown"
    ]
    skills.sort(
        key=lambda s: (s["pending_audit"], s["invocations"], s["patch_count"]),
        reverse=True,
    )
    skills = [s for s in skills if s["invocations"] > 0 or s["patch_count"] > 0]

    return {
        "period_days": period,
        "pending_invocations": pending,
        "badcase_invocations": badcase,
        "badcase_count": len(badcase),
        "skills": skills,
        "tuning_history": tuning[:100],
    }


def audit_invocation_ok(invocation_id: str) -> Optional[dict[str, Any]]:
    records = _load_invocations()
    for r in records:
        if r.get("id") == invocation_id:
            if r.get("audit_status") != "pending":
                return None
            r["audit_status"] = "ok"
            r["audited_at"] = datetime.now(timezone.utc).isoformat()
            _persist_invocations(records)
            return r
    return None


def audit_invocation_badcase(
    invocation_id: str,
    *,
    note: Optional[str] = None,
    created_by: Optional[str] = None,
) -> dict[str, Any]:
    records = _load_invocations()
    inv = next((r for r in records if r.get("id") == invocation_id), None)
    if not inv:
        return {"error": "执行记录不存在"}
    if inv.get("audit_status") != "pending":
        return {"error": "该执行已审计"}

    inv["audit_status"] = "badcase"
    inv["audited_at"] = datetime.now(timezone.utc).isoformat()
    _persist_invocations(records)

    entry: Optional[dict[str, Any]] = None
    clean_note = (note or "").strip()
    if clean_note:
        entry = {
            "id": _new_id("tune"),
            "skill_id": inv.get("skill_id"),
            "tool": inv.get("tool"),
            "invocation_id": inv.get("id"),
            "rating": "bad",
            "issue": "badcase",
            "agent_instruction": clean_note,
            "created_by": created_by,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "active": True,
        }
        _append_jsonl(TUNING_PATH, entry)

    evolution = bridge_badcase_to_evolution(inv, note=clean_note, issue="badcase")
    if evolution.get("diagnosis"):
        inv["evolution_diagnosis"] = evolution["diagnosis"]
        inv["evolution_feedback_id"] = evolution.get("feedback_id")
        _persist_invocations(records)

    return {"invocation": inv, "entry": entry, "evolution": evolution}


def audit_invocation_with_patch(
    *,
    invocation_id: str,
    issue: str,
    agent_instruction: str,
    created_by: Optional[str] = None,
) -> dict[str, Any]:
    records = _load_invocations()
    inv = next((r for r in records if r.get("id") == invocation_id), None)
    if not inv:
        return {"error": "执行记录不存在"}
    if inv.get("audit_status") != "pending":
        return {"error": "该执行已审计"}

    status = "badcase" if issue == "badcase" else "tuned"
    inv["audit_status"] = status
    inv["audited_at"] = datetime.now(timezone.utc).isoformat()
    _persist_invocations(records)

    entry = {
        "id": _new_id("tune"),
        "skill_id": inv.get("skill_id"),
        "tool": inv.get("tool"),
        "invocation_id": inv.get("id"),
        "rating": "bad",
        "issue": issue,
        "agent_instruction": agent_instruction.strip(),
        "created_by": created_by,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "active": True,
    }
    _append_jsonl(TUNING_PATH, entry)

    evolution: Optional[dict[str, Any]] = None
    if issue == "badcase":
        evolution = bridge_badcase_to_evolution(
            inv,
            note=agent_instruction.strip(),
            issue=issue,
            run_analysis=True,
        )
        if evolution.get("diagnosis"):
            inv["evolution_diagnosis"] = evolution["diagnosis"]
            inv["evolution_feedback_id"] = evolution.get("feedback_id")
            _persist_invocations(records)

    return {"invocation": inv, "entry": entry, "evolution": evolution}


def deactivate_tuning_entry(entry_id: str) -> bool:
    entries = _read_jsonl(TUNING_PATH)
    changed = False
    for e in entries:
        if e.get("id") == entry_id:
            e["active"] = False
            changed = True
    if changed:
        _write_jsonl(TUNING_PATH, entries)
    return changed


def get_active_skill_tuning_instructions(limit: int = 10) -> list[str]:
    """返回活跃中的审计补丁文案，供 orchestrator 注入 system prompt。"""
    entries = sorted(
        _read_jsonl(TUNING_PATH),
        key=lambda t: str(t.get("created_at") or ""),
        reverse=True,
    )
    seen: set[str] = set()
    out: list[str] = []
    for e in entries:
        if not e.get("active", True):
            continue
        text = str(e.get("agent_instruction") or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= limit:
            break
    return out

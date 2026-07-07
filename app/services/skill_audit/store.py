"""Skill 审计存储（读写 data/agent/*.jsonl）。"""

from __future__ import annotations

import json
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from app.core.paths import data_dir

INVOCATIONS_PATH = data_dir() / "agent" / "skill-invocations.jsonl"
TUNING_PATH = data_dir() / "agent" / "skill-tuning.jsonl"
MAX_INVOCATIONS = 5000

_cache: list[dict[str, Any]] | None = None


def _new_id(prefix: str) -> str:
    return f"{prefix}-{int(time.time() * 1000)}-{random.randint(0, 99999):05d}"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
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
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(json.dumps(i, ensure_ascii=False) for i in items)
    path.write_text(text + ("\n" if text else ""), encoding="utf-8")


def _load_invocations() -> list[dict[str, Any]]:
    global _cache
    if _cache is not None:
        return _cache
    records = _read_jsonl(INVOCATIONS_PATH)
    records.sort(key=lambda r: r.get("at", ""), reverse=True)
    _cache = records
    return records


def _persist_invocations(records: list[dict[str, Any]]) -> None:
    global _cache
    trimmed = records[:MAX_INVOCATIONS]
    _write_jsonl(INVOCATIONS_PATH, trimmed)
    _cache = trimmed


def get_skill_audit_overview(days: int) -> dict[str, Any]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(0, days))
    invocations = [
        r
        for r in _load_invocations()
        if not r.get("at") or datetime.fromisoformat(r["at"].replace("Z", "+00:00")) >= cutoff
    ]
    by_skill: dict[str, dict[str, int]] = {}
    for inv in invocations:
        sid = inv.get("skill_id") or "unknown"
        bucket = by_skill.setdefault(sid, {"total": 0, "ok": 0, "fail": 0, "pending": 0})
        bucket["total"] += 1
        status = inv.get("audit_status") or "pending"
        if status == "ok":
            bucket["ok"] += 1
        elif status in ("badcase", "tuned"):
            bucket["fail"] += 1
        else:
            bucket["pending"] += 1
    skills = [
        {"skill_id": sid, **counts, "name": sid}
        for sid, counts in by_skill.items()
    ]
    tuning = _read_jsonl(TUNING_PATH)
    return {
        "skills": skills,
        "invocations": invocations[:200],
        "tuning": [t for t in tuning if t.get("active", True)],
        "period_days": days,
    }


def audit_invocation_ok(invocation_id: str) -> Optional[dict[str, Any]]:
    records = _load_invocations()
    for r in records:
        if r.get("id") == invocation_id:
            r["audit_status"] = "ok"
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
    for r in records:
        if r.get("id") == invocation_id:
            r["audit_status"] = "badcase"
            r["audit_note"] = note
            r["audited_by"] = created_by
            _persist_invocations(records)
            return {"invocation": r}
    return {"error": "执行记录不存在"}


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
    inv["audit_status"] = "tuned"
    _persist_invocations(records)
    entry = {
        "id": _new_id("tune"),
        "skill_id": inv.get("skill_id"),
        "tool": inv.get("tool"),
        "issue": issue,
        "agent_instruction": agent_instruction,
        "created_by": created_by,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "active": True,
    }
    TUNING_PATH.parent.mkdir(parents=True, exist_ok=True)
    with TUNING_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return {"invocation": inv, "tuning": entry}


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

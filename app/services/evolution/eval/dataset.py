"""Shadow eval 样本集（来自 evolution feedback）。"""

from __future__ import annotations

from typing import Any, Optional

from app.services.evolution.eval.case_enrich import enrich_correction_case
from app.services.evolution.store import get_feedback_records


def load_correction_cases(
    skill_id: str,
    *,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """加载人工纠正样本（真 badcase）。"""
    records = get_feedback_records(skill_id=skill_id, limit=limit * 3)
    out: list[dict[str, Any]] = []
    for row in records:
        intent = str(row.get("feedback_intent") or "").strip()
        sentiment = str(row.get("sentiment") or "").strip()
        correction = str(row.get("correction_value") or row.get("human_decision_preview") or "").strip()
        if not correction:
            continue
        if intent not in ("correction", "") and sentiment != "negative":
            if intent != "correction":
                continue
        out.append(enrich_correction_case(row))
        if len(out) >= limit:
            break
    return out

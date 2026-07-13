"""部署后指标：将 override / adoption 反馈关联到已部署补丁。"""

from __future__ import annotations

from typing import Any

from app.services.evolution.auto_deploy import should_apply_patch
from app.services.evolution.eval.metrics import append_deploy_metric
from app.services.evolution.store import get_active_patches

_OVERRIDE_INTENTS = frozenset({"correction"})
_OVERRIDE_SOURCES = frozenset({"auto_override", "auto_dismiss", "manual_audit"})


def _context_key_for_feedback(item: dict[str, Any]) -> str:
    meta = item.get("context_meta") if isinstance(item.get("context_meta"), dict) else {}
    for key in ("ord_line_no", "goods_id", "title", "product_title"):
        val = str(meta.get(key) or item.get(key) or "").strip()
        if val:
            return val
    return str(item.get("context_ref") or "").strip() or "default"


def _is_override_feedback(item: dict[str, Any]) -> bool:
    intent = str(item.get("feedback_intent") or "").strip()
    if intent in _OVERRIDE_INTENTS:
        return True
    return (
        str(item.get("sentiment") or "") == "negative"
        and str(item.get("source") or "") in _OVERRIDE_SOURCES
    )


def _is_confirmation_feedback(item: dict[str, Any]) -> bool:
    intent = str(item.get("feedback_intent") or "").strip()
    if intent == "confirmation":
        return True
    return str(item.get("sentiment") or "") == "positive"


def track_deploy_feedback(item: dict[str, Any]) -> None:
    """已部署补丁在灰度桶内收到纠正/采纳时写入 post_deploy_override 指标。"""
    skill_id = str(item.get("skill_id") or "").strip()
    if not skill_id:
        return

    is_override = _is_override_feedback(item)
    is_confirm = _is_confirmation_feedback(item)
    if not is_override and not is_confirm:
        return

    context_key = _context_key_for_feedback(item)
    value = 1.0 if is_override else 0.0

    for patch in get_active_patches(skill_id=skill_id):
        if patch.get("status") != "deployed":
            continue
        gray = int(patch.get("gray_percent") or 0)
        if gray <= 0:
            continue
        if not should_apply_patch(context_key, gray):
            continue
        append_deploy_metric(
            str(patch.get("id") or ""),
            metric="post_deploy_override",
            value=value,
            context={
                "skill_id": skill_id,
                "context_key": context_key,
                "feedback_intent": item.get("feedback_intent"),
                "source": item.get("source"),
            },
        )

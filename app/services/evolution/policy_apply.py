"""进化补丁运行时注入（policy patch）。"""

from __future__ import annotations

from typing import Any

from app.services.evolution.auto_deploy import should_apply_patch


def apply_keyword_boost_to_candidates(
    title: str,
    candidates: list[dict[str, Any]],
    *,
    context_key: str,
    patches: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not candidates or not patches:
        return candidates
    boosted = [dict(c) for c in candidates]
    title_raw = title or ""
    for patch in patches:
        gray = int(patch.get("gray_percent") or 100)
        if not should_apply_patch(context_key or title_raw, gray):
            continue
        payload = patch.get("payload") if isinstance(patch.get("payload"), dict) else {}
        kb = payload.get("keyword_boost") if isinstance(payload.get("keyword_boost"), dict) else {}
        keywords = [str(k).strip() for k in (kb.get("trigger_keywords") or []) if str(k).strip()]
        target = str(kb.get("target_category_name") or "").strip().lower()
        if not keywords or not target:
            continue
        if not any(k in title_raw for k in keywords):
            continue
        factor = float(kb.get("boost_factor") or 0.28)
        for row in boosted:
            name = str(
                row.get("category_cn_name")
                or row.get("cn_name")
                or row.get("declare_cn_name")
                or ""
            ).lower()
            if target in name or name in target:
                conf = float(row.get("confidence") or row.get("score") or 0.5)
                row["confidence"] = min(0.99, conf + factor)
                row["evolution_boost"] = patch.get("id")
        boosted.sort(
            key=lambda x: float(x.get("confidence") or x.get("score") or 0),
            reverse=True,
        )
    return boosted


def resolve_threshold_for_skill(
    skill_id: str,
    default: float,
    context_key: str,
    patches: list[dict[str, Any]],
    *,
    threshold_key: str = "gross_margin_threshold",
) -> float:
    best = default
    for patch in patches:
        if patch.get("target_skill_id") != skill_id:
            continue
        if str(patch.get("type") or "") != "threshold_adjust":
            continue
        gray = int(patch.get("gray_percent") or 100)
        if not should_apply_patch(context_key, gray):
            continue
        payload = patch.get("payload") if isinstance(patch.get("payload"), dict) else {}
        patch_key = str(payload.get("threshold_key") or "").strip()
        if patch_key and patch_key != threshold_key:
            continue
        try:
            best = float(payload.get("new_value") or best)
        except (TypeError, ValueError):
            continue
    return best

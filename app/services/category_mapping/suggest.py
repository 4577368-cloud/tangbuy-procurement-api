"""品类映射 suggest（含英文标题翻译）。"""

from __future__ import annotations

from typing import Any, Optional

from app.services.category_mapping.similar_enrich import enrich_suggest_response
from app.services.category_mapping.vision_pipeline import run_category_mapping_suggest_with_vision
from app.services.category_mapping.title_translate import prepare_title_for_mapping


def run_category_mapping_suggest(
    title: str,
    *,
    hint: Optional[str] = None,
    goods_id: Optional[str] = None,
    image_url: Optional[str] = None,
    skip_history: bool = False,
    hint_as_reference: bool = False,
    ord_line_no: Optional[str] = None,
    context_key: Optional[str] = None,
) -> dict[str, Any]:
    prep = prepare_title_for_mapping(title, hint=hint if not hint_as_reference else None)
    result = dict(
        run_category_mapping_suggest_with_vision(
            prep.title,
            hint=hint,
            goods_id=goods_id,
            image_url=image_url,
            skip_history=skip_history,
            hint_as_reference=hint_as_reference,
        )
    )

    if prep.was_translated:
        result["source_title_original"] = prep.original_title
        result["title_zh"] = prep.title
        result["title_translated"] = True
        detail = (result.get("match_detail") or "").strip()
        suffix = f"英文标题已译：{prep.title}"
        result["match_detail"] = f"{detail}；{suffix}" if detail else suffix
        if not result.get("match_method"):
            result["match_method"] = "title_translated"

    out = enrich_suggest_response(
        result,
        title=prep.title,
        hint=hint if not hint_as_reference else None,
    )
    return _apply_evolution_boost(
        out,
        prep.title,
        gray_context_key=(ord_line_no or context_key or goods_id or prep.title or "").strip(),
    )


def _apply_evolution_boost(
    result: dict[str, Any],
    title: str,
    *,
    gray_context_key: str,
) -> dict[str, Any]:
    try:
        from app.services.evolution.patch_generator import get_active_keyword_boost_patches
        from app.services.evolution.policy_apply import apply_keyword_boost_to_candidates

        patches = get_active_keyword_boost_patches()
        if not patches:
            return result
        for key in ("semantic_candidates", "candidates"):
            rows = result.get(key)
            if isinstance(rows, list) and rows:
                result[key] = apply_keyword_boost_to_candidates(
                    title,
                    rows,
                    context_key=gray_context_key or title,
                    patches=patches,
                )
        primary = result.get("hs_mapping") if isinstance(result.get("hs_mapping"), dict) else None
        if not primary:
            sem = result.get("semantic_candidates")
            if isinstance(sem, list) and sem:
                top = sem[0]
                if top.get("evolution_boost"):
                    result["match_method"] = result.get("match_method") or "evolution_boost"
    except Exception:
        pass
    return result

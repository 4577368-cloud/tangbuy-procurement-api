"""品类映射 suggest（含英文标题翻译）。"""

from __future__ import annotations

from typing import Any, Optional

from app.services.category_mapping.vision_pipeline import run_category_mapping_suggest_with_vision
from app.services.category_mapping.title_translate import prepare_title_for_mapping


def run_category_mapping_suggest(
    title: str,
    *,
    hint: Optional[str] = None,
    goods_id: Optional[str] = None,
    image_url: Optional[str] = None,
) -> dict[str, Any]:
    prep = prepare_title_for_mapping(title, hint=hint)
    result = dict(
        run_category_mapping_suggest_with_vision(
            prep.title,
            hint=prep.hint,
            goods_id=goods_id,
            image_url=image_url,
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

    return result

"""商品主图视觉理解 → 注入品类映射 suggest（对齐 procurement-demo vision-pipeline.ts）。"""

from __future__ import annotations

from typing import Any, Optional

from app.core.config import get_settings
from app.integrations.skill_cli import run_category_lookup, run_category_suggest
from app.services.agent.llm import parse_json_from_llm, vision_chat_completion


def _analyze_product_image(
    image_url: str,
    title: str,
    hint: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    prompt = (
        "你是跨境报关品类识别专家。请观察商品主图，结合标题判断商品类型，输出 JSON（不要其它文字）：\n"
        "{\n"
        '  "visual_summary": "从图片看到的客观描述（材质、品类、用途，30字内）",\n'
        '  "product_type": "中文品类名，如：蓝牙耳机、腰带、运动鞋",\n'
        '  "category_keywords": ["关键词1","关键词2","关键词3"]\n'
        "}\n\n"
        f"1688标题：{title}\n"
        + (f"平台类目提示：{hint}\n" if hint else "")
        + "要求：category_keywords 用于匹配海关申报品类，优先具体名词。"
    )
    try:
        raw = vision_chat_completion(prompt, image_url)
        parsed = parse_json_from_llm(raw)
        if not parsed:
            return None
        keywords = parsed.get("category_keywords")
        kw_list = [str(x) for x in keywords if x] if isinstance(keywords, list) else []
        summary = str(parsed.get("visual_summary") or "").strip()
        product_type = str(parsed.get("product_type") or "").strip()
        if not summary and not product_type and not kw_list:
            return None
        return {
            "visual_summary": summary,
            "product_type": product_type,
            "category_keywords": kw_list,
        }
    except Exception:
        return None


def _rerank_candidates_with_vision(
    image_url: str,
    title: str,
    visual_summary: str,
    current: dict[str, Any],
) -> Optional[dict[str, Any]]:
    candidates = (current.get("semantic_candidates") or current.get("candidates") or [])[:6]
    if len(candidates) < 2:
        return None

    list_text = "\n".join(
        f"{i + 1}. cid={c.get('category_id')} {c.get('category_cn_name') or c.get('label') or ''} "
        f"HS={c.get('hs_code') or '—'}"
        for i, c in enumerate(candidates)
    )
    prompt = (
        "根据商品图片和标题，从下列已检索出的海关品类候选中选择最准确的一项。\n"
        '只输出 JSON：{"category_id": 数字, "reason": "一句话"}\n\n'
        f"标题：{title}\n"
        f"图片理解：{visual_summary}\n\n"
        f"候选：\n{list_text}"
    )
    try:
        raw = vision_chat_completion(prompt, image_url, max_tokens=300)
        parsed = parse_json_from_llm(raw)
        if not parsed:
            return None
        picked_id = int(parsed.get("category_id") or 0)
        if not picked_id:
            return None
        if not any(int(c.get("category_id") or 0) == picked_id for c in candidates):
            return None
        lookup = run_category_lookup(picked_id)
        if not lookup.get("success"):
            return None
        reason = str(parsed.get("reason") or "图片与标题综合判断")
        sem = list(current.get("semantic_candidates") or [])
        reranked: list[dict[str, Any]] = []
        for c in sem:
            row = dict(c)
            if int(row.get("category_id") or 0) == picked_id:
                row["reason"] = reason
                row["confidence"] = max(float(row.get("confidence") or 0), 0.9)
            reranked.append(row)
        reranked.sort(key=lambda x: float(x.get("confidence") or 0), reverse=True)
        for i, row in enumerate(reranked):
            row["rank"] = i + 1

        merged = {**lookup, **current}
        merged.update(
            {
                "confidence": 0.9,
                "match_method": "image_vl_rerank",
                "match_detail": reason,
                "semantic_candidates": reranked or sem,
                "decision": "semantic_agreement",
                "vision_summary": visual_summary,
            }
        )
        return merged
    except Exception:
        return None


def _vision_conflicts_with_pick(
    vision_keywords: list[str],
    top: dict[str, Any],
) -> bool:
    """识图/标题已指向包袋，但当前首选却是玩具等非包类目。"""
    if not vision_keywords:
        return False
    bag_signals = ("包", "胸包", "斜挎", "斜跨", "背包", "挎包", "腰包", "拎包")
    toy_signals = ("玩具", "益智", "积木", "玩偶")
    apparel_signals = ("卫衣", "毛衣", "针织", "T恤", "连衣裙")
    kw_blob = " ".join(vision_keywords)
    has_bag_signal = any(s in kw_blob for s in bag_signals)
    if not has_bag_signal:
        return False
    top_blob = f"{top.get('category_cn_name') or ''} {top.get('declare_cn_name') or ''}"
    has_bag_cat = any(s in top_blob for s in bag_signals)
    has_toy_cat = any(s in top_blob for s in toy_signals)
    has_apparel_cat = any(s in top_blob for s in apparel_signals)
    return (has_toy_cat or has_apparel_cat) and not has_bag_cat


def run_category_mapping_suggest_with_vision(
    title: str,
    *,
    hint: Optional[str] = None,
    goods_id: Optional[str] = None,
    image_url: Optional[str] = None,
    skip_history: bool = False,
    hint_as_reference: bool = False,
) -> dict[str, Any]:
    vision_summary: Optional[str] = None
    vision_keywords: list[str] = []
    used_vision = False
    platform_hint = (hint or "").strip()
    merged_hint = platform_hint

    image = (image_url or "").strip()
    if image and get_settings().llm_configured:
        analysis = _analyze_product_image(image, title, platform_hint or None)
        if analysis:
            used_vision = True
            vision_summary = analysis.get("visual_summary") or None
            vision_keywords = [
                x
                for x in [
                    analysis.get("product_type"),
                    *(analysis.get("category_keywords") or []),
                ]
                if x
            ]
            if not hint_as_reference:
                extra = "，".join(vision_keywords)
                merged_hint = "，".join([x for x in [merged_hint, extra] if x])

    result = dict(
        run_category_suggest(
            title,
            hint=merged_hint or None,
            goods_id=goods_id,
            image_url=image_url,
            vision_keywords=vision_keywords or None,
            skip_history=skip_history,
            hint_as_reference=hint_as_reference,
            platform_hint=platform_hint or None,
        )
    )

    should_rerank = (
        used_vision
        and image
        and result.get("success")
        and len((result.get("semantic_candidates") or result.get("candidates") or [])) >= 2
        and (
            result.get("decision") != "history_hit"
            or _vision_conflicts_with_pick(vision_keywords, result)
        )
    )
    if should_rerank:
        reranked = _rerank_candidates_with_vision(image, title, vision_summary or "", result)
        if reranked:
            result = reranked

    if used_vision and result.get("success"):
        result["vision_summary"] = vision_summary
        result["vision_keywords"] = vision_keywords
        merged_kw = [
            *(result.get("matched_keywords") or []),
            *vision_keywords,
            *(result.get("title_image_agreement_keywords") or []),
        ]
        result["matched_keywords"] = list(dict.fromkeys(merged_kw))[:12]

    return result

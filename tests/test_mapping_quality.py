"""mapping_quality 单元测试。"""

from __future__ import annotations

from unittest.mock import patch

from app.services.category_mapping.mapping_quality import (
    assess_mapping_quality,
    catalog_leaf_incoherent,
    hint_conflicts_title,
    hs_aligns_with_agreement_terms,
    mapping_aligns_with_title,
    title_vision_agreement_terms,
)
from app.services.category_mapping.vision_pipeline import _rerank_candidates_with_vision


def test_pants_title_rejects_sweater_hs():
    title = "春秋款运动裤男爸爸春装裤子松紧腰中老年人直筒老人男裤春季长裤"
    hs = {
        "category_cn_name": "卫衣",
        "declare_cn_name": "卫衣",
        "hs_code": "6110200090",
    }
    ok, detail, score = mapping_aligns_with_title(title, hs)
    assert not ok
    assert score < 0.2
    assert "冲突" in detail or "不一致" in detail


def test_pants_title_accepts_sweatpants_hs():
    title = "春秋款运动裤男爸爸春装裤子松紧腰中老年人直筒老人男裤春季长裤"
    hs = {
        "category_cn_name": "运动裤",
        "declare_cn_name": "裤子",
        "hs_code": "6204630000",
    }
    ok, _, score = mapping_aligns_with_title(title, hs)
    assert ok
    assert score >= 0.55


def test_bag_title_rejects_toy_hs():
    title = "厂家小众设计休闲旅游户外运动男生大容量潮流时尚单肩胸包斜跨包"
    hs = {
        "category_cn_name": "户外运动/休闲/传统玩具",
        "declare_cn_name": "益智玩具",
        "hs_code": "9503008900",
    }
    ok, _, _ = mapping_aligns_with_title(title, hs)
    assert not ok


def test_hint_conflicts_title_pants_vs_sweater():
    title = "春秋款运动裤男爸爸春装裤子松紧腰中老年人直筒老人男裤春季长裤"
    assert hint_conflicts_title(title, "卫衣")
    assert not hint_conflicts_title(title, "运动裤")


def test_local_cache_not_auto_pass_when_mismatch():
    title = "厂家小众设计休闲旅游户外运动男生大容量潮流时尚单肩胸包斜跨包"
    hs = {
        "category_cn_name": "户外运动/休闲/传统玩具",
        "declare_cn_name": "益智玩具",
        "hs_code": "9503008900",
    }
    q = assess_mapping_quality(title, hs, match_method="local_item_mapped", confidence=1.0)
    assert not q["auto_pass_eligible"]


def test_fish_pet_supplies_leaf_incoherent():
    hs = {
        "category_id": 50003251,
        "category_cn_name": "鱼",
        "declare_cn_name": "宠物用品",
        "hs_code": "3926909090",
    }
    assert catalog_leaf_incoherent(hs)
    title = "宠物厂家直销宠物用品亚马逊爆款宠物狗窝猫窝四季保暖毛绒宠物窝"
    vision = ["宠物窝", "宠物用品", "狗窝", "猫窝"]
    ok, detail, _ = mapping_aligns_with_title(title, hs, vision_keywords=vision)
    assert not ok
    assert "不一致" in detail or "不匹配" in detail or "印证" in detail
    q = assess_mapping_quality(
        title,
        hs,
        vision_keywords=vision,
        match_method="image_vl_rerank",
        confidence=0.9,
    )
    assert not q["auto_pass_eligible"]


def test_title_vision_agreement_and_cat_bed_align():
    title = "宠物厂家直销宠物用品亚马逊爆款宠物狗窝猫窝四季保暖毛绒宠物窝"
    vision = ["宠物窝", "狗窝", "猫窝", "毛绒垫"]
    agree = title_vision_agreement_terms(title, vision)
    assert "猫窝" in agree or "狗窝" in agree or "宠物窝" in agree
    hs_ok = {
        "category_cn_name": "猫窝/屋/帐篷",
        "declare_cn_name": "猫窝",
        "hs_code": "9404909000",
    }
    assert hs_aligns_with_agreement_terms(hs_ok, agree)
    q = assess_mapping_quality(
        title,
        hs_ok,
        vision_keywords=vision,
        match_method="image_vl_rerank",
        confidence=0.9,
    )
    assert q["auto_pass_eligible"]


def test_nan_hs_blocks_auto_pass():
    title = "宠物狗窝猫窝四季保暖毛绒宠物窝"
    vision = ["宠物窝", "猫窝"]
    hs = {
        "category_cn_name": "猫窝/屋/帐篷",
        "declare_cn_name": "猫窝",
        "hs_code": "nan",
    }
    q = assess_mapping_quality(
        title,
        hs,
        vision_keywords=vision,
        match_method="image_vl_rerank",
        confidence=0.9,
    )
    assert not q["auto_pass_eligible"]
    assert "HS" in q["detail"]


def test_vision_only_match_not_auto_pass():
    """标题无印证、仅识图词命中申报名 → 不过自动放行。"""
    title = "春季新款时尚百搭"
    vision = ["猫窝", "宠物窝"]
    hs = {
        "category_cn_name": "其他塑料制品",
        "declare_cn_name": "宠物窝",
        "hs_code": "3926909090",
    }
    q = assess_mapping_quality(
        title,
        hs,
        vision_keywords=vision,
        match_method="image_vl_rerank",
        confidence=0.95,
    )
    assert not q["auto_pass_eligible"]


def test_vl_rerank_merge_keeps_lookup_category():
    current = {
        "success": True,
        "category_id": 50003251,
        "category_cn_name": "鱼",
        "declare_cn_name": "宠物用品",
        "hs_code": "3926909090",
        "semantic_candidates": [
            {
                "category_id": 201829117,
                "category_cn_name": "猫窝/屋/帐篷",
                "hs_code": "nan",
                "confidence": 0.5,
            },
            {
                "category_id": 50003251,
                "category_cn_name": "鱼",
                "hs_code": "3926909090",
                "confidence": 0.8,
            },
        ],
        "decision": "scored",
    }
    lookup = {
        "success": True,
        "category_id": 201829117,
        "category_cn_name": "猫窝/屋/帐篷",
        "category_en_name": "Cat bed",
        "hs_code": "nan",
        "declare_cn_name": "nan",
        "declare_en_name": "nan",
        "tariff": None,
    }
    with (
        patch(
            "app.services.category_mapping.vision_pipeline.vision_chat_completion",
            return_value='{"category_id": 201829117, "reason": "毛绒宠物窝"}',
        ),
        patch(
            "app.services.category_mapping.vision_pipeline.parse_json_from_llm",
            return_value={"category_id": 201829117, "reason": "毛绒宠物窝"},
        ),
        patch(
            "app.services.category_mapping.vision_pipeline.run_category_lookup",
            return_value=lookup,
        ),
    ):
        merged = _rerank_candidates_with_vision(
            "https://example.com/a.jpg",
            "宠物狗窝猫窝",
            "毛绒宠物窝",
            current,
        )
    assert merged is not None
    assert merged["category_id"] == 201829117
    assert merged["category_cn_name"] == "猫窝/屋/帐篷"
    assert merged["vl_picked_category_id"] == 201829117

"""mapping_quality 单元测试。"""

from __future__ import annotations

from app.services.category_mapping.mapping_quality import (
    assess_mapping_quality,
    hint_conflicts_title,
    mapping_aligns_with_title,
)


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

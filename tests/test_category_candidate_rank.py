"""品类候选排序与地域修饰词测试。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from category_heuristics import (  # noqa: E402
    evidence_multiplier,
    term_catalog_extension_mismatch,
    term_catalog_extension_penalty,
)
from category_mapper import (  # noqa: E402
    GEOGRAPHIC_REGION_TERMS,
    THEME_SERIES_TERMS,
    _best_catalog_for_term,
    _finalize_semantic_candidate_ranks,
    classify_term,
    load_data,
    rank_semantic_candidates,
    tokenize,
)


def test_finalize_ranks_by_confidence():
    items = [
        {"label": "非洲", "confidence": 0.56, "rank": 1},
        {"label": "假发", "confidence": 0.9, "rank": 3},
        {"label": "发套", "confidence": 0.49, "rank": 2},
    ]
    out = _finalize_semantic_candidate_ranks(items)
    assert out[0]["label"] == "假发"
    assert out[0]["rank"] == 1
    assert out[1]["label"] == "非洲"
    assert out[2]["label"] == "发套"


def test_africa_classified_as_attribute():
    assert classify_term("非洲") == "attribute"
    assert "非洲" in GEOGRAPHIC_REGION_TERMS


def test_ocean_theme_classified_as_attribute():
    assert classify_term("海洋") == "attribute"
    assert "海洋" in THEME_SERIES_TERMS


def test_book_declare_needs_book_evidence():
    weak = evidence_multiplier(
        "热卖藏银 1款混装海洋系列合金饰品配件DIY项链手链挂件工厂直销",
        "海洋",
        "教育书",
        ["项链吊坠", "合金吊坠", "饰品配件"],
    )
    strong = evidence_multiplier(
        "小学海洋科普教育书籍课外读物",
        "海洋",
        "教育书",
        ["图书"],
    )
    assert weak <= 0.12
    assert strong >= 0.5


def test_ocean_series_jewelry_not_education_book():
    """「海洋系列」饰品配件不得以 海洋·教育书 作为高置信候选项。"""
    catalog, *_ = load_data()
    title = "热卖藏银 1款混装海洋系列合金饰品配件DIY项链手链挂件工厂直销"
    vision = ["项链吊坠", "合金吊坠", "饰品配件", "珠宝首饰配件"]
    cands = rank_semantic_candidates(title, "", vision, catalog)
    assert cands
    labels = [c["label"] for c in cands]
    assert "海洋" not in labels or float(next(c for c in cands if c["label"] == "海洋")["confidence"]) < 0.25
    top_labels = labels[:2]
    assert any(
        any(k in (c.get("label") or "") or k in (c.get("category_cn_name") or "") or k in (c.get("declare_cn_name") or "")
            for k in ("配件", "饰品", "项链", "吊坠", "挂件"))
        for c in cands[:2]
    ), top_labels
    for c in cands:
        if c.get("declare_cn_name") == "教育书":
            assert float(c["confidence"]) < 0.25


def test_jewelry_term_not_mapped_to_jewelry_box():
    assert term_catalog_extension_mismatch("首饰", "首饰盒", "首饰盒", "珍珠项链女锁骨链")
    assert term_catalog_extension_penalty("首饰", "首饰盒", "首饰盒", "珍珠项链女锁骨链") >= 0.5
    assert not term_catalog_extension_mismatch("首饰", "首饰套装及其他", "首饰", "珍珠项链女锁骨链")

    catalog, *_ = load_data()
    title = "珍珠项链女锁骨链简约气质首饰"
    picked = _best_catalog_for_term("首饰", title, tokenize(title), "", catalog, [])
    assert picked is not None
    _, entry = picked
    assert "盒" not in entry["cn_name"]
    assert entry.get("dec_cn_name") == "首饰" or "首饰" in entry.get("cn_name", "")

    cands = rank_semantic_candidates(title, "", [], catalog)
    top = cands[0]
    assert top["label"] in ("珍珠", "项链", "首饰", "锁骨链")
    jewelry = next(c for c in cands if c["label"] == "首饰")
    assert "盒" not in jewelry["category_cn_name"]

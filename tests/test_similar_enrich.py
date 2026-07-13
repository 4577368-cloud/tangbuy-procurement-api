"""Tests for category mapping similar enrich."""

from app.services.category_mapping.similar_enrich import (
    _auth_search_query,
    _filter_auth_hits,
    enrich_suggest_response,
)


def test_enrich_attaches_similar_and_excludes_primary():
    result = {
        "success": True,
        "category_id": 100,
        "category_cn_name": "卫衣",
        "hs_code": "6110200090",
        "semantic_candidates": [
            {
                "category_id": 100,
                "category_cn_name": "卫衣",
                "hs_code": "6110200090",
                "score": 0.9,
                "label": "卫衣",
            },
            {
                "category_id": 200,
                "category_cn_name": "毛衣",
                "hs_code": "6110110000",
                "score": 0.7,
                "label": "毛衣",
            },
        ],
    }
    out = enrich_suggest_response(result, title="针织卫衣")
    assert out["similar"]
    assert all(item["category_id"] != 100 for item in out["similar"])
    assert out["similar"][0]["category_id"] == 200


def test_enrich_keeps_authoritative_near_list():
    result = {"success": False, "error": "未能匹配到合适品类"}
    out = enrich_suggest_response(result, title="双肩背包")
    assert "authoritative_near" in out
    assert isinstance(out["authoritative_near"], list)


def test_auth_search_query_uses_semantic_not_full_title():
    result = {
        "success": True,
        "hs_code": "7117190000",
        "semantic_candidates": [
            {
                "label": "项链",
                "category_cn_name": "珍珠",
                "declare_cn_name": "项链",
            }
        ],
    }
    q = _auth_search_query(
        result,
        "跨境外贸爆款珍珠项链女锁骨链简约气质首饰",
        "",
    )
    assert "项链" in q
    assert "跨境" not in q
    assert "爆款" not in q


def test_filter_auth_hits_drops_unrelated():
    hits = [
        {
            "hs_code": "4202920000",
            "declare_cn_name": "首饰盒",
            "names": ["首饰盒", "塑料盒"],
            "score": 2.1,
        },
        {
            "hs_code": "7117190000",
            "declare_cn_name": "珍珠项链",
            "names": ["项链", "珍珠制品"],
            "score": 4.5,
        },
    ]
    out = _filter_auth_hits(hits, query="项链 珍珠", primary_hs="7117190000")
    codes = [h["hs_code"] for h in out]
    assert "7117190000" in codes
    assert "4202920000" not in codes

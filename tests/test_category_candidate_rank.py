"""品类候选排序与地域修饰词测试。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from category_heuristics import (  # noqa: E402
    term_catalog_extension_mismatch,
    term_catalog_extension_penalty,
)
from category_mapper import (  # noqa: E402
    GEOGRAPHIC_REGION_TERMS,
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
    assert top["label"] in ("珍珠", "项链")
    jewelry = next(c for c in cands if c["label"] == "首饰")
    assert "盒" not in jewelry["category_cn_name"]

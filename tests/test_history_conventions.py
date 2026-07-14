"""历史订单类目惯例沉淀测试。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from category_heuristics import (  # noqa: E402
    _history_title_terms,
    build_history_conventions,
    clear_history_conventions_cache,
    lookup_history_conventions_for_text,
)
from category_mapper import load_data, rank_semantic_candidates  # noqa: E402


def test_history_title_terms_keeps_pet_bed_atoms():
    terms = _history_title_terms(
        "狗窝四季通用泰迪比熊宠物垫子小型大型犬狗狗用品狗床猫窝冬季宠物窝"
    )
    assert "猫窝" in terms
    assert "狗窝" in terms
    assert "宠物窝" in terms or "宠物垫" in terms
    assert "熊宠物垫" not in terms
    assert "狗床猫窝" not in terms


def test_build_conventions_pet_bed_dominant():
    catalog, history, *_ = load_data()
    # 用全量 history 太重时也可抽样；此处数据已就绪且构建 <2s
    conv = build_history_conventions(history, catalog.get("by_cid") or {})
    dom = (conv.get("dominant") or {}).get("猫窝")
    assert dom is not None
    assert dom["count"] >= 10
    assert "宠物" in str(dom.get("category_cn_name") or "") or "垫" in str(
        dom.get("category_cn_name") or ""
    )


def test_rank_injects_history_convention_not_fish():
    clear_history_conventions_cache()
    catalog, *_ = load_data()
    title = "宠物厂家直销宠物用品亚马逊爆款宠物狗窝猫窝四季保暖毛绒宠物窝"
    vision = ["宠物窝", "猫窝", "狗窝", "毛绒垫"]
    hits = lookup_history_conventions_for_text(title, vision)
    assert any(h.get("term") == "猫窝" for h in hits)
    cands = rank_semantic_candidates(title, "", vision, catalog)
    assert cands
    top = cands[0]
    cn = str(top.get("category_cn_name") or "")
    assert cn != "鱼"
    assert "history" in (top.get("sources") or []) or top.get("history_convention")
    assert "宠物" in cn or "垫" in cn or "窝" in cn

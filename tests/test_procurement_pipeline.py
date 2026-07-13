"""procurement pipeline 与 prepare 阶段单元测试。"""

from __future__ import annotations

from app.services.orders.procurement_pipeline import _step_followup_monitor, parse_admin_error_blockers
from app.services.orders.procurement_release import evaluate_prepare_stage


def _row_stat_23() -> dict:
    return {
        "ord_line_no": "TI26070000151",
        "ord_no": "TO26070000151",
        "ord_line_stat": 23,
        "_store_source": "alibaba",
        "shop_pltf_cd": "1688",
        "lvl1_ctgy_nm": "路由器",
        "ord_cnt": 1,
        "pur_amt": 100.0,
        "post_fee": 10.0,
        "ds_ord_amt": 50.0,
        "pur_prc": 100.0,
        "item_url": "https://detail.1688.com/offer/1.html",
        "usr_id": 12345,
        "note_block_procurement": False,
    }


def test_prepare_stage_blocks_category_other():
    row = {**_row_stat_23(), "lvl1_ctgy_nm": "其他"}
    result = evaluate_prepare_stage(row)
    assert result["all_clear"] is False
    keys = [b["key"] for b in result["blockers"]]
    assert "CATEGORY_OTHER" in keys


def test_prepare_stage_does_not_block_low_margin():
    row = {**_row_stat_23(), "ds_ord_amt": 10.0}
    result = evaluate_prepare_stage(row)
    margin_conds = [c for c in result["conditions"] if c["key"] == "margin"]
    assert margin_conds == []


def test_prepare_stage_blocks_note():
    row = {
        **_row_stat_23(),
        "note_block_procurement": True,
        "note_tier": "high",
        "note_classify_reason": "要L码不要S码",
    }
    result = evaluate_prepare_stage(row)
    assert result["all_clear"] is False
    assert any(b["key"] == "NOTE_BLOCK" for b in result["blockers"])


def test_followup_monitor_returns_dict():
    row = {
        "ord_line_no": "TI26070000999",
        "ord_line_stat": 22,
        "pur_time": "2026-07-01T00:00:00Z",
    }
    result = _step_followup_monitor(row)
    assert isinstance(result, dict)
    assert "ok" in result
    assert result.get("step") == "followup"


def test_parse_admin_error_stock():
    blockers = parse_admin_error_blockers("商品库存不足，无法下单", stage="place_order")
    assert any(b["key"] == "ADMIN_STOCK" for b in blockers)


def test_parse_admin_error_moq():
    blockers = parse_admin_error_blockers("起订量不满足要求", stage="place_order")
    assert any(b["key"] == "ADMIN_MOQ" for b in blockers)


def test_parse_admin_error_sku_summarized():
    raw = (
        "失败 1 个 1688 订单:[错误信息：PO=TO26070000100; PI=TI26070000160; skuid=5659749404921 ;"
        "请确认！ sku属性信息不匹配，需要颜色啊:白色; 1688查询的是[颜色:白色, 尺码:S];"
        "TOs=TO26070000100TIs=TI26070000160], 成功：[]"
    )
    blockers = parse_admin_error_blockers(raw, stage="place_order")
    sku = next(b for b in blockers if b["key"] == "ADMIN_SKU")
    assert "PO=" not in sku["detail"]
    assert "白色" in sku["detail"]
    assert sku["raw_detail"] == raw

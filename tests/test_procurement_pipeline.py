"""procurement pipeline 与 prepare 阶段单元测试。"""

from __future__ import annotations

from unittest.mock import patch

from app.services.orders.procurement_pipeline import (
    _step_followup_monitor,
    ensure_category_before_accept,
    parse_admin_error_blockers,
    run_pipeline_for_line,
)
from app.services.orders.procurement_release import evaluate_prepare_stage
from app.services.orders.pipeline_store import enrich_row_pipeline_fields


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


def _row_stat_0_other() -> dict:
    return {
        "ord_line_no": "TI26070000181",
        "ord_no": "TO26070001776",
        "ord_line_stat": 0,
        "lvl1_ctgy_nm": "其它",
        "item_nm": "测试商品",
        "item_url": "https://detail.1688.com/offer/1.html",
        "splr_item_id": "12345",
    }


def test_prepare_stage_blocks_category_other():
    row = {**_row_stat_23(), "lvl1_ctgy_nm": "其他"}
    result = evaluate_prepare_stage(row)
    assert result["all_clear"] is False
    keys = [b["key"] for b in result["blockers"]]
    assert "CATEGORY_OTHER" in keys


def test_pipeline_stat_23_does_not_unbound_evaluate_prepare():
    """回归：函数内局部 import 曾遮蔽 evaluate_prepare_stage，导致 23 步 UnboundLocalError。"""
    row = _row_stat_23()
    row_54 = {**row, "ord_line_stat": 54}
    with (
        patch(
            "app.services.orders.procurement_pipeline.get_ord_line",
            side_effect=[row, row_54, row_54],
        ),
        patch(
            "app.services.orders.procurement_pipeline._try_auto_category_map",
            side_effect=lambda r: r,
        ),
        patch(
            "app.services.orders.procurement_pipeline.submit_1688_pre_purchase",
            return_value={"ok": True},
        ) as submit,
        patch("app.services.orders.order_line_sync.refresh_ord_lines"),
        patch(
            "app.services.orders.procurement_pipeline._save_state",
            side_effect=lambda *a, **k: {"ord_line_no": row["ord_line_no"], **k},
        ),
        patch(
            "app.services.orders.procurement_pipeline.is_1688_place_order_eligible",
            return_value=(False, "skip"),
        ),
    ):
        result = run_pipeline_for_line("TI26070000151", trigger="manual")
    assert result.get("error") is None
    assert "cannot access local variable" not in str(result)
    submit.assert_called_once()


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
        "note_topics": ["color_change"],
        "note_signal_type": "SKU_MISMATCH",
        "note_classify_reason": "要L码不要S码",
        "usr_rmk": "要L码不要S码",
    }
    with patch(
        "app.services.orders.order_note_classify.enrich_row_note_fields",
        side_effect=lambda r, **_k: r,
    ), patch(
        "app.services.orders.pipeline_store.list_acked_keys",
        return_value=set(),
    ):
        result = evaluate_prepare_stage(row)
    assert result["all_clear"] is False
    assert any(b["key"] == "NOTE_BLOCK" for b in result["blockers"])


def test_prepare_note_block_ack_insufficient_for_color():
    """仅 NOTE_BLOCK 签收不能放行颜色类备注，须 NOTE_HANDLED。"""
    row = {
        **_row_stat_23(),
        "usr_rmk": "实际采购粉色",
        "note_block_procurement": True,
        "note_tier": "high",
        "note_topics": ["color_change"],
        "note_signal_type": "SKU_MISMATCH",
        "note_classify_reason": "实际采购粉色",
        "lvl1_ctgy_nm": "路由器",
    }
    with patch(
        "app.services.orders.order_note_classify.enrich_row_note_fields",
        side_effect=lambda r, **_k: r,
    ), patch(
        "app.services.orders.pipeline_store.list_acked_keys",
        return_value={"NOTE_BLOCK"},
    ):
        blocked = evaluate_prepare_stage(row)
    assert blocked["all_clear"] is False
    assert any(b["key"] == "NOTE_BLOCK" for b in blocked["blockers"])

    with patch(
        "app.services.orders.order_note_classify.enrich_row_note_fields",
        side_effect=lambda r, **_k: r,
    ), patch(
        "app.services.orders.pipeline_store.list_acked_keys",
        return_value={"NOTE_HANDLED"},
    ):
        cleared = evaluate_prepare_stage(row)
    assert cleared["all_clear"] is True


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
    assert blockers[0]["label"] == "疑似缺货"


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


def test_parse_admin_error_fetch_goods_as_stock():
    raw = (
        "失败 1 个 1688 订单:[错误信息：PI sku not match fetch goods , "
        "skuId : 5774218782981 ;items : TI26060000151;TOs=TO26060000129TIs=TI26060000151]，成功 : []"
    )
    blockers = parse_admin_error_blockers(raw, stage="place_order")
    assert len(blockers) == 1
    assert blockers[0]["key"] == "ADMIN_STOCK"
    assert blockers[0]["label"] == "疑似缺货"
    assert blockers[0]["classify_source"] == "rule"


def test_ensure_category_ok_when_lvl1_mapped():
    row = {**_row_stat_0_other(), "lvl1_ctgy_nm": "路由器"}
    updated, blockers = ensure_category_before_accept(row)
    assert blockers == []
    assert updated["lvl1_ctgy_nm"] == "路由器"


def test_ensure_category_blocks_when_map_fails():
    row = _row_stat_0_other()
    with (
        patch(
            "app.services.orders.procurement_pipeline._ensure_product_for_line",
            return_value={"tangbuy_product_id": "P1", "category_status": "pending"},
        ),
        patch(
            "app.services.products.service.map_product_category_by_id",
            return_value={"tangbuy_product_id": "P1", "category_status": "pending"},
        ),
        patch(
            "app.services.orders.procurement_pipeline._overlay_product_category",
            side_effect=lambda r, _p: r,
        ),
        patch("app.services.orders.order_line_sync.refresh_ord_lines"),
        patch("app.services.orders.procurement_pipeline.get_ord_line", return_value=row),
    ):
        _, blockers = ensure_category_before_accept(row)
    assert any(b["key"] == "CATEGORY_OTHER" for b in blockers)


def test_run_pipeline_blocks_accept_when_category_other():
    row = _row_stat_0_other()
    with (
        patch("app.services.orders.procurement_pipeline.get_ord_line", return_value=row),
        patch(
            "app.services.orders.procurement_pipeline.ensure_category_before_accept",
            return_value=(row, [{"key": "CATEGORY_OTHER", "label": "品类未映射", "stage": "accept"}]),
        ),
        patch("app.services.orders.procurement_pipeline.accept_order_for_line") as accept_mock,
        patch("app.services.orders.procurement_pipeline._save_state", side_effect=lambda *a, **k: k),
    ):
        result = run_pipeline_for_line("TI26070000181", trigger="manual")
    assert result["ok"] is False
    assert result["step"] == "accept"
    assert any(b["key"] == "CATEGORY_OTHER" for b in result["blockers"])
    accept_mock.assert_not_called()


def test_enrich_stat0_exposes_category_blocker():
    row = enrich_row_pipeline_fields(_row_stat_0_other(), states={})
    assert any(b.get("key") == "CATEGORY_OTHER" for b in row.get("pipeline_blockers") or [])

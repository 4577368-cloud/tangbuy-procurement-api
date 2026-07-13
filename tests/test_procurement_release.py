"""procurement_release 单元测试。"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.services.orders.procurement_release import (
    evaluate_procurement_pass,
    is_1688_pre_purchase_eligible,
    submit_1688_pre_purchase,
)


def _base_row() -> dict:
    return {
        "ord_line_no": "TI26070000151",
        "ord_no": "TO26070000151",
        "ord_line_stat": 23,
        "ord_line_stat_nm": "处理中",
        "_store_source": "alibaba",
        "shop_pltf_cd": "1688",
        "lvl1_ctgy_nm": "路由器",
        "ord_cnt": 1,
        "pur_amt": 100.0,
        "post_fee": 10.0,
        "ds_ord_amt": 200.0,
        "pur_prc": 100.0,
        "item_url": "https://detail.1688.com/offer/804336334825.html",
        "usr_id": 12345,
        "front_sku_attr_desc": "默认",
        "item_attr": "默认",
        "pay_time": "2026-07-10T08:00:00+08:00",
    }


def test_eligible_1688_row():
    ok, code = is_1688_pre_purchase_eligible(_base_row())
    assert ok is True
    assert code == "ok"


def test_ineligible_when_category_other():
    row = {**_base_row(), "lvl1_ctgy_nm": "其他"}
    ok, code = is_1688_pre_purchase_eligible(row)
    assert ok is False
    assert code == "category_blocked"


def test_ineligible_when_not_status_23():
    row = {**_base_row(), "ord_line_stat": 0}
    ok, code = is_1688_pre_purchase_eligible(row)
    assert ok is False
    assert code.startswith("invalid_stage")


def test_evaluate_all_passed():
    result = evaluate_procurement_pass(_base_row())
    assert result["all_passed"] is True
    assert result["passed_count"] == result["total_count"]


def test_evaluate_blocks_negative_margin():
    row = {**_base_row(), "ds_ord_amt": 50.0}
    result = evaluate_procurement_pass(
        row,
        config={
            "gross_margin_threshold": 15,
            "moq": {"enabled": True, "default_min": 1},
            "rules": {"block_negative_margin": True},
        },
    )
    margin = next(c for c in result["conditions"] if c["key"] == "margin")
    assert margin["passed"] is False
    assert result["all_passed"] is False


@patch("app.services.orders.procurement_release.release_store.has_successful_release", return_value=False)
@patch("app.services.orders.procurement_release.get_ord_line")
@patch("app.services.orders.procurement_release.alibaba_pre_purchase")
@patch("app.services.orders.procurement_release._refresh_ord_line")
def test_submit_calls_admin(mock_refresh, mock_pre, mock_get_line, _mock_has):
    row = _base_row()
    mock_get_line.return_value = row
    mock_refresh.return_value = {**row, "ord_line_stat": 54, "ord_line_stat_nm": "1688待生成"}
    mock_pre.return_value = {"ok": True}

    result = submit_1688_pre_purchase("TI26070000151", trigger="manual", force=True)

    assert result["ok"] is True
    mock_pre.assert_called_once_with(["TI26070000151"])
    assert result["ord_line_stat_after"] == 54

"""procurement_place_order 单元测试。"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.services.orders.procurement_place_order import (
    build_order_targets,
    group_targets_by_store,
    is_1688_place_order_eligible,
    submit_1688_place_order,
    _extract_wait_rows,
)


def _base_row() -> dict:
    return {
        "ord_line_no": "TI26070000054",
        "ord_no": "TO26070000044",
        "ord_line_stat": 54,
        "ord_line_stat_nm": "1688待生成",
        "_store_source": "alibaba",
        "shop_pltf_cd": "1688",
    }


def _wait_rows() -> list[dict]:
    return [
        {
            "orderNo": "TO26070000044",
            "storeId": "BBBvtpGbJxoEl8gcvBcuRgJlg",
            "items": [{"itemNo": "TI26070000054"}],
        },
        {
            "orderNo": "TO26070000072",
            "storeId": "BBBoMhGyBCdnmX2S_2ZN5eEUw",
            "items": [{"itemNo": "TI26070000095"}],
        },
        {
            "orderNo": "TO26060000129",
            "storeId": "BBBvtpGbJxoEl8gcvBcuRgJlg",
            "items": [
                {"itemNo": "TI26060000153"},
                {"itemNo": "TI26060000152"},
            ],
        },
    ]


def test_extract_wait_rows_direct_rows():
    assert _extract_wait_rows({"rows": [{"orderNo": "TO1"}], "total": 1}) == [{"orderNo": "TO1"}]


def test_extract_wait_rows_nested_data():
    assert _extract_wait_rows({"data": {"rows": [{"orderNo": "TO2"}], "total": 1}}) == [{"orderNo": "TO2"}]


def test_eligible_stat_54():
    ok, code = is_1688_place_order_eligible(_base_row())
    assert ok is True
    assert code == "ok"


def test_ineligible_when_not_status_54():
    row = {**_base_row(), "ord_line_stat": 23}
    ok, code = is_1688_place_order_eligible(row)
    assert ok is False
    assert code.startswith("invalid_stage")


def test_build_order_targets_matches_item_nos():
    targets = build_order_targets(_wait_rows(), {"TI26070000054", "TI26070000095"})
    assert len(targets) == 2
    item_nos = {item for t in targets for item in t["itemNos"]}
    assert item_nos == {"TI26070000054", "TI26070000095"}


def test_group_targets_by_store_merges_same_store():
    targets = build_order_targets(_wait_rows(), {"TI26070000054", "TI26060000153"})
    groups = group_targets_by_store(targets, merge_same_store=True)
    assert len(groups) == 1
    assert len(groups[0]) == 2


def test_group_targets_by_store_split_when_disabled():
    targets = build_order_targets(_wait_rows(), {"TI26070000054", "TI26060000153"})
    groups = group_targets_by_store(targets, merge_same_store=False)
    assert len(groups) == 2


@patch("app.services.orders.procurement_place_order.place_order_store.has_successful_place_order", return_value=False)
@patch("app.services.orders.procurement_place_order.get_ord_line")
@patch("app.services.orders.procurement_place_order.list_wait_generate_orders")
@patch("app.services.orders.procurement_place_order.create_platform_order")
@patch("app.services.orders.procurement_place_order._refresh_ord_lines")
def test_submit_calls_create(
    mock_refresh,
    mock_create,
    mock_wait,
    mock_get_line,
    _mock_has,
):
    row = _base_row()
    mock_get_line.return_value = row
    mock_wait.return_value = {"rows": _wait_rows(), "total": 3}
    mock_create.return_value = {"code": 200}
    mock_refresh.return_value = {row["ord_line_no"]: {**row, "ord_line_stat": 55, "ord_line_stat_nm": "1688待支付"}}

    result = submit_1688_place_order(["TI26070000054"], trigger="manual")

    assert result["ok"] is True
    mock_create.assert_called_once()
    assert result["ord_line_stat_after"] == 55


@patch("app.services.orders.procurement_place_order.place_order_store.has_successful_place_order", return_value=False)
@patch("app.services.orders.procurement_place_order.get_ord_line")
@patch("app.services.orders.procurement_place_order.list_wait_generate_orders")
def test_submit_rejects_not_in_wait_list(mock_wait, mock_get_line, _mock_has):
    row = _base_row()
    mock_get_line.return_value = row
    mock_wait.return_value = {"rows": [], "total": 0}

    with pytest.raises(Exception) as exc:
        submit_1688_place_order(["TI26070000054"], trigger="manual")
    assert "待生成列表" in str(exc.value)

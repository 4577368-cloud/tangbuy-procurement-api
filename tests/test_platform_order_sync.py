"""platform_order_sync 单元测试。"""

from unittest.mock import patch

from app.services.orders.platform_order_sync import _query_for_line

_ROW_TI185 = {
    "platformOrderId": "3312141027418003195",
    "tradeOrderNo": "TO26070001829",
    "createTime": "2026-07-14 11:18:22",
    "orders": [
        {
            "orderNo": "TO26070001815",
            "items": [
                {
                    "itemNo": "TI26070000185",
                    "orderNo": "TO26070001815",
                    "amount": 615.6,
                    "quantity": 20,
                }
            ],
        }
    ],
    "platformTotalAmount": 476.0,
    "platformGoodsAmount": 476.0,
    "platformPostFee": 16.0,
    "orderGoodsAmount": 615.6,
    "orderPostFee": 0.0,
    "orderTotalAmount": 615.6,
    "benefitGoodsAmount": 155.6,
    "benefitPostFee": -16.0,
    "benefitTotalAmount": 139.6,
    "platformOrderDetailUrl": "https://trade.1688.com/order/new_step_order_detail.htm?orderId=3312141027418003195",
}


def _page(rows: list[dict], *, total: int) -> dict:
    return {"total": total, "rows": rows}


@patch("app.services.orders.platform_order_sync.query_platform_orders")
def test_query_for_line_scans_pages_when_filter_ignored(mock_query) -> None:
    """itemNo 过滤不生效时，应分页扫描待支付列表。"""
    filler = {
        "platformOrderId": "999",
        "orders": [{"orderNo": "TO000", "items": [{"itemNo": "TI00000000001"}]}],
    }

    def side_effect(*, page_num: int, page_size: int, status: int = 0, **kwargs):
        if page_num == 1:
            return _page([filler] * 50, total=120)
        if page_num == 2:
            return _page([filler] * 50, total=120)
        if page_num == 3:
            return _page([filler] * 49 + [_ROW_TI185], total=120)
        return _page([], total=120)

    mock_query.side_effect = side_effect
    hit = _query_for_line(ord_line_no="TI26070000185", ord_no="TO26070001815")
    assert hit is not None
    assert hit["plt_ord_id"] == "3312141027418003195"
    assert hit["pur_no"] == "3312141027418003195"
    assert mock_query.call_count == 3


@patch("app.services.orders.platform_order_sync.query_platform_orders")
def test_query_for_line_found_on_first_page(mock_query) -> None:
    mock_query.return_value = _page([_ROW_TI185], total=1)
    hit = _query_for_line(ord_line_no="TI26070000185")
    assert hit is not None
    assert hit["ord_line_no"] == "TI26070000185"
    mock_query.assert_called_once()

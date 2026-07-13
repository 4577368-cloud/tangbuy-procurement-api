"""订单回传字段映射测试。"""

from app.integrations.tangbuy_admin.platform_order_mapper import (
    index_platform_rows_by_line,
    map_platform_row_to_line_fields,
)

SAMPLE_ROW = {
    "tradeOrderNo": "TO24080001253",
    "orderGoodsAmount": 11.60,
    "orderPostFee": 3.80,
    "orderTotalAmount": 15.40,
    "platformTotalAmount": 9.50,
    "platformGoodsAmount": 5.70,
    "platformPostFee": 3.90,
    "platformOrderId": "2257342428758342274",
    "benefitGoodsAmount": 6.00,
    "benefitPostFee": -0.10,
    "benefitTotalAmount": 5.90,
    "lastSyncTime": "2025-12-16 13:55:16",
    "platformOrderDetailUrl": "https://trade.1688.com/order/new_step_order_detail.htm?orderId=2257342428758342274",
    "orders": [
        {
            "orderNo": "TO24080001252",
            "items": [
                {
                    "itemNo": "TI24080001250",
                    "amount": 11.60,
                    "quantity": 2,
                    "comparePrice": 6.00,
                    "goodsUrl": "https://detail.1688.com/offer/671234965595.html",
                }
            ],
        }
    ],
    "platformOrderDetail": {
        "id": "2257342428758342274",
        "status": "success",
        "postFee": 3.9,
        "totalAmount": 9.5,
        "payTime": "2024-08-14 18:13:01",
        "sellerLoginId": "szbenchang",
        "items": [
            {
                "price": 2.8,
                "amount": 5.6,
                "quantity": 2,
                "goodsId": "671234965595",
                "skuDesc": "C5黑色-出风口款",
                "logisticsStatus": "已收货",
            }
        ],
        "logisticInfo": {
            "items": [
                {
                    "logisticsCompanyName": "中通快递(ZTO)",
                    "mailNo": "78825735743908",
                }
            ]
        },
    },
}


def test_map_platform_row_to_line_fields():
    fields = map_platform_row_to_line_fields(
        SAMPLE_ROW,
        ord_line_no="TI24080001250",
        order_no="TO24080001252",
    )
    assert fields["plt_ord_id"] == "2257342428758342274"
    assert fields["pur_no"] == "2257342428758342274"
    assert fields["plt_goods_amt"] == 5.70
    assert fields["plt_post_fee"] == 3.90
    assert fields["plt_total_amt"] == 9.50
    assert fields["plt_benefit_total_amt"] == 5.90
    assert fields["plt_line_unit_prc"] == 2.8
    assert fields["plt_logistics_no"] == "78825735743908"


def test_index_platform_rows_by_line():
    indexed = index_platform_rows_by_line([SAMPLE_ROW])
    assert "TI24080001250" in indexed
    assert indexed["TI24080001250"]["plt_goods_amt"] == 5.70

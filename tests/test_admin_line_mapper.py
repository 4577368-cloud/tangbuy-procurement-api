"""Admin listOrderDetail → 宽表映射：发货字段。"""

from app.integrations.tangbuy_admin.mapper import map_admin_line


def test_map_express_and_signed_fields():
    order = {
        "orderNo": "TO25110000252",
        "orderStatus": 2,
        "payTime": "2025-11-21T10:37:32Z",
        "storeSource": "alibaba",
    }
    item = {
        "itemNo": "TI25110000307",
        "goodsStatus": 8,
        "goodsName": "宝宝靴",
        "nums": 1,
        "unitPrice": 27,
        "express": "韵达快递",
        "expressId": "yunda",
        "expressNo": "YD465789",
        "purchaseNo": "1233447909876555544",
        "purchaseTime": "2026-06-30T02:45:41Z",
        "signedTime": "2026-07-14T02:04:20Z",
    }
    row = map_admin_line(order, item)
    assert row["ord_line_stat"] == 8
    assert row["ord_line_stat_nm"] == "已签收"
    assert row["exprs_no"] == "YD465789"
    assert row["exprs_nm"] == "韵达快递"
    assert row["is_exprs_dlyd"] == 1
    assert row["pur_no"] == "1233447909876555544"
    assert row["pur_time"] == "2026-06-30T02:45:41Z"
    assert row["sign_time"] == "2026-07-14T02:04:20Z"


def test_map_without_express_omits_logistics_keys():
    row = map_admin_line(
        {"orderNo": "TO1", "orderStatus": 2},
        {"itemNo": "TI1", "goodsStatus": 22, "nums": 1, "unitPrice": 1},
    )
    assert "exprs_no" not in row
    assert "exprs_nm" not in row
    assert "pur_no" not in row
    assert row["ord_line_stat"] == 22

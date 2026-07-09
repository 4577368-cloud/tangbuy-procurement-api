"""Admin 状态码 → 宽表状态名（与 web status-enums 对齐）。"""

from __future__ import annotations

ORD_STAT: dict[int, str] = {
    0: "待付款",
    1: "待接单",
    2: "处理中",
    3: "转单中",
    4: "取消订购",
    5: "邮费补款",
    6: "风控中",
    7: "撤单退款",
    8: "支付中",
    9: "已完成",
}

ORD_LINE_STAT: dict[int, str] = {
    -2: "支付中",
    -1: "待支付",
    0: "待接单",
    2: "待补款",
    5: "已发货",
    6: "分开发货",
    8: "已签收",
    9: "已到货",
    10: "已入库",
    11: "作废",
    14: "待确认",
    22: "已订购",
    23: "处理中",
    24: "取消订购",
    25: "异常订单",
    28: "出库中",
    29: "出库打包完毕",
    30: "寄送海外",
    31: "已收到货",
    33: "风控审核",
    37: "等待出库",
    54: "1688待生成",
    55: "1688待支付",
    58: "仓库处理中",
}


def ord_stat_label(code: int | None) -> str | None:
    if code is None:
        return None
    return ORD_STAT.get(code)


def ord_line_stat_label(code: int | None) -> str | None:
    if code is None:
        return None
    return ORD_LINE_STAT.get(code)

"""看板信号聚合 — 与 Web order-signal-board 口径对齐。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services.command_center.board_signals import (
    aggregate_board_signal_stats,
    row_to_board_signal,
)


def _base_row(**overrides) -> dict:
    row = {
        "ord_line_no": "OL-1",
        "pay_time": "2026-07-01T10:00:00Z",
        "ord_line_stat": 14,
        "pur_amt": 100.0,
        "post_fee": 10.0,
        "ds_ord_amt": 110.0,
        "pur_prc": 100.0,
        "ord_cnt": 1,
        "is_ord_del": 0,
        "is_line_del": 0,
    }
    row.update(overrides)
    return row


def test_pay_amount_gap_deferred_for_prep_stat_zero():
    row = _base_row(
        ord_line_stat=0,
        pur_amt=200.0,
        ds_ord_amt=110.0,
    )
    signal = row_to_board_signal(row, [])
    assert signal is None


def test_sku_mismatch_requires_enriched_flag():
    row = _base_row(ord_line_stat=0, sku_mismatch=True)
    signal = row_to_board_signal(row, [])
    assert signal is not None
    assert signal["signal_type"] == "SKU_MISMATCH"
    assert signal["urgency"] == "today"


def test_ship_overdue_before_finance():
    old = datetime.now(timezone.utc) - timedelta(hours=72)
    row = _base_row(
        ord_line_stat=22,
        pur_time=old.isoformat().replace("+00:00", "Z"),
        pur_amt=200.0,
        ds_ord_amt=50.0,
    )
    signal = row_to_board_signal(row, [])
    assert signal is not None
    assert signal["signal_type"] == "SHIP_OVERDUE"


def test_stockout_from_product_inventory():
    row = _base_row(ord_line_stat=0)
    products = [
        {
            "tangbuy_product_id": "P1",
            "linked_ord_lines": ["OL-1"],
            "stock_status": "out",
            "inventory_total": 0,
        }
    ]
    signal = row_to_board_signal(row, products)
    assert signal is not None
    assert signal["signal_type"] == "STOCKOUT"


def test_pay_amount_gap_uses_platform_actual():
    row = _base_row(
        ord_line_stat=55,
        ds_ord_amt=50.0,
        pur_amt=30.0,
        post_fee=5.0,
        plt_total_amt=60.0,
        plt_goods_amt=55.0,
        plt_post_fee=5.0,
        plt_line_detail_amt=55.0,
    )
    signal = row_to_board_signal(row, [])
    assert signal is not None
    assert signal["signal_type"] == "PAY_AMOUNT_GAP"


def test_aggregate_board_action_band():
    old = datetime.now(timezone.utc) - timedelta(hours=72)
    rows = [
        _base_row(
            ord_line_no="OL-SHIP",
            ord_line_stat=22,
            pur_time=old.isoformat().replace("+00:00", "Z"),
        ),
        _base_row(
            ord_line_no="OL-SKU",
            ord_line_stat=0,
            sku_mismatch=True,
        ),
    ]
    stats = aggregate_board_signal_stats(rows, [])
    assert stats["board_band_counts"]["action"] == 2
    assert stats["board_signal_counts_action"]["SHIP_OVERDUE"] == 1
    assert stats["board_signal_counts_action"]["SKU_MISMATCH"] == 1

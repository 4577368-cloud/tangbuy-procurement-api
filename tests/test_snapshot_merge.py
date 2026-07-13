from app.services.orders.snapshot_merge import (
    apply_category_overlay,
    build_category_overlay,
    merge_admin_snapshot,
    status_fingerprint,
)


def test_merge_preserves_category_overlay():
    prev = {
        "ord_line_no": "TI001",
        "ord_line_stat": 23,
        "lvl1_ctgy_nm": "上衣",
        "cstm_hs_cd": "6204330000",
        "_category_overlay": build_category_overlay(
            {
                "category_id": 1,
                "category_cn_name": "上衣",
                "hs_code": "6204330000",
                "declare_cn_name": "上衣",
            },
            source="category_writeback",
            at="2026-07-12T00:00:00Z",
        ),
    }
    incoming = {
        "ord_line_no": "TI001",
        "ord_line_stat": 54,
        "lvl1_ctgy_nm": "其它",
        "cstm_hs_cd": "",
    }
    merged = merge_admin_snapshot(prev, incoming)
    assert merged["ord_line_stat"] == 54
    assert merged["lvl1_ctgy_nm"] == "上衣"
    assert merged["cstm_hs_cd"] == "6204330000"


def test_fingerprint_includes_category_fields():
    a = {"ord_line_no": "TI001", "ord_line_stat": 23, "cstm_hs_cd": "1"}
    b = {**a, "cstm_hs_cd": "2"}
    assert status_fingerprint(a) != status_fingerprint(b)


def test_apply_category_overlay():
    row = {"ord_line_no": "TI001", "lvl1_ctgy_nm": "旧"}
    hs = {
        "category_id": 9,
        "category_cn_name": "女鞋",
        "hs_code": "6402200000",
        "declare_cn_name": "女鞋",
    }
    out = apply_category_overlay(row, build_category_overlay(hs, source="test", at="t"))
    assert out["lvl1_ctgy_nm"] == "女鞋"
    assert out["_category_overlay"]["locked"] is True

"""备注分级单元测试。"""

from app.services.orders.order_note_classify import classify_order_note


def test_low_value_ship_fast_zh():
    r = classify_order_note("请尽快发货")
    assert r.tier == "low"
    assert r.block_procurement is False
    assert r.signal_type is None


def test_low_value_ship_fast_en():
    r = classify_order_note("Please ship as soon as possible, thanks!")
    assert r.tier == "low"
    assert r.block_procurement is False


def test_high_value_size_change_zh():
    r = classify_order_note("把M码换成XL")
    assert r.tier == "high"
    assert r.block_procurement is True
    assert r.signal_type == "SKU_MISMATCH"
    assert "size_change" in r.topics


def test_high_value_color_change_en():
    r = classify_order_note("Please change color to black instead of white")
    assert r.tier == "high"
    assert r.signal_type == "SKU_MISMATCH"


def test_high_value_size_change_fr():
    r = classify_order_note("Changer la taille M en XL svp")
    assert r.tier == "high"
    assert r.block_procurement is True


def test_high_value_size_change_es():
    r = classify_order_note("Cambiar talla M por XL")
    assert r.tier == "high"
    assert "size_change" in r.topics


def test_empty_note():
    r = classify_order_note("")
    assert r.tier == "none"

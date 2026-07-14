"""Admin 下单回执分类测试。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.services.orders.admin_error_classify import (
    classify_admin_error,
    classify_admin_error_rules,
)


def test_fetch_goods_rule_is_stock_not_sku():
    msg = "PI sku not match fetch goods , skuId : 5774218782981"
    c = classify_admin_error_rules(msg)
    assert c is not None
    assert c.key == "ADMIN_STOCK"
    assert c.label == "疑似缺货"


def test_attr_mismatch_rule_is_sku():
    msg = "sku属性信息不匹配，需要颜色啊:白色; 1688查询的是[颜色:白色, 尺码:S]"
    c = classify_admin_error_rules(msg)
    assert c is not None
    assert c.key == "ADMIN_SKU"


def test_llm_used_when_rules_miss():
    msg = "下游渠道异常码 X9，请联系平台"
    fake = MagicMock()
    fake.content = '{"key":"ADMIN_ERROR","reason":"未知平台错误","confidence":0.8}'
    with (
        patch("app.services.orders.admin_error_classify.classify_admin_error_rules", return_value=None),
        patch("app.core.config.get_settings") as gs,
        patch("app.services.agent.llm.chat_completion", return_value=fake),
        patch("app.services.orders.admin_error_classify._load_cache", return_value={}),
        patch("app.services.orders.admin_error_classify._save_cache"),
    ):
        gs.return_value.llm_configured = True
        c = classify_admin_error(msg, allow_llm=True)
    assert c.key == "ADMIN_ERROR"
    assert c.source == "llm"

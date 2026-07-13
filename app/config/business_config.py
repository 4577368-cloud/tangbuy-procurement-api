"""业务配置模型（对齐 src/lib/config/business-config.ts）。"""

from __future__ import annotations

from typing import Any

DEFAULT_BUSINESS_CONFIG: dict[str, Any] = {
    "gross_margin_threshold": 15,
    "moq": {"enabled": True, "default_min": 1},
    "unshipped_timeout_hours": 48,
    "ai_confidence_threshold": 0.85,
    # 大店售价 / 运费统一加价倍率（相对 1688 原价）
    "price_markup": 1.2,
    "rules": {
        "auto_category_mapping": True,
        "admin_category_writeback": True,
        "auto_add_to_store": False,
        "auto_order_followup": False,
        "block_negative_margin": True,
    },
    # 增量同步后：准入全过的 1688 子单自动调用 alibabaPrePurchase
    "auto_1688_pre_purchase_enabled": True,
    # 增量同步后：stat=54 子单按 storeId 自动调用 platform/order/create
    "auto_1688_place_order_enabled": True,
    # 增量同步后：接单池自动 confirmList（0→23）
    "auto_accept_orders_enabled": True,
    # True=每次进入指挥中心重新生成简报；False=10 分钟内用缓存
    "briefing_always_refresh": True,
    # 演示/接库前：写操作失败时按成功返回，避免 UI 卡在提交中
    "demo_submit_always_success": True,
}

RULE_KEYS = (
    "auto_category_mapping",
    "admin_category_writeback",
    "auto_add_to_store",
    "auto_order_followup",
    "block_negative_margin",
)


def normalize_business_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    base = {
        **DEFAULT_BUSINESS_CONFIG,
        "moq": dict(DEFAULT_BUSINESS_CONFIG["moq"]),
        "rules": dict(DEFAULT_BUSINESS_CONFIG["rules"]),
    }
    if not raw:
        return base
    moq = {**base["moq"], **(raw.get("moq") or {})}
    rules = {**base["rules"], **(raw.get("rules") or {})}
    rules = {k: bool(rules.get(k)) for k in RULE_KEYS}
    pct = raw.get("gross_margin_threshold", base["gross_margin_threshold"])
    conf = raw.get("ai_confidence_threshold", base["ai_confidence_threshold"])
    hours = raw.get("unshipped_timeout_hours", base["unshipped_timeout_hours"])
    markup_raw = raw.get("price_markup", base["price_markup"])
    try:
        markup = float(markup_raw) if markup_raw is not None else 1.2
    except (TypeError, ValueError):
        markup = 1.2
    return {
        "gross_margin_threshold": max(0, min(100, float(pct) if pct is not None else 15)),
        "moq": {
            "enabled": bool(moq.get("enabled")),
            "default_min": max(0, int(moq.get("default_min") or 0)),
        },
        "unshipped_timeout_hours": max(0, int(hours or 0)),
        "ai_confidence_threshold": max(0, min(1, float(conf) if conf is not None else 0.85)),
        "price_markup": max(1.0, min(5.0, round(markup, 4))),
        "rules": rules,
        "briefing_always_refresh": bool(
            raw.get("briefing_always_refresh", base["briefing_always_refresh"])
        ),
        "auto_1688_pre_purchase_enabled": bool(
            raw.get(
                "auto_1688_pre_purchase_enabled",
                base["auto_1688_pre_purchase_enabled"],
            )
        ),
        "auto_1688_place_order_enabled": bool(
            raw.get(
                "auto_1688_place_order_enabled",
                base["auto_1688_place_order_enabled"],
            )
        ),
        "auto_accept_orders_enabled": bool(
            raw.get(
                "auto_accept_orders_enabled",
                base["auto_accept_orders_enabled"],
            )
        ),
        "demo_submit_always_success": bool(
            raw.get(
                "demo_submit_always_success",
                base["demo_submit_always_success"],
            )
        ),
    }


def get_price_markup() -> float:
    from app.config.store import get_business_config

    cfg = get_business_config()
    try:
        n = float(cfg.get("price_markup") or 1.2)
    except (TypeError, ValueError):
        n = 1.2
    return max(1.0, min(5.0, n))

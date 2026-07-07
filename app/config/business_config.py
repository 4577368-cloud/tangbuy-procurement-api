"""业务配置模型（对齐 src/lib/config/business-config.ts）。"""

from __future__ import annotations

from typing import Any

DEFAULT_BUSINESS_CONFIG: dict[str, Any] = {
    "gross_margin_threshold": 15,
    "moq": {"enabled": True, "default_min": 1},
    "unshipped_timeout_hours": 48,
    "ai_confidence_threshold": 0.85,
    "rules": {
        "auto_category_mapping": True,
        "auto_add_to_store": False,
        "auto_order_followup": False,
        "block_negative_margin": True,
    },
}

RULE_KEYS = (
    "auto_category_mapping",
    "auto_add_to_store",
    "auto_order_followup",
    "block_negative_margin",
)


def normalize_business_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    base = DEFAULT_BUSINESS_CONFIG.copy()
    if not raw:
        return base
    moq = {**base["moq"], **(raw.get("moq") or {})}
    rules = {**base["rules"], **(raw.get("rules") or {})}
    rules = {k: bool(rules.get(k)) for k in RULE_KEYS}
    pct = raw.get("gross_margin_threshold", base["gross_margin_threshold"])
    conf = raw.get("ai_confidence_threshold", base["ai_confidence_threshold"])
    hours = raw.get("unshipped_timeout_hours", base["unshipped_timeout_hours"])
    return {
        "gross_margin_threshold": max(0, min(100, float(pct) if pct is not None else 15)),
        "moq": {
            "enabled": bool(moq.get("enabled")),
            "default_min": max(0, int(moq.get("default_min") or 0)),
        },
        "unshipped_timeout_hours": max(0, int(hours or 0)),
        "ai_confidence_threshold": max(0, min(1, float(conf) if conf is not None else 0.85)),
        "rules": rules,
    }

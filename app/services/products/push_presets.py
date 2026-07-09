"""商家客户推送店铺预设。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from app.core.paths import data_dir

_BD_POOL = ["张敏", "李浩", "王芳", "陈杰", "刘洋", "赵倩"]

# 按店铺固定 BD（稳定随机：同一店铺始终同一人）
DEFAULT_SHOP_PRESETS: list[dict[str, str]] = [
    {"shop_url": "6yxzb0-yp.myshopify.com", "user_id": "280492879740944", "user_email": "", "bd_name": "张敏"},
    {"shop_url": "svakvw-9d.myshopify.com", "user_id": "162805459402768", "user_email": "", "bd_name": "李浩"},
    {"shop_url": "42b04e-e7.myshopify.com", "user_id": "211182745288720", "user_email": "", "bd_name": "王芳"},
    {"shop_url": "cugrij-g8.myshopify.com", "user_id": "49770657873936", "user_email": "", "bd_name": "陈杰"},
    {"shop_url": "moutaz-ahmed6.myshopify.com", "user_id": "13285546819600", "user_email": "", "bd_name": "刘洋"},
    {"shop_url": "0e8651-14.myshopify.com", "user_id": "9994673700880", "user_email": "", "bd_name": "赵倩"},
    {"shop_url": "2yxbus-af.myshopify.com", "user_id": "152671879561248", "user_email": "", "bd_name": "张敏"},
    {"shop_url": "jaydeebedding.myshopify.com", "user_id": "214138074087440", "user_email": "", "bd_name": "李浩"},
    {"shop_url": "vxdbqw-nd.myshopify.com", "user_id": "16682915053600", "user_email": "", "bd_name": "王芳"},
    {"shop_url": "658e4b-3.myshopify.com", "user_id": "229695158059040", "user_email": "", "bd_name": "陈杰"},
    {"shop_url": "rywwtc-i2.myshopify.com", "user_id": "215697838194720", "user_email": "", "bd_name": "刘洋"},
]

_PRESET_PATH = data_dir() / "products" / "push-shop-presets.json"
_AUDIT_PATH = data_dir() / "products" / "push-audit.jsonl"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _assign_bd(shop_url: str, explicit: str = "") -> str:
    name = (explicit or "").strip()
    if name:
        return name
    if not shop_url:
        return _BD_POOL[0]
    idx = sum(ord(c) for c in shop_url) % len(_BD_POOL)
    return _BD_POOL[idx]


def _normalize_row(row: dict[str, Any]) -> Optional[dict[str, str]]:
    shop_url = str(row.get("shop_url") or "").strip()
    if not shop_url:
        return None
    return {
        "shop_url": shop_url,
        "user_id": str(row.get("user_id") or "").strip(),
        "user_email": str(row.get("user_email") or "").strip(),
        "bd_name": _assign_bd(shop_url, str(row.get("bd_name") or "")),
    }


def list_shop_presets() -> list[dict[str, str]]:
    if _PRESET_PATH.exists():
        try:
            data = json.loads(_PRESET_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list) and data:
                rows = [_normalize_row(r) for r in data if isinstance(r, dict)]
                return [r for r in rows if r]
        except (OSError, json.JSONDecodeError):
            pass
    return [dict(r) for r in DEFAULT_SHOP_PRESETS]


def find_preset(shop_url: str) -> Optional[dict[str, str]]:
    needle = (shop_url or "").strip().lower()
    for row in list_shop_presets():
        if row["shop_url"].lower() == needle:
            return row
    return None


def record_product_push(
    *,
    product_id: str,
    shop_url: str,
    user_id: str,
    user_email: str = "",
    bd_name: str = "",
    operator: str = "",
) -> dict[str, Any]:
    entry = {
        "at": _now_iso(),
        "product_id": product_id,
        "shop_url": shop_url,
        "user_id": user_id,
        "user_email": user_email,
        "bd_name": bd_name or _assign_bd(shop_url),
        "operator": operator,
        "status": "recorded",
    }
    _AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _AUDIT_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry

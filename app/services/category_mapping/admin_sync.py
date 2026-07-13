"""Admin 品类现状读取与可信判定。"""

from __future__ import annotations

import logging
from typing import Any, Optional

from app.integrations.tangbuy_admin.category_api import list_goods_categories
from app.services.products.store import GENERIC_PLATFORM_HINTS, RESOLVED_MAPPING_STATUSES, confirm_product_mapping, finalize_product_mapping_confirm, is_valid_hs_mapping

_log = logging.getLogger(__name__)

PLACEHOLDER_CATEGORIES = GENERIC_PLATFORM_HINTS | frozenset({"其它", "其他", "待映射", "未分类", "默认"})


def is_placeholder_category(name: Any) -> bool:
    text = str(name or "").strip()
    if not text:
        return True
    if text in PLACEHOLDER_CATEGORIES:
        return True
    lowered = text.lower()
    return lowered in ("other", "others", "misc", "default")


def _int_flag(v: Any) -> int:
    try:
        return 1 if int(v or 0) != 0 else 0
    except (TypeError, ValueError):
        return 0


def hs_from_admin_entry(entry: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Admin listByGoodsIds 单条 → 商品中心 hs_mapping 结构。"""
    dto = entry.get("hsCodeDTO")
    if not isinstance(dto, dict):
        dto = {}
    try:
        cid = int(entry.get("categoryId") or dto.get("cid") or 0)
    except (TypeError, ValueError):
        cid = 0
    if cid <= 0:
        return None

    cn_name = str(dto.get("cnName") or entry.get("categoryName") or "").strip()
    if is_placeholder_category(cn_name):
        return None

    hs_code = str(dto.get("hsCode") or "").strip()
    declare_cn = str(dto.get("decCnName") or cn_name).strip()
    declare_en = str(dto.get("decEnName") or dto.get("enName") or "").strip()

    hs = {
        "category_id": cid,
        "category_cn_name": cn_name,
        "category_en_name": str(dto.get("enName") or "").strip(),
        "hs_code": hs_code,
        "declare_cn_name": declare_cn,
        "declare_en_name": declare_en,
        "tariff": dto.get("zipRate"),
    }
    return hs if is_valid_hs_mapping(hs) else None


def is_admin_category_trusted(
    entry: dict[str, Any],
    *,
    ord_row: Optional[dict[str, Any]] = None,
) -> bool:
    """categoryId 有效、类目非占位、且无需确认。"""
    if ord_row and _int_flag(ord_row.get("is_need_cfm")):
        return False

    dto = entry.get("hsCodeDTO") if isinstance(entry.get("hsCodeDTO"), dict) else {}
    if _int_flag(dto.get("needConfirm")):
        return False

    try:
        cid = int(entry.get("categoryId") or dto.get("cid") or 0)
    except (TypeError, ValueError):
        return False
    if cid <= 0:
        return False

    cn_name = str(dto.get("cnName") or "").strip()
    if is_placeholder_category(cn_name):
        return False

    # 订单行上的类目名也是占位时，不信任 Admin
    if ord_row:
        row_name = str(ord_row.get("lvl1_ctgy_nm") or "").strip()
        if is_placeholder_category(row_name):
            return False

    return hs_from_admin_entry(entry) is not None


def prefetch_admin_goods_cache(
    goods_ids: list[str],
    cache: Optional[dict[str, dict[str, Any]]] = None,
) -> dict[str, dict[str, Any]]:
    """批量 listByGoodsIds，填充 cache（goodsId → entry）。"""
    out: dict[str, dict[str, Any]] = cache if cache is not None else {}
    ids = list({str(g).strip() for g in goods_ids if str(g).strip()})
    if not ids:
        return out
    missing = [g for g in ids if g not in out]
    if not missing:
        return out
    try:
        rows = list_goods_categories(missing)
    except Exception as exc:
        _log.warning("prefetch listByGoodsIds failed: %s", exc)
        return out
    seen: set[str] = set()
    for row in rows:
        gid = str(row.get("goodsId") or "").strip()
        if gid:
            out[gid] = row
            seen.add(gid)
    for gid in missing:
        if gid not in seen:
            out.setdefault(gid, {})
    return out


def find_admin_goods_entry(
    goods_id: str,
    cache: Optional[dict[str, dict[str, Any]]] = None,
) -> Optional[dict[str, Any]]:
    gid = str(goods_id or "").strip()
    if not gid:
        return None
    if cache is not None and gid in cache:
        return cache[gid]
    try:
        rows = list_goods_categories([gid])
    except Exception as exc:
        _log.warning("listByGoodsIds failed goods_id=%s: %s", gid, exc)
        return None
    for row in rows:
        if str(row.get("goodsId") or "").strip() == gid:
            if cache is not None:
                cache[gid] = row
            return row
    if cache is not None:
        cache[gid] = {}
    return None


def try_adopt_admin_category(
    product: dict[str, Any],
    *,
    goods_id: Optional[str] = None,
    ord_row: Optional[dict[str, Any]] = None,
    admin_cache: Optional[dict[str, dict[str, Any]]] = None,
) -> Optional[dict[str, Any]]:
    """
    Admin 已有可信类目时直接采纳，跳过 AI。
    返回更新后的 product；不可信则返回 None。
    """
    if product.get("category_status") in RESOLVED_MAPPING_STATUSES:
        return None

    gid = str(goods_id or product.get("source_product_id") or "").strip()
    if not gid:
        return None

    entry = find_admin_goods_entry(gid, admin_cache)
    if not entry:
        return None
    if not is_admin_category_trusted(entry, ord_row=ord_row):
        return None

    hs = hs_from_admin_entry(entry)
    if not hs:
        return None

    updated = confirm_product_mapping(
        {
            **product,
            "mapping_record": {
                **(product.get("mapping_record") or {}),
                "match_method": "admin_existing",
                "match_detail": f"Admin goodsCategory 已存在类目 cid={hs.get('category_id')}",
                "decision": "admin_existing",
            },
        },
        hs,
        resolution="auto",
        skip_admin_writeback=True,
        defer_side_effects=True,
    )
    return finalize_product_mapping_confirm(
        updated,
        resolution="auto",
        skip_admin_writeback=True,
    )

"""Admin 子单快照合并：状态字段跟 Admin，已确认品类不被旧数据覆盖。"""

from __future__ import annotations

from typing import Any, Optional

# 宽表品类/报关字段（field-catalog category 域）
CATEGORY_ROW_FIELDS = (
    "ctgy_id",
    "lvl1_ctgy_id",
    "lvl1_ctgy_nm",
    "lvl2_ctgy_nm",
    "lvl3_ctgy_nm",
    "lvl4_ctgy_nm",
    "cstm_hs_cd",
    "dcl_cn_nm",
    "dcl_en_nm",
    "ctgy_decl_lvl",
)

STATUS_FINGERPRINT_KEYS = (
    "ord_line_stat",
    "ord_stat",
    "rtn_stat",
    "abn_type_cd",
    "cfm_stat",
    "pur_prc",
    "post_fee",
    "ds_ord_amt",
    "ti_status",
    "to_status",
    "splr_item_id",
    "item_nm",
    "lvl1_ctgy_nm",
    "cstm_hs_cd",
    "dcl_cn_nm",
    "pur_no",
    "pay_time",
    "exprs_no",
    "sign_time",
)


def status_fingerprint(row: dict[str, Any]) -> str:
    return "|".join(f"{k}={row.get(k)!r}" for k in STATUS_FINGERPRINT_KEYS)


def _overlay_from_hs(hs: dict[str, Any]) -> dict[str, Any]:
    return {
        "ctgy_id": hs.get("category_id"),
        "lvl1_ctgy_nm": hs.get("category_cn_name"),
        "cstm_hs_cd": hs.get("hs_code"),
        "dcl_cn_nm": hs.get("declare_cn_name"),
        "dcl_en_nm": hs.get("declare_en_name"),
    }


def build_category_overlay(hs: dict[str, Any], *, source: str, at: str) -> dict[str, Any]:
    """品类写回 Admin 后锁定到快照，避免后续 Admin 拉取用旧宽表覆盖。"""
    fields = {k: v for k, v in _overlay_from_hs(hs).items() if v not in (None, "")}
    return {
        "locked": True,
        "source": source,
        "at": at,
        "hs": dict(hs),
        **fields,
    }


def apply_category_overlay(row: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = {**row, "_category_overlay": overlay}
    for key in CATEGORY_ROW_FIELDS:
        val = overlay.get(key)
        if val not in (None, ""):
            out[key] = val
    return out


def merge_admin_snapshot(prev: Optional[dict[str, Any]], incoming: dict[str, Any]) -> dict[str, Any]:
    """Admin 新行合并进本地快照；已锁定品类 overlay 优先于 Admin 宽表旧值。"""
    if not prev:
        return dict(incoming)
    merged = {**prev, **incoming}
    overlay = prev.get("_category_overlay")
    if isinstance(overlay, dict) and overlay.get("locked"):
        for key in CATEGORY_ROW_FIELDS:
            val = overlay.get(key)
            if val not in (None, ""):
                merged[key] = val
    # 保留本地元数据
    for meta in ("_category_overlay", "_cache_updated_at", "_admin_synced_at"):
        if meta in prev and meta not in incoming:
            merged[meta] = prev[meta]
    return merged

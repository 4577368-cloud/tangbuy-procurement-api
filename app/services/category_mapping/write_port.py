"""品类宽表写回契约（OrdLine WritePort 本地实现）。

与 web `categoryWriteBackFromHs` 对齐；真实库接入后换 HTTP/DB Port，不改字段名。
当前：构建 payload + 写入子单 overlay（含已有 cstm_hs_cd / dcl_*）。
"""

from __future__ import annotations

from typing import Any, Literal, Optional

MappingResolution = Literal["auto", "manual_confirm", "manual_correct"]


def category_writeback_from_hs(
    ord_line_no: str,
    hs: dict[str, Any],
    meta: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """镜像 TS categoryWriteBackFromHs。"""
    payload: dict[str, Any] = {
        "ord_line_no": ord_line_no,
        "ctgy_id": hs.get("category_id"),
        "lvl1_ctgy_nm": hs.get("category_cn_name"),
        "cstm_hs_cd": hs.get("hs_code"),
        "dcl_cn_nm": hs.get("declare_cn_name"),
        "dcl_en_nm": hs.get("declare_en_name"),
    }
    for key in (
        "lvl1_ctgy_id",
        "lvl2_ctgy_id",
        "lvl2_ctgy_nm",
        "lvl3_ctgy_id",
        "lvl3_ctgy_nm",
        "lvl4_ctgy_id",
        "lvl4_ctgy_nm",
    ):
        if hs.get(key) not in (None, ""):
            payload[key] = hs.get(key)
    if meta:
        if meta.get("mapping_confidence") is not None:
            payload["mapping_confidence"] = meta["mapping_confidence"]
        if meta.get("mapping_resolution") is not None:
            payload["mapping_resolution"] = meta["mapping_resolution"]
    return payload


def apply_ord_line_category_writeback(
    ord_line_nos: list[str],
    hs: dict[str, Any],
    *,
    source: str = "category_confirm",
    mapping_resolution: Optional[MappingResolution] = None,
    mapping_confidence: Optional[float] = None,
) -> dict[str, Any]:
    """确认成功路径：写本地子单品类 overlay（宽表字段契约）。"""
    from app.services.orders import line_cache

    lines = [str(x).strip() for x in ord_line_nos if str(x).strip()]
    if not lines or not hs:
        return {"ok": False, "reason": "empty", "count": 0}

    meta_hs = dict(hs)
    if mapping_resolution:
        meta_hs["mapping_resolution"] = mapping_resolution
    if mapping_confidence is not None:
        meta_hs["mapping_confidence"] = mapping_confidence

    line_cache.apply_category_overlay_to_lines(lines, meta_hs, source=source)
    return {"ok": True, "count": len(lines), "ord_line_nos": lines}

"""订单行叠加商品中心已确认 HS 映射（宽表 lvl1 常不准）。"""

from __future__ import annotations

from typing import Any, Optional

from app.services.category_mapping.mapping_quality import mapping_aligns_with_title
from app.services.products.store import (
    find_product_for_ord_line,
    find_product_in_index,
    is_valid_hs_mapping,
)


def enrich_row_mapped_category(
    row: dict[str, Any],
    *,
    product_index: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    if product_index is not None:
        product = find_product_in_index(row, product_index)
    else:
        product = find_product_for_ord_line(row)
    if not product:
        return row
    hs = product.get("hs_mapping")
    if not is_valid_hs_mapping(hs):
        return row

    title = str(row.get("item_nm") or row.get("item_nm_cn") or product.get("product_name") or "")
    ok, detail, score = mapping_aligns_with_title(title, hs)
    out = {
        **row,
        "mapped_category_cn": hs.get("category_cn_name"),
        "mapped_declare_cn": hs.get("declare_cn_name"),
        "mapped_hs_code": hs.get("hs_code"),
        "mapped_category_id": hs.get("category_id"),
        "mapping_quality_ok": ok,
        "mapping_quality_detail": detail,
        "mapping_quality_score": score,
    }
    if ok:
        out["procurement_category_cn"] = hs.get("category_cn_name")
        out["procurement_declare_cn"] = hs.get("declare_cn_name")
        out["procurement_hs_code"] = hs.get("hs_code")
    return out

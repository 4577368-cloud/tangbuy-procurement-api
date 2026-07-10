"""Tangbuy Admin 品类读/写接口。"""

from __future__ import annotations

from typing import Any

from app.integrations.tangbuy_admin.client import TangbuyAdminError, admin_post_any

LIST_BY_GOODS_IDS = "/resource/goodsCategory/listByGoodsIds"
CHANGE_ITEM_CATEGORY = "/order/changeItemCategory"


def list_goods_categories(goods_ids: list[str]) -> list[dict[str, Any]]:
    """按 1688 goodsId 批量查询商品类目与 hsCodeDTO。"""
    ids = [str(g).strip() for g in goods_ids if str(g).strip()]
    if not ids:
        return []
    data = admin_post_any(LIST_BY_GOODS_IDS, {"goodsIds": ids}, timeout=60)
    if not data:
        return []
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        items = data.get("items") or data.get("list")
        if isinstance(items, list):
            return [x for x in items if isinstance(x, dict)]
    return []


def change_item_category(
    *,
    item_nos: list[str],
    cid: int,
    update_goods_category: bool = True,
) -> dict[str, Any]:
    """写回子单品类；ids 为 ord_line_no（itemNo）。"""
    ids = [str(x).strip() for x in item_nos if str(x).strip()]
    if not ids:
        raise TangbuyAdminError("changeItemCategory 需要至少一个子单号")
    try:
        cid_int = int(cid)
    except (TypeError, ValueError) as exc:
        raise TangbuyAdminError(f"无效 category_id: {cid}") from exc
    if cid_int <= 0:
        raise TangbuyAdminError(f"无效 category_id: {cid}")

    body = {
        "ids": ids,
        "cid": cid_int,
        "updateGoodsCategory": bool(update_goods_category),
    }
    data = admin_post_any(CHANGE_ITEM_CATEGORY, body, timeout=60)
    return data if isinstance(data, dict) else {"ok": True, "data": data}

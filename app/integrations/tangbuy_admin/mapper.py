"""Admin listOrderDetail → TangbuyOrdLineRow（宽表字段名）。"""

from __future__ import annotations

import re
from typing import Any, Optional

from app.integrations.tangbuy_admin.status_labels import ord_line_stat_label, ord_stat_label

_RE_OFFER = re.compile(r"/offer/(\d+)", re.I)


def _offer_id(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    m = _RE_OFFER.search(url)
    return m.group(1) if m else None


def _platform_label(store_source: Optional[str]) -> str:
    src = (store_source or "").lower()
    if src in ("alibaba", "1688"):
        return "1688"
    return store_source or "SHOP"


def _first_text(*values: Any) -> Optional[str]:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def map_track_list(tracks: list[dict[str, Any]]) -> list[dict[str, str]]:
    events: list[dict[str, str]] = []
    for t in tracks or []:
        content = str(t.get("content") or "").strip()
        if not content:
            continue
        events.append(
            {
                "time": str(t.get("createTime") or ""),
                "actor": str(t.get("userName") or "系统"),
                "action": content,
            }
        )
    return events


def _resolve_customer_paid(product_amt: Any, post_fee: Any, ds_raw: Any) -> float:
    """用户实付 = 商品 + 运费；Admin 的 userRealPayPrice 有时仅为商品价。"""
    product = float(product_amt or 0)
    shipping = float(post_fee or 0)
    payable = round(product + shipping, 2)
    try:
        raw = float(ds_raw) if ds_raw is not None else 0.0
    except (TypeError, ValueError):
        raw = 0.0
    if payable <= 0:
        return raw if raw > 0 else 0.0
    if raw <= 0:
        return payable
    if shipping > 0 and abs(raw - product) < 0.02 and abs(raw - payable) > 0.02:
        return payable
    if abs(raw - payable) < 0.02:
        return payable
    if raw >= payable - 0.02:
        return round(raw, 2)
    return payable


def _int_flag(v: Any) -> int:
    try:
        return 1 if int(v or 0) != 0 else 0
    except (TypeError, ValueError):
        return 0


def _photo_urls(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    urls: list[str] = []
    for entry in raw:
        if isinstance(entry, str) and entry.strip():
            urls.append(entry.strip())
            continue
        if isinstance(entry, dict):
            url = entry.get("url") or entry.get("imgUrl") or entry.get("image")
            if url:
                urls.append(str(url).strip())
    return urls


def map_admin_line(order: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    ext = item.get("extendField") if isinstance(item.get("extendField"), dict) else {}
    range_info = item.get("rangeInfo") if isinstance(item.get("rangeInfo"), dict) else {}
    ord_ext = order.get("extendField") if isinstance(order.get("extendField"), dict) else {}

    goods_status = item.get("goodsStatus")
    order_status = order.get("orderStatus")
    unit_price = item.get("unitPrice") or item.get("writePrice") or 0
    qty = item.get("nums") or 1
    product_amt = item.get("settlementTotalAmount")
    if product_amt is None:
        product_amt = float(unit_price) * int(qty)
    post_fee = order.get("postageAmount")
    if post_fee is None:
        post_fee = order.get("totalPostage") or item.get("backPostage") or 0
    ds_raw = item.get("userRealPayPrice") or item.get("realAmount") or order.get("userAmountTotal")

    category = item.get("categoryInfo") if isinstance(item.get("categoryInfo"), dict) else {}
    need_confirm = _int_flag(item.get("needConfirm")) or _int_flag(category.get("needConfirm"))
    photos = _photo_urls(item.get("photos")) + _photo_urls(item.get("finePhotos"))
    destination = _first_text(order.get("destination"))
    destination_id = order.get("destinationId")

    express_no = _first_text(item.get("expressNo"), item.get("express_no"))
    express_nm = _first_text(item.get("express"), item.get("expressName"), item.get("express_nm"))
    purchase_no = _first_text(item.get("purchaseNo"), item.get("purchase_no"))
    purchase_time = item.get("purchaseTime") or item.get("purchase_time")
    sign_time = item.get("signedTime") or item.get("signTime") or item.get("signed_time")

    out = {
        "ord_line_no": item.get("itemNo"),
        "ord_no": order.get("orderNo"),
        "out_ord_no": order.get("pluginOrderId"),
        "pay_no": order.get("payNo"),
        "ord_stat": order_status,
        "ord_stat_nm": ord_stat_label(order_status if isinstance(order_status, int) else None),
        "ord_line_stat": goods_status,
        "ord_line_stat_nm": ord_line_stat_label(goods_status if isinstance(goods_status, int) else None),
        "cfm_stat": item.get("confirmStatus"),
        "rtn_stat": item.get("returnStatus"),
        "abn_type_cd": item.get("exceptionType"),
        "is_need_cfm": need_confirm,
        "ord_type_cd": order.get("orderType"),
        "is_expd": _int_flag(order.get("expedited")),
        "dev": order.get("device"),
        "usr_lang": order.get("orderLanguage"),
        "usr_cntry_cd": str(destination_id) if destination_id is not None else None,
        "usr_cntry_nm": destination,
        "pkg_rcv_cntry": destination,
        "pkg_rcv_cntry_cd": str(destination_id) if destination_id is not None else None,
        "ccy": order.get("orderCurrency") or "CNY",
        "data_src": item.get("dataSource") or ord_ext.get("dataSource"),
        "usr_id": order.get("userId"),
        "usr_nm": order.get("userName"),
        "usr_vip_lvl": order.get("userVip"),
        "shop_id": ord_ext.get("shopId") or order.get("storeId"),
        "shop_nm": ord_ext.get("shopName") or order.get("storeName"),
        "splr_shop_id": order.get("storeId"),
        "splr_shop_nm": order.get("storeName") or order.get("sellerName"),
        "splr_shop_url": order.get("storeUrl"),
        "shop_pltf_cd": _platform_label(order.get("storeSource")),
        "pur_usr_nm": order.get("buyer"),
        "bd_usr_nm": range_info.get("bdUserName"),
        "item_id": ext.get("tangGoodsId"),
        "item_nm": ext.get("tangGoodName") or item.get("goodsName"),
        "item_nm_cn": ext.get("tangGoodNameCn") or item.get("goodsName"),
        "item_attr": item.get("goodsAttribute"),
        "item_attr_cn": item.get("goodsAttributeCn"),
        "item_url": item.get("goodsUrl") or ext.get("tangGoodsUrl"),
        "item_img": item.get("goodsImg"),
        "sku_id": item.get("skuId") or ext.get("frontSkuId"),
        "front_sku_id": ext.get("frontSkuId") or item.get("skuId"),
        "front_sku_attr_desc": ext.get("frontSkuAttributeDesc"),
        "item_bar_cd": item.get("barCode"),
        "settlement_real_amt": item.get("settlementRealAmount"),
        "pur_comm_disc_amt": ext.get("purchaseDiscount"),
        "pur_pref_amt": ext.get("preferredAmount"),
        "splr_item_id": item.get("goodsId") or _offer_id(item.get("goodsUrl")),
        "ctgy_id": item.get("categoryId"),
        "lvl1_ctgy_nm": category.get("cnName"),
        "dcl_en_nm": category.get("enName"),
        "ctgy_decl_lvl": category.get("declareLevel"),
        "item_wt": item.get("weight"),
        "vol_desc": str(item.get("volume")) if item.get("volume") not in (None, "") else None,
        "item_type_cd": item.get("goodsType"),
        "apply_refund_stat": item.get("applyRefundStatus"),
        "deferred_type_cd": item.get("deferredType"),
        "sales_type_cd": item.get("saleType"),
        "no_reason_7d_rtn": 1 if item.get("noReason7DReturn") else 0,
        "wh_dis_pack": _int_flag(item.get("disPack")),
        "wh_dis_tag": _int_flag(item.get("disTag")),
        "wh_can_fold": _int_flag(item.get("canFolding")),
        "ord_expire_time": order.get("expireTime"),
        "tang_item_url": ext.get("tangGoodsUrl"),
        "ord_cnt": qty,
        "pur_prc": unit_price,
        "pur_amt": product_amt,
        "post_fee": post_fee,
        "ds_ord_amt": _resolve_customer_paid(product_amt, post_fee, ds_raw),
        "disc_amt": item.get("discountAmount"),
        "pay_time": order.get("payTime"),
        "ord_pend_time": order.get("pendingTime") or item.get("eventTime"),
        "crt_time": item.get("createTime") or order.get("createTime"),
        "wh_id": order.get("storageNo"),
        "pur_sugg_prc": range_info.get("purchaseSuggestionPrice"),
        "pur_sugg_post_prc": range_info.get("purchaseSuggestionPostPrice"),
        "usr_rmk": _first_text(
            item.get("comment"),
            ext.get("userRemark"),
            ext.get("remark"),
            ord_ext.get("userRemark"),
            ord_ext.get("remark"),
        ),
        # 详情扩展（非宽表标准字段，供 UI 组装）
        "_timeline": map_track_list(item.get("trackList") or []),
        "qc_photo_urls": photos,
        "_plugin_order_id": order.get("pluginOrderId"),
        "_store_source": order.get("storeSource"),
        "_plugin_store_platform": ord_ext.get("dataSource") or item.get("dataSource"),
    }
    if purchase_time:
        out["pur_time"] = purchase_time
    if purchase_no:
        out["pur_no"] = purchase_no
    if sign_time:
        out["sign_time"] = sign_time
    if express_no:
        out["exprs_no"] = express_no
        out["is_exprs_dlyd"] = 1
    if express_nm:
        out["exprs_nm"] = express_nm
    return out


def flatten_admin_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for order in rows:
        items = order.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict):
                lines.append(map_admin_line(order, item))
    return lines

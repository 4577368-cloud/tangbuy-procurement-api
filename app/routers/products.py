"""HTTP 路由 — 商品中心。"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.api.deps import require_auth
from app.auth.permissions import grants_allow
from app.config.store import get_role_grants
from app.services.products import service as product_service
from app.services.products.product_jobs import schedule_product_pipeline
from app.services.products.service import apply_mapping_record, map_product_category_by_id, set_product_shipping
from app.services.products.store import confirm_product_mapping, update_product

router = APIRouter(prefix="/api/products", tags=["products"])


class AddProductBody(BaseModel):
    product: dict[str, Any]
    shipping: Optional[float] = None
    category: Optional[str] = None
    auto_map: Optional[bool] = True


class PatchProductBody(BaseModel):
    action: Optional[str] = None
    hs: Optional[dict[str, Any]] = None
    note: Optional[str] = None
    save_to_product: Optional[bool] = True
    record: Optional[dict[str, Any]] = None
    shipping: Optional[float] = None
    to_city: Optional[str] = None


class SwitchSupplierBody(BaseModel):
    offer_id: str
    product_id: Optional[str] = None
    ord_line_no: Optional[str] = None
    signal_type: Optional[str] = None
    note: Optional[str] = None


@router.get("")
def list_products() -> dict[str, Any]:
    return {
        "products": product_service.list_products(),
        "stats": product_service.get_product_stats(),
    }


@router.post("/switch-supplier")
def switch_supplier(request: Request, body: SwitchSupplierBody) -> dict[str, Any]:
    """用备选 B 全量替换原商品 A（关联子单迁挂 + 本地货源覆盖）。"""
    user = require_auth(request)
    grants = get_role_grants(user.role)
    if not grants_allow(grants, "product.catalog", "edit") and not grants_allow(
        grants, "product.add_to_store", "edit"
    ):
        raise HTTPException(status_code=403, detail="无换供权限")
    from app.services.products.switch_supplier import SwitchSupplierError, switch_supplier as do_switch

    try:
        return do_switch(
            product_id=body.product_id,
            ord_line_no=body.ord_line_no,
            offer_id=body.offer_id,
            operator=getattr(user, "name", None) or getattr(user, "username", None),
            signal_type=body.signal_type,
            note=body.note,
        )
    except SwitchSupplierError as exc:
        status = 404 if exc.code == "not_found" else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc


@router.post("")
def add_product(request: Request, body: AddProductBody) -> dict[str, Any]:
    user = require_auth(request)
    grants = get_role_grants(user.role)
    if not grants_allow(grants, "product.add_to_store", "edit"):
        raise HTTPException(status_code=403, detail="无「加入大店」权限")
    if not body.product:
        raise HTTPException(status_code=400, detail="缺少 product")
    result = product_service.add_product_from_find_item(
        body.product,
        shipping=body.shipping,
        category=body.category,
    )
    item = result["item"]
    if result.get("created") and body.auto_map is not False:
        mapped = map_product_category_by_id(item["tangbuy_product_id"])
        if mapped:
            item = mapped
    schedule_product_pipeline(item["tangbuy_product_id"])
    return {
        "product": item,
        "created": result["created"],
        "stats": product_service.get_product_stats(),
    }


class SyncFromOrdersBody(BaseModel):
    queue: Optional[str] = "pending_procurement"
    page: Optional[int] = 1
    page_size: Optional[int] = 200
    auto_map: Optional[bool] = True


class ProductPushBody(BaseModel):
    shop_url: str
    user_id: Optional[str] = None
    user_email: Optional[str] = None
    bd_name: Optional[str] = None


@router.post("/sync-from-orders")
def sync_from_orders(request: Request, body: SyncFromOrdersBody) -> dict[str, Any]:
    require_auth(request)
    from app.services.products.order_sync import sync_products_from_orders

    result = sync_products_from_orders(
        queue=(body.queue or "pending_procurement").strip(),
        page=max(1, int(body.page or 1)),
        page_size=max(1, min(500, int(body.page_size or 200))),
        auto_map=body.auto_map is not False,
    )
    stats = dict(result.get("stats") or {})
    stats["products_total"] = product_service.get_product_stats().get("total", 0)
    return {**result, "stats": stats}


@router.get("/push-targets")
def push_targets(request: Request) -> dict[str, Any]:
    require_auth(request)
    from app.services.products.push_presets import list_shop_presets

    return {"shops": list_shop_presets()}


@router.get("/alt-scan-status")
def alt_scan_status(request: Request) -> dict[str, Any]:
    require_auth(request)
    from app.services.products.product_jobs import get_alt_scan_quota_status

    return get_alt_scan_quota_status()


@router.post("/scan-pending-alternatives")
def scan_pending_alternatives(request: Request) -> dict[str, Any]:
    """批量入队待扫备选商品（内部/脚本用；日限额见 product_alt_scan_daily_limit，<=0 不限）。"""
    require_auth(request)
    from app.services.products.product_jobs import enqueue_alt_scan_batch

    return enqueue_alt_scan_batch()


@router.post("/{product_id}/enrich")
def enrich_product(request: Request, product_id: str) -> dict[str, Any]:
    require_auth(request)
    from app.services.products.enrichment import enrich_product_by_id

    return enrich_product_by_id(product_id)


class ScanAlternativesBody(BaseModel):
    """refresh=true：屏蔽上一轮备选后重新图搜换一批。"""
    refresh: Optional[bool] = False


@router.post("/{product_id}/scan-alternatives")
def scan_alternatives(
    request: Request,
    product_id: str,
    body: Optional[ScanAlternativesBody] = None,
) -> dict[str, Any]:
    """单商品备选：入队扫描（受日配额）。body.refresh 时屏蔽上一轮并重取。"""
    require_auth(request)
    from app.services.products.product_jobs import enqueue_alt_scan_product

    refresh = bool(body.refresh) if body else False
    result = enqueue_alt_scan_product(product_id, refresh=refresh)
    if not result.get("ok") and result.get("message") == "商品不存在":
        raise HTTPException(status_code=404, detail="商品不存在")
    return result


@router.post("/{product_id}/compare-alternatives")
def compare_alternatives(request: Request, product_id: str) -> dict[str, Any]:
    """当前 + 备选对照；LLM/规则标出最推荐。"""
    require_auth(request)
    from app.services.products.alt_compare import compare_product_alternatives

    result = compare_product_alternatives(product_id)
    if not result.get("ok") and result.get("error") == "商品不存在":
        raise HTTPException(status_code=404, detail="商品不存在")
    return result


@router.post("/{product_id}/push")
def push_product(request: Request, product_id: str, body: ProductPushBody) -> dict[str, Any]:
    user = require_auth(request)
    grants = get_role_grants(user.role)
    if not grants_allow(grants, "product.add_to_store", "edit") and not grants_allow(
        grants, "product.catalog", "edit"
    ):
        raise HTTPException(status_code=403, detail="无推送权限")
    existing = product_service.get_product_by_id(product_id)
    if not existing:
        raise HTTPException(status_code=404, detail="商品不存在")

    from app.services.products.push_presets import find_preset, record_product_push

    shop_url = (body.shop_url or "").strip()
    if not shop_url:
        raise HTTPException(status_code=400, detail="请选择店铺")
    preset = find_preset(shop_url)
    user_id = (body.user_id or (preset or {}).get("user_id") or "").strip()
    user_email = (body.user_email or (preset or {}).get("user_email") or "").strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="缺少用户 ID")

    entry = record_product_push(
        product_id=product_id,
        shop_url=shop_url,
        user_id=user_id,
        user_email=user_email,
        bd_name=(body.bd_name or (preset or {}).get("bd_name") or "").strip(),
        operator=getattr(user, "account", None) or getattr(user, "id", "") or "",
    )
    # 后续对接正式「授权上架」写接口；当前先落审计
    return {"ok": True, "push": entry, "message": "已记录推送（待接正式上架授权接口）"}


@router.post("/{product_id}")
def trigger_mapping(request: Request, product_id: str) -> dict[str, Any]:
    user = require_auth(request)
    grants = get_role_grants(user.role)
    if not grants_allow(grants, "product.category_mapping", "edit"):
        raise HTTPException(status_code=403, detail="无「品类映射」权限")
    product = map_product_category_by_id(product_id)
    if not product:
        raise HTTPException(status_code=404, detail="商品不存在")
    return {"product": product}


@router.patch("/{product_id}")
def patch_product(request: Request, product_id: str, body: PatchProductBody) -> dict[str, Any]:
    user = require_auth(request)
    grants = get_role_grants(user.role)
    existing = product_service.get_product_by_id(product_id)
    if not existing:
        raise HTTPException(status_code=404, detail="商品不存在")

    action = body.action or ""
    perm = "product.catalog" if action == "set_shipping" else "product.category_mapping"
    if not grants_allow(grants, perm, "edit"):
        raise HTTPException(status_code=403, detail="无权限执行该操作")

    product = existing
    if action == "set_shipping":
        val = body.shipping
        product = set_product_shipping(product_id, val, to_city=body.to_city) or existing
    elif action == "apply_mapping" and body.record:
        try:
            product = update_product(
                product_id,
                lambda p: apply_mapping_record(p, body.record or {}),
            ) or existing
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    elif action in ("confirm", "correct") and body.hs:
        product = update_product(
            product_id,
            lambda p: confirm_product_mapping(
                p,
                body.hs,
                reviewer_note=body.note,
                manual=action == "correct",
            ),
        ) or existing
    else:
        raise HTTPException(status_code=400, detail="无效操作")
    return {"product": product}

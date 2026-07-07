"""HTTP 路由 — 商品中心。"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.api.deps import require_auth
from app.auth.permissions import grants_allow
from app.config.store import get_role_grants
from app.services.products import service as product_service
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


@router.get("")
def list_products() -> dict[str, Any]:
    return {
        "products": product_service.list_products(),
        "stats": product_service.get_product_stats(),
    }


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
    return {
        "product": item,
        "created": result["created"],
        "stats": product_service.get_product_stats(),
    }


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
        product = update_product(
            product_id,
            lambda p: apply_mapping_record(p, body.record or {}),
        ) or existing
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

"""HTTP 路由 — 商品中心。"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.api.deps import require_auth
from app.auth.permissions import grants_allow
from app.config.store import get_role_grants
from app.services.products import service as product_service
from app.services.products.product_jobs import schedule_product_pipeline
from app.services.products.service import (
    apply_mapping_record,
    map_product_category_by_id,
    save_mapping_draft,
    set_product_shipping,
)
from app.services.products.store import (
    confirm_product_mapping,
    finalize_product_mapping_confirm,
    persist_product_mapping_side_effects,
    update_product,
)

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
    reason_key: Optional[str] = None


class TriggerMappingBody(BaseModel):
    wait: bool = False


class QuoteTranslateTitlesBody(BaseModel):
    titles: list[str]
    target_lang: str = "en"


@router.post("/quote/translate-titles")
def quote_translate_titles(request: Request, body: QuoteTranslateTitlesBody) -> dict[str, Any]:
    """报价单标题清洗 + 英/法翻译（买家可见文案）。"""
    require_auth(request)
    from app.services.products.quote_title import translate_quote_titles as do_translate

    titles = [str(t or "").strip() for t in body.titles if str(t or "").strip()]
    if not titles:
        raise HTTPException(status_code=400, detail="缺少标题")
    lang = (body.target_lang or "en").strip().lower()
    if lang not in ("en", "fr"):
        raise HTTPException(status_code=400, detail="仅支持 en / fr")
    try:
        translated = do_translate(titles, target_lang=lang)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"titles": translated, "target_lang": lang}


@router.get("")
def list_products() -> dict[str, Any]:
    items = product_service.list_products()
    return {
        "products": items,
        "stats": {"total": len(items)},
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
            reason_key=body.reason_key,
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
    all_pages: Optional[bool] = False
    max_pages: Optional[int] = 100
    wait: bool = False


class ProductPushBody(BaseModel):
    shop_url: str
    user_id: Optional[str] = None
    user_email: Optional[str] = None
    bd_name: Optional[str] = None


@router.get("/find-cache")
def list_find_cache(request: Request, q: Optional[str] = None, limit: int = 100) -> dict[str, Any]:
    require_auth(request)
    from app.services.products.find_cache import list_find_cache as load_cached

    items = load_cached(limit=limit, q=q)
    return {"items": items, "total": len(items)}


@router.post("/sync-from-orders")
def sync_from_orders(request: Request, body: SyncFromOrdersBody):
    require_auth(request)
    from app.services.background_jobs import create_job, run_job
    from app.services.products.sync_jobs import execute_products_sync_from_orders

    payload = {
        "queue": (body.queue or "pending_procurement").strip(),
        "page": body.page,
        "page_size": body.page_size,
        "auto_map": body.auto_map is not False,
        "all_pages": body.all_pages is True,
        "max_pages": body.max_pages,
    }
    if body.wait:
        return execute_products_sync_from_orders(**payload)

    label = (body.queue or "orders").strip() or "orders"
    job_id = create_job("product_sync", label=label)
    run_job(job_id, lambda: execute_products_sync_from_orders(**payload))
    return JSONResponse(
        status_code=202,
        content={"job_id": job_id, "status": "pending", "kind": "product_sync"},
    )


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
        from app.config.demo_submit import demo_submit_stub, is_demo_submit_always_success

        if is_demo_submit_always_success():
            return demo_submit_stub(message="已记录推送")
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


@router.get("/{product_id}")
def get_product(product_id: str) -> dict[str, Any]:
    product = product_service.get_product_by_id(product_id)
    if not product:
        raise HTTPException(status_code=404, detail="商品不存在")
    return {"product": product}


@router.post("/{product_id}")
def trigger_mapping(
    request: Request,
    product_id: str,
    body: TriggerMappingBody = TriggerMappingBody(),
):
    user = require_auth(request)
    grants = get_role_grants(user.role)
    if not grants_allow(grants, "product.category_mapping", "edit"):
        raise HTTPException(status_code=403, detail="无「品类映射」权限")
    wait = body.wait
    if wait:
        product = map_product_category_by_id(product_id)
        if not product:
            raise HTTPException(status_code=404, detail="商品不存在")
        return {"product": product}

    from app.services.background_jobs import create_job, run_job

    job_id = create_job("product_mapping", label=product_id)

    def _run() -> dict[str, Any]:
        product = map_product_category_by_id(product_id)
        if not product:
            raise RuntimeError("商品不存在")
        return {"product": product}

    run_job(job_id, _run)
    return JSONResponse(
        status_code=202,
        content={"job_id": job_id, "status": "pending", "kind": "product_mapping"},
    )


@router.post("/{product_id}/admin-writeback")
def trigger_admin_writeback(request: Request, product_id: str) -> dict[str, Any]:
    """手动触发 Admin 品类回写（映射已确认后）。"""
    user = require_auth(request)
    grants = get_role_grants(user.role)
    if not grants_allow(grants, "product.category_mapping", "edit"):
        raise HTTPException(status_code=403, detail="无「品类映射」权限")
    from app.services.category_mapping.admin_writeback import (
        prefetch_writeback_category_labels,
        schedule_admin_writeback,
    )
    from app.services.products.store import get_product_by_id, is_valid_hs_mapping, _now_iso, update_product

    product = get_product_by_id(product_id)
    if not product:
        raise HTTPException(status_code=404, detail="商品不存在")
    hs = product.get("hs_mapping")
    if not is_valid_hs_mapping(hs):
        raise HTTPException(status_code=400, detail="请先完成品类映射")

    from app.services.category_mapping.admin_writeback import (
        collect_item_nos,
        should_skip_admin_writeback,
    )

    if should_skip_admin_writeback(product, hs, item_nos=collect_item_nos(product))[0]:
        return {"product": product, "skipped_duplicate": True}

    from_name, to_name, from_cid, to_cid = prefetch_writeback_category_labels(product, hs)

    def _mark_writing(p: dict[str, Any]) -> dict[str, Any]:
        mapping = dict(p.get("mapping_record") or {})
        mapping["admin_writeback"] = {
            "status": "writing",
            "at": _now_iso(),
            "from_category": from_name or None,
            "to_category": to_name or None,
            "from_cid": from_cid or None,
            "to_cid": to_cid or None,
        }
        p["mapping_record"] = mapping
        return p

    update_product(product_id, _mark_writing)
    schedule_admin_writeback(product_id, hs, resolution="manual_confirm")
    updated = get_product_by_id(product_id)
    return {"product": updated}


@router.patch("/{product_id}")
def patch_product(request: Request, product_id: str, body: PatchProductBody) -> dict[str, Any]:
    user = require_auth(request)
    grants = get_role_grants(user.role)
    existing = product_service.get_product_by_id(product_id)
    if not existing:
        from app.config.demo_submit import demo_submit_stub, is_demo_submit_always_success

        if is_demo_submit_always_success():
            return demo_submit_stub(product={"tangbuy_product_id": product_id})
        raise HTTPException(status_code=404, detail="商品不存在")

    action = body.action or ""
    perm = "product.catalog" if action == "set_shipping" else "product.category_mapping"
    if not grants_allow(grants, perm, "edit"):
        raise HTTPException(status_code=403, detail="无权限执行该操作")

    product = existing
    if action == "set_shipping":
        val = body.shipping
        product = set_product_shipping(product_id, val, to_city=body.to_city) or existing
    elif action == "save_mapping_draft" and body.record:
        product = update_product(
            product_id,
            lambda p: save_mapping_draft(p, body.record or {}),
        ) or existing
        finalize_product_mapping_confirm(product, resolution="auto")
    elif action == "apply_mapping" and body.record:
        record = body.record or {}
        try:
            product = update_product(
                product_id,
                lambda p: apply_mapping_record(p, record),
            ) or existing
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        hs = record.get("suggested_hs") or record.get("corrected_hs") or {}
        review = str(record.get("review_status") or "")
        if review == "corrected":
            resolution = "manual_correct"
        elif review in ("confirmed", "pending"):
            resolution = "manual_confirm"
        else:
            resolution = "auto"
        persist_product_mapping_side_effects(
            product,
            hs if isinstance(hs, dict) else {},
            manual=review == "corrected",
            resolution=resolution,
            skip_admin_writeback=body.save_to_product is False,
        )
    elif action in ("confirm", "correct") and body.hs:
        product = update_product(
            product_id,
            lambda p: confirm_product_mapping(
                p,
                body.hs,
                reviewer_note=body.note,
                manual=action == "correct",
                resolution="manual_correct" if action == "correct" else "manual_confirm",
                skip_admin_writeback=body.save_to_product is False,
                defer_side_effects=True,
            ),
        ) or existing
        persist_product_mapping_side_effects(
            product,
            body.hs,
            manual=action == "correct",
            resolution="manual_correct" if action == "correct" else "manual_confirm",
            skip_admin_writeback=body.save_to_product is False,
        )
    else:
        raise HTTPException(status_code=400, detail="无效操作")
    return {"product": product}

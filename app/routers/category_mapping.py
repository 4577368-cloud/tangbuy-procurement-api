"""HTTP 路由 — 品类映射。"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.api.deps import require_auth
from app.auth.permissions import grants_allow
from app.config.store import get_role_grants
from app.integrations.skill_cli import run_category_suggest
from app.services.category_mapping import feedback
from app.services.category_mapping.catalog_search import (
    build_suggest_markdown,
    is_catalog_search_ready,
    is_category_data_ready,
    run_category_search,
    search_hs_catalog,
)

router = APIRouter(prefix="/api/category-mapping", tags=["category-mapping"])


class FeedbackBody(BaseModel):
    type: str
    entry: dict[str, Any]


class SuggestBody(BaseModel):
    title: Optional[str] = None
    source_title: Optional[str] = None
    product_title: Optional[str] = None
    source_category_hint: Optional[str] = None
    goods_id: Optional[str] = None
    image_url: Optional[str] = None
    item_id: Optional[str] = None
    mapping_id: Optional[str] = None
    external_order_no: Optional[str] = None


@router.get("/suggest")
def suggest_meta() -> dict[str, Any]:
    return {
        "ready": is_category_data_ready(),
        "fields": [
            "category_cn_name",
            "category_en_name",
            "category_id",
            "hs_code",
            "declare_cn_name",
            "declare_en_name",
            "decision",
            "semantic_candidates",
            "matched_keywords",
            "vision_summary",
        ],
    }


@router.post("/suggest")
def suggest_run(body: SuggestBody) -> dict[str, Any]:
    title = (body.source_title or body.title or body.product_title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="需要商品标题")
    if not is_category_data_ready():
        raise HTTPException(status_code=503, detail="品类数据未构建，请运行 python3 scripts/build-category-data.py")
    result = run_category_suggest(
        title,
        hint=body.source_category_hint,
        goods_id=body.goods_id,
        image_url=body.image_url,
    )
    return {
        "result": result,
        "markdown": build_suggest_markdown(result),
        "mapping_id": body.mapping_id,
    }


@router.get("/search")
def search_catalog(q: str = "") -> dict[str, Any]:
    query = q.strip()
    if not query:
        return {"results": []}
    if is_catalog_search_ready():
        return {"success": True, "results": search_hs_catalog(query, 12)}
    return run_category_search(query, 12)


@router.post("/feedback")
def save_feedback(request: Request, body: FeedbackBody) -> dict[str, bool]:
    user = require_auth(request)
    grants = get_role_grants(user.role)
    if not grants_allow(grants, "product.category_mapping", "edit"):
        raise HTTPException(status_code=403, detail="无「品类映射」权限")
    if not body.entry or not body.type:
        raise HTTPException(status_code=400, detail="需要 type 和 entry")
    if body.type == "feedback":
        feedback.append_feedback(body.entry)
    elif body.type == "archive":
        feedback.append_archive(body.entry)
    else:
        raise HTTPException(status_code=400, detail="未知 type")
    return {"ok": True}

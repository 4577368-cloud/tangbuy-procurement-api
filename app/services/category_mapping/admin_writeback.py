"""品类映射确认后写回 Tangbuy Admin。"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from app.config.store import get_business_config
from app.integrations.tangbuy_admin.category_api import change_item_category, list_goods_categories
from app.integrations.tangbuy_admin.client import TangbuyAdminError

_log = logging.getLogger(__name__)

AdminWritebackResolution = Literal["auto", "manual_confirm", "manual_correct"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def admin_writeback_enabled() -> bool:
    cfg = get_business_config()
    rules = cfg.get("rules") if isinstance(cfg.get("rules"), dict) else {}
    return bool(rules.get("admin_category_writeback", True))


def collect_item_nos(product: dict[str, Any]) -> list[str]:
    lines = product.get("linked_ord_lines") or []
    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        oid = str(line or "").strip()
        if oid and oid not in seen:
            seen.add(oid)
            out.append(oid)
    return out


def _admin_category_label(entry: dict[str, Any]) -> tuple[int, str]:
    dto = entry.get("hsCodeDTO") if isinstance(entry.get("hsCodeDTO"), dict) else {}
    try:
        cid = int(entry.get("categoryId") or dto.get("cid") or 0)
    except (TypeError, ValueError):
        cid = 0
    name = str(dto.get("cnName") or entry.get("categoryName") or "").strip()
    return cid, name


def _fetch_admin_from_category(goods_id: str) -> tuple[int, str]:
    gid = str(goods_id or "").strip()
    if not gid or gid == "0":
        return 0, ""
    try:
        rows = list_goods_categories([gid])
        if not rows:
            return 0, ""
        return _admin_category_label(rows[0])
    except Exception as exc:
        _log.debug("listByGoodsIds before writeback failed goods=%s: %s", gid, exc)
        return 0, ""


def push_category_to_admin(
    *,
    product: dict[str, Any],
    hs: dict[str, Any],
    resolution: AdminWritebackResolution = "auto",
    item_nos: Optional[list[str]] = None,
) -> dict[str, Any]:
    """写回 Admin changeItemCategory；失败不抛异常，返回状态 dict。"""
    if not admin_writeback_enabled():
        return {"status": "skipped", "reason": "admin_category_writeback disabled"}

    try:
        cid = int(hs.get("category_id") or 0)
    except (TypeError, ValueError):
        cid = 0
    if cid <= 0:
        return {"status": "skipped", "reason": "invalid category_id"}

    ids = item_nos if item_nos is not None else collect_item_nos(product)
    if not ids:
        return {"status": "skipped", "reason": "no linked ord_line_no"}

    prev = (product.get("mapping_record") or {}).get("admin_writeback") or {}
    if (
        prev.get("status") == "ok"
        and int(prev.get("cid") or 0) == cid
        and set(prev.get("item_nos") or []) >= set(ids)
    ):
        return {**prev, "status": "ok", "skipped_duplicate": True}

    at = _now_iso()
    goods_id = str(product.get("source_product_id") or "").strip()
    from_cid, from_name = _fetch_admin_from_category(goods_id)
    if not from_name:
        hint = str(product.get("platform_category_hint") or product.get("category") or "").strip()
        if hint and hint not in ("待映射", "其它", "其他"):
            from_name = hint
    to_name = str(hs.get("category_cn_name") or "").strip()

    try:
        change_item_category(item_nos=ids, cid=cid, update_goods_category=True)
        return {
            "status": "ok",
            "at": at,
            "item_nos": ids,
            "cid": cid,
            "goods_id": goods_id or None,
            "resolution": resolution,
            "from_cid": from_cid or None,
            "from_category": from_name or None,
            "to_cid": cid,
            "to_category": to_name or None,
        }
    except TangbuyAdminError as exc:
        _log.warning(
            "Admin changeItemCategory failed cid=%s items=%s: %s",
            cid,
            ids,
            exc,
        )
        return {
            "status": "failed",
            "at": at,
            "item_nos": ids,
            "cid": cid,
            "goods_id": goods_id or None,
            "resolution": resolution,
            "from_cid": from_cid or None,
            "from_category": from_name or None,
            "to_cid": cid,
            "to_category": to_name or None,
            "error": str(exc),
            "code": getattr(exc, "status", None),
            "needs_retry": True,
        }
    except Exception as exc:
        _log.exception("Admin changeItemCategory unexpected error")
        return {
            "status": "failed",
            "at": at,
            "item_nos": ids,
            "cid": cid,
            "goods_id": goods_id or None,
            "resolution": resolution,
            "from_cid": from_cid or None,
            "from_category": from_name or None,
            "to_cid": cid,
            "to_category": to_name or None,
            "error": str(exc),
            "needs_retry": True,
        }


def attach_admin_writeback(
    product: dict[str, Any],
    hs: dict[str, Any],
    *,
    resolution: AdminWritebackResolution = "auto",
    item_nos: Optional[list[str]] = None,
) -> dict[str, Any]:
    """执行写回并把结果写入 mapping_record.admin_writeback。"""
    result = push_category_to_admin(
        product=product,
        hs=hs,
        resolution=resolution,
        item_nos=item_nos,
    )
    mapping = dict(product.get("mapping_record") or {})
    mapping["admin_writeback"] = result
    product["mapping_record"] = mapping
    return product


def schedule_admin_writeback(
    product_id: str,
    hs: dict[str, Any],
    *,
    resolution: AdminWritebackResolution = "auto",
    item_nos: Optional[list[str]] = None,
) -> None:
    """异步写回 Admin；商品中心先展示映射类目，再显示「回写中」。"""
    if not admin_writeback_enabled():
        return
    pid = (product_id or "").strip()
    if not pid:
        return

    def _run() -> None:
        from app.services.products.store import get_product_by_id, update_product

        product = get_product_by_id(pid)
        if not product:
            return
        update_product(
            pid,
            lambda p: attach_admin_writeback(p, hs, resolution=resolution, item_nos=item_nos),
        )

    threading.Thread(
        target=_run,
        daemon=True,
        name=f"admin-wb-{pid[:12]}",
    ).start()

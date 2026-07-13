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

_writeback_locks: dict[str, threading.Lock] = {}
_writeback_locks_guard = threading.Lock()


def _writeback_lock(product_id: str) -> threading.Lock:
    key = (product_id or "").strip()
    with _writeback_locks_guard:
        if key not in _writeback_locks:
            _writeback_locks[key] = threading.Lock()
        return _writeback_locks[key]

AdminWritebackResolution = Literal["auto", "manual_confirm", "manual_correct"]

WRITEBACK_STALE_SECONDS = 120

_SKIP_REASON_LABELS = {
    "no_items": "暂无可回写子单（商品未关联 TI）",
    "invalid_cid": "类目无效",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _writing_placeholder(
    product: dict[str, Any],
    hs: dict[str, Any],
    *,
    at: str,
) -> dict[str, Any]:
    from_name = str(product.get("platform_category_hint") or product.get("category") or "").strip()
    if from_name in ("待映射", "其它", "其他"):
        from_name = ""
    to_name = str(hs.get("category_cn_name") or hs.get("declare_cn_name") or "").strip()
    try:
        to_cid = int(hs.get("category_id") or 0)
    except (TypeError, ValueError):
        to_cid = 0
    return {
        "status": "writing",
        "at": at,
        "from_category": from_name or None,
        "to_category": to_name or None,
        "from_cid": None,
        "to_cid": to_cid or None,
    }


def _skipped_writeback_result(
    *,
    reason: str,
    item_nos: list[str],
    hs: dict[str, Any],
    resolution: AdminWritebackResolution,
    at: Optional[str] = None,
) -> dict[str, Any]:
    cid = target_hs_cid(hs)
    to_name = str(hs.get("category_cn_name") or hs.get("declare_cn_name") or "").strip()
    return {
        "status": "skipped",
        "at": at or _now_iso(),
        "reason": _SKIP_REASON_LABELS.get(reason, reason),
        "skip_reason": reason,
        "item_nos": item_nos,
        "to_cid": cid or None,
        "to_category": to_name or None,
        "resolution": resolution,
    }


def resolve_admin_writeback_placeholder(
    product: dict[str, Any],
    hs: dict[str, Any],
    *,
    at: str,
    resolution: AdminWritebackResolution = "auto",
    item_nos: Optional[list[str]] = None,
) -> dict[str, Any]:
    """映射确认时同步判定：可写回 → writing；否则直接终态，避免长期卡在回写中。"""
    ids = item_nos if item_nos is not None else collect_item_nos(product)
    skip, skip_reason = should_skip_admin_writeback(product, hs, item_nos=ids)
    if skip:
        if skip_reason in ("already_ok", "in_flight", "admin_already"):
            return _build_skip_writeback_result(
                product,
                hs,
                reason=skip_reason,
                item_nos=ids,
                resolution=resolution,
            )
        return _skipped_writeback_result(
            reason=skip_reason or "skipped",
            item_nos=ids,
            hs=hs,
            resolution=resolution,
            at=at,
        )
    return _writing_placeholder(product, hs, at=at)


def _parse_writeback_at(at: Optional[str]) -> Optional[datetime]:
    if not at:
        return None
    raw = str(at).strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def reconcile_stale_admin_writeback(product: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """writing 超时（含服务重启中断）→ failed / skipped，避免商品中心长期展示回写中。"""
    mapping = dict(product.get("mapping_record") or {})
    wb = dict(mapping.get("admin_writeback") or {})
    if wb.get("status") != "writing":
        return product, False

    hs = product.get("hs_mapping") if isinstance(product.get("hs_mapping"), dict) else {}
    ids = collect_item_nos(product)
    resolution = wb.get("resolution") if wb.get("resolution") in ("auto", "manual_confirm", "manual_correct") else "auto"

    if not ids:
        mapping["admin_writeback"] = _skipped_writeback_result(
            reason="no_items",
            item_nos=[],
            hs=hs or {},
            resolution=resolution,  # type: ignore[arg-type]
        )
        product = {**product, "mapping_record": mapping}
        return product, True

    started = _parse_writeback_at(wb.get("at"))
    if started is None:
        age_seconds = WRITEBACK_STALE_SECONDS + 1
    else:
        age_seconds = (datetime.now(timezone.utc) - started.astimezone(timezone.utc)).total_seconds()

    if age_seconds < WRITEBACK_STALE_SECONDS:
        return product, False

    if not ids:
        mapping["admin_writeback"] = _skipped_writeback_result(
            reason="no_items",
            item_nos=[],
            hs=hs or {},
            resolution=resolution,  # type: ignore[arg-type]
        )
    else:
        mapping["admin_writeback"] = {
            "status": "failed",
            "at": _now_iso(),
            "item_nos": ids,
            "resolution": resolution,
            "from_category": wb.get("from_category"),
            "to_category": wb.get("to_category"),
            "from_cid": wb.get("from_cid"),
            "to_cid": wb.get("to_cid") or target_hs_cid(hs or {}),
            "error": "回写未完成（可能服务重启），请重试",
            "needs_retry": True,
            "stale_recovered": True,
        }

    product = {**product, "mapping_record": mapping}
    return product, True


def admin_writeback_enabled() -> bool:
    cfg = get_business_config()
    rules = cfg.get("rules") if isinstance(cfg.get("rules"), dict) else {}
    return bool(rules.get("admin_category_writeback", True))


from app.services.category_mapping.admin_writeback_collect import collect_item_nos
def collect_global_ok_item_nos_for_cid(
    cid: int,
    *,
    exclude_product_id: Optional[str] = None,
) -> set[str]:
    """其它商品已成功写回同 cid 的子单，避免跨商品重复 changeItemCategory。"""
    try:
        cid_int = int(cid)
    except (TypeError, ValueError):
        return set()
    if cid_int <= 0:
        return set()
    exclude = (exclude_product_id or "").strip()
    out: set[str] = set()
    try:
        from app.services.products.store import load_products
    except Exception:
        return out
    for p in load_products():
        pid = str(p.get("tangbuy_product_id") or "").strip()
        if exclude and pid == exclude:
            continue
        wb = (p.get("mapping_record") or {}).get("admin_writeback") or {}
        if wb.get("status") != "ok":
            continue
        try:
            wc = int(wb.get("cid") or wb.get("to_cid") or 0)
        except (TypeError, ValueError):
            continue
        if wc != cid_int:
            continue
        for item in wb.get("item_nos") or []:
            s = str(item or "").strip()
            if s:
                out.add(s)
    return out


def filter_pending_item_nos(
    product: dict[str, Any],
    hs: dict[str, Any],
    item_nos: list[str],
) -> list[str]:
    """仅返回尚未成功写回 Admin 的子单号（本商品记录 + 全局同 cid 成功记录）。"""
    cid = target_hs_cid(hs)
    if cid <= 0:
        return []
    prev = (product.get("mapping_record") or {}).get("admin_writeback") or {}
    done: set[str] = set()
    if prev.get("status") == "ok":
        try:
            if int(prev.get("cid") or prev.get("to_cid") or 0) == cid:
                done.update(str(x).strip() for x in (prev.get("item_nos") or []) if str(x).strip())
        except (TypeError, ValueError):
            pass
    done.update(
        collect_global_ok_item_nos_for_cid(
            cid,
            exclude_product_id=str(product.get("tangbuy_product_id") or ""),
        )
    )
    pending: list[str] = []
    seen: set[str] = set()
    for item in item_nos:
        s = str(item or "").strip()
        if not s or s in done or s in seen:
            continue
        seen.add(s)
        pending.append(s)
    return pending


def target_hs_cid(hs: dict[str, Any]) -> int:
    try:
        return int(hs.get("category_id") or 0)
    except (TypeError, ValueError):
        return 0


def local_writeback_covers(
    prev: dict[str, Any],
    *,
    cid: int,
    item_nos: list[str],
) -> bool:
    """本地已记录同目标类目的成功回写，且子单范围已覆盖。"""
    if prev.get("status") != "ok":
        return False
    try:
        prev_cid = int(prev.get("cid") or prev.get("to_cid") or 0)
    except (TypeError, ValueError):
        return False
    if cid <= 0 or prev_cid != cid:
        return False
    if not item_nos:
        return True
    return set(item_nos) <= set(prev.get("item_nos") or [])


def writeback_in_flight_for_same_target(
    prev: dict[str, Any],
    *,
    cid: int,
) -> bool:
    """同目标类目已有回写任务进行中，避免并发重复提交。"""
    if prev.get("status") != "writing":
        return False
    if prev.get("needs_retry"):
        return False
    started = _parse_writeback_at(prev.get("at"))
    if started is not None:
        age = (datetime.now(timezone.utc) - started.astimezone(timezone.utc)).total_seconds()
        if age >= WRITEBACK_STALE_SECONDS:
            return False
    try:
        prev_cid = int(prev.get("to_cid") or prev.get("cid") or 0)
    except (TypeError, ValueError):
        return False
    return cid > 0 and prev_cid == cid


def should_skip_admin_writeback(
    product: dict[str, Any],
    hs: dict[str, Any],
    *,
    item_nos: Optional[list[str]] = None,
    ignore_in_flight: bool = False,
) -> tuple[bool, str]:
    """
    判断是否跳过 Admin 回写。
    reason: already_ok | in_flight | admin_already | no_items | invalid_cid
    """
    cid = target_hs_cid(hs)
    if cid <= 0:
        return True, "invalid_cid"

    ids = item_nos if item_nos is not None else collect_item_nos(product)
    if not ids:
        return True, "no_items"

    pending = filter_pending_item_nos(product, hs, ids)
    if not pending:
        return True, "already_ok"

    prev = (product.get("mapping_record") or {}).get("admin_writeback") or {}
    if not ignore_in_flight and writeback_in_flight_for_same_target(prev, cid=cid):
        return True, "in_flight"

    return False, ""


def _build_skip_writeback_result(
    product: dict[str, Any],
    hs: dict[str, Any],
    *,
    reason: str,
    item_nos: list[str],
    resolution: AdminWritebackResolution,
) -> dict[str, Any]:
    prev = (product.get("mapping_record") or {}).get("admin_writeback") or {}
    if reason == "already_ok":
        return {**prev, "status": "ok", "skipped_duplicate": True, "skip_reason": reason}
    if reason == "in_flight":
        return {**prev, "skipped_duplicate": True, "skip_reason": reason}

    at = _now_iso()
    cid = target_hs_cid(hs)
    goods_id = str(product.get("source_product_id") or "").strip()
    from_cid, from_name = _fetch_admin_from_category(goods_id)
    if not from_name:
        from_name = str(prev.get("from_category") or product.get("platform_category_hint") or "").strip()
    to_name = str(hs.get("category_cn_name") or hs.get("declare_cn_name") or prev.get("to_category") or "").strip()
    return {
        "status": "ok",
        "at": at,
        "item_nos": item_nos,
        "cid": cid,
        "goods_id": goods_id or None,
        "resolution": resolution,
        "from_cid": from_cid or None,
        "from_category": from_name or None,
        "to_cid": cid,
        "to_category": to_name or None,
        "skipped_duplicate": True,
        "skip_reason": reason,
    }


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


def prefetch_writeback_category_labels(
    product: dict[str, Any],
    hs: dict[str, Any],
) -> tuple[str, str, int, int]:
    """确认映射时预取回写前后类目名（toast 与写回结果一致）。"""
    goods_id = str(product.get("source_product_id") or "").strip()
    from_cid, from_name = _fetch_admin_from_category(goods_id)
    if not from_name:
        hint = str(product.get("platform_category_hint") or product.get("category") or "").strip()
        if hint and hint not in ("待映射", "其它", "其他"):
            from_name = hint
    try:
        to_cid = int(hs.get("category_id") or 0)
    except (TypeError, ValueError):
        to_cid = 0
    to_name = str(hs.get("category_cn_name") or hs.get("declare_cn_name") or "").strip()
    return from_name, to_name, from_cid, to_cid


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

    pending = filter_pending_item_nos(product, hs, ids)
    if not pending:
        return _build_skip_writeback_result(
            product,
            hs,
            reason="already_ok",
            item_nos=ids,
            resolution=resolution,
        )

    skip, skip_reason = should_skip_admin_writeback(
        product,
        hs,
        item_nos=ids,
        ignore_in_flight=True,
    )
    if skip:
        if skip_reason in ("already_ok", "in_flight", "admin_already"):
            return _build_skip_writeback_result(
                product,
                hs,
                reason=skip_reason,
                item_nos=ids,
                resolution=resolution,
            )
        return {"status": "skipped", "reason": skip_reason}

    at = _now_iso()
    goods_id = str(product.get("source_product_id") or "").strip()
    pre = (product.get("mapping_record") or {}).get("admin_writeback") or {}
    from_cid, from_name = _fetch_admin_from_category(goods_id)
    if not from_name:
        from_name = str(pre.get("from_category") or "").strip()
    if not from_name:
        hint = str(product.get("platform_category_hint") or product.get("category") or "").strip()
        if hint and hint not in ("待映射", "其它", "其他"):
            from_name = hint
    try:
        from_cid = int(from_cid or pre.get("from_cid") or 0)
    except (TypeError, ValueError):
        from_cid = 0
    to_name = str(hs.get("category_cn_name") or hs.get("declare_cn_name") or "").strip()
    if not to_name:
        to_name = str(pre.get("to_category") or "").strip()

    try:
        change_item_category(item_nos=pending, cid=cid, update_goods_category=True)
        prev_ok = set()
        if pre.get("status") == "ok":
            try:
                if int(pre.get("cid") or pre.get("to_cid") or 0) == cid:
                    prev_ok.update(str(x).strip() for x in (pre.get("item_nos") or []) if str(x).strip())
            except (TypeError, ValueError):
                pass
        all_written = sorted(prev_ok | set(pending))
        return {
            "status": "ok",
            "at": at,
            "item_nos": all_written,
            "cid": cid,
            "goods_id": goods_id or None,
            "resolution": resolution,
            "from_cid": from_cid or None,
            "from_category": from_name or None,
            "to_cid": cid,
            "to_category": to_name or None,
            "written_item_nos": pending,
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

        lock = _writeback_lock(pid)
        if not lock.acquire(blocking=False):
            _log.debug("admin writeback skipped: lock busy pid=%s", pid)
            return
        updated: Optional[dict[str, Any]] = None
        try:
            product = get_product_by_id(pid)
            if not product:
                return
            ids = item_nos if item_nos is not None else collect_item_nos(product)
            skip, skip_reason = should_skip_admin_writeback(
                product,
                hs,
                item_nos=ids,
                ignore_in_flight=True,
            )
            if skip:
                updated_skip = update_product(
                    pid,
                    lambda p: {
                        **p,
                        "mapping_record": {
                            **(p.get("mapping_record") or {}),
                            "admin_writeback": resolve_admin_writeback_placeholder(
                                p,
                                hs,
                                at=_now_iso(),
                                resolution=resolution,
                                item_nos=ids,
                            ),
                        },
                    },
                )
                try:
                    from app.services.workflow.hooks import trace_admin_writeback

                    wb_skip = (updated_skip or {}).get("mapping_record", {}).get("admin_writeback") or {}
                    trace_admin_writeback(ids, product_id=pid, wb=wb_skip)
                except Exception:
                    pass
                return

            updated = update_product(
                pid,
                lambda p: attach_admin_writeback(p, hs, resolution=resolution, item_nos=item_nos),
            )
            wb = (updated or {}).get("mapping_record", {}).get("admin_writeback") or {}
            try:
                from app.services.workflow.hooks import trace_admin_writeback

                lines_for_trace = item_nos if item_nos is not None else collect_item_nos(updated or product)
                trace_admin_writeback(lines_for_trace, product_id=pid, wb=wb)
            except Exception:
                pass
            if wb.get("status") != "ok" or wb.get("skipped_duplicate"):
                return

            lines = item_nos if item_nos is not None else collect_item_nos(updated or product)
            snapshot_ok = True
            pipeline_errors: list[str] = []
            try:
                from app.services.orders import line_cache, order_line_sync

                order_line_sync.refresh_ord_lines(lines)
                line_cache.apply_category_overlay_to_lines(lines, hs, source="category_writeback")
            except Exception as exc:
                snapshot_ok = False
                pipeline_errors.append(f"snapshot: {exc}")
                _log.warning("post writeback snapshot sync failed pid=%s: %s", pid, exc)
            try:
                from app.services.orders.procurement_pipeline import resume_pipeline

                for line_no in lines:
                    key = str(line_no or "").strip()
                    if not key:
                        continue
                    try:
                        resume_pipeline(key, operator="category_writeback")
                    except Exception as exc:
                        pipeline_errors.append(f"{key}: {exc}")
            except Exception as exc:
                pipeline_errors.append(f"pipeline: {exc}")
                _log.warning("resume_pipeline after category writeback failed pid=%s: %s", pid, exc)

            if not snapshot_ok or pipeline_errors:

                def _mark_post_sync(p: dict[str, Any]) -> dict[str, Any]:
                    mapping = dict(p.get("mapping_record") or {})
                    wb_row = dict(mapping.get("admin_writeback") or {})
                    wb_row["post_sync"] = {
                        "snapshot_refresh": "ok" if snapshot_ok else "failed",
                        "pipeline_resume": "ok" if not pipeline_errors else "partial",
                        "errors": pipeline_errors[:5] or None,
                        "at": _now_iso(),
                    }
                    mapping["admin_writeback"] = wb_row
                    p["mapping_record"] = mapping
                    return p

                update_product(pid, _mark_post_sync)
        finally:
            lock.release()

    threading.Thread(
        target=_run,
        daemon=True,
        name=f"admin-wb-{pid[:12]}",
    ).start()

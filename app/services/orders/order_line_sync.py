"""订单分批同步：增量拉最近页 + 后台回填历史页。"""

from __future__ import annotations

from typing import Any, Optional

from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import time

from app.core.config import get_settings
from app.services.orders import line_cache
from app.services.orders import service as order_service
from app.services.orders.queue_filters import QUEUE_GOODS_STATUS_BUCKETS

DEFAULT_PAGE_SIZE = 100
INCREMENTAL_PAGES = 3
BACKFILL_QUEUES = line_cache.SYNC_QUEUES
# Admin 待采购桶 pageSize>~170 会返回 fail
PENDING_PROCUREMENT_MAX_PAGE = 100
# 本地仍标「已订购」的单，按子单号 live 回刷条数（捕获已发货/已签收跃迁）
STALE_ORDERED_REVALIDATE_LIMIT = 40
STALE_ORDERED_HOURS = 48
_ORDERED_STAT = 22


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _enrich_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """与 service._enrich_rows 对齐：预加载索引，同步路径不调备注 LLM。"""
    from app.services.orders import disposition_store
    from app.services.orders.order_note_classify import enrich_row_note_fields
    from app.services.orders.order_sku_check import enrich_row_sku_fields
    from app.services.orders.pipeline_store import (
        _pipeline_state_map,
        enrich_row_pipeline_fields,
    )
    from app.services.orders.product_category_enrich import enrich_row_mapped_category
    from app.services.orders.purchase_cost import enrich_row_purchase_cost_fields
    from app.services.products.store import build_ord_line_product_index

    if not items:
        return []

    states = _pipeline_state_map()
    product_index = build_ord_line_product_index()
    overrides = disposition_store.load_all_overrides()

    out: list[dict[str, Any]] = []
    for row in items:
        if not isinstance(row, dict):
            continue
        base = dict(row)
        ord_line_no = str(base.get("ord_line_no") or "").strip()
        if ord_line_no:
            override = overrides.get(ord_line_no)
            if override:
                for key, value in override.items():
                    if key in ("queue_override", "passed_at", "action_key", "signal_type"):
                        continue
                    base[key] = value
        enriched = enrich_row_purchase_cost_fields(
            enrich_row_sku_fields(enrich_row_note_fields(base, allow_llm=False))
        )
        enriched = enrich_row_mapped_category(enriched, product_index=product_index)
        enriched = enrich_row_pipeline_fields(enriched, states=states)
        out.append(enriched)
    return out


def _fetch_page(queue: Optional[str], page: int, page_size: int) -> dict[str, Any]:
    settings = get_settings()
    token = (settings.tangbuy_admin_token or "").strip()
    if not token or token == "your-admin-bearer-token":
        return {
            "items": [],
            "total": 0,
            "error": "未配置 TANGBUY_ADMIN_TOKEN（.env.local）",
        }

    if queue == "pending_procurement":
        cap = min(page_size, PENDING_PROCUREMENT_MAX_PAGE)
        try:
            rows, total = order_service.fetch_pending_procurement_bucket(
                filter_index=0,
                page=page,
                page_size=cap,
                storage_no=settings.tangbuy_admin_storage_no,
                event_type_pending=settings.tangbuy_admin_event_type_pending,
            )
            # 增量：额外拉 stat=0 / 54 桶第一页（并发）
            if page == 1:
                merged: dict[str, dict[str, Any]] = {str(r.get("ord_line_no") or ""): r for r in rows if r.get("ord_line_no")}
                n_extra = order_service.pending_procurement_filter_count() - 1
                if n_extra > 0:
                    with ThreadPoolExecutor(max_workers=n_extra) as pool:
                        extra_futures = {
                            pool.submit(
                                order_service.fetch_pending_procurement_bucket,
                                filter_index=fi,
                                page=1,
                                page_size=page_size,
                            ): fi
                            for fi in range(1, n_extra + 1)
                        }
                        for fut in as_completed(extra_futures):
                            try:
                                extra, _ = fut.result()
                                for row in extra:
                                    key = str(row.get("ord_line_no") or "")
                                    if key:
                                        merged[key] = row
                            except Exception:
                                pass
                rows = sorted(merged.values(), key=lambda r: str(r.get("pay_time") or ""), reverse=True)
            return {"items": _enrich_items(rows), "total": total}
        except Exception as exc:
            return {"items": [], "total": 0, "error": str(exc)}

    # shipped / in_warehouse / …：首页合并多 goodsStatus 桶，避免已签收等状态漏同步
    buckets = QUEUE_GOODS_STATUS_BUCKETS.get(queue or "") if queue else None
    if buckets and page == 1 and len(buckets) > 1:
        try:
            merged_rows: dict[str, dict[str, Any]] = {}
            total_hint = 0
            with ThreadPoolExecutor(max_workers=min(len(buckets), 6)) as pool:
                futures = {
                    pool.submit(
                        order_service.fetch_queue_status_bucket,
                        queue=queue,
                        goods_status=gs,
                        page=1,
                        page_size=page_size,
                    ): gs
                    for gs in buckets
                }
                for fut in as_completed(futures):
                    try:
                        rows, total = fut.result()
                        total_hint = max(total_hint, int(total or 0))
                        for row in rows:
                            key = str(row.get("ord_line_no") or "")
                            if key:
                                merged_rows[key] = row
                    except Exception:
                        pass
            rows = sorted(
                merged_rows.values(),
                key=lambda r: str(r.get("pay_time") or r.get("pur_time") or ""),
                reverse=True,
            )
            return {"items": _enrich_items(rows), "total": max(total_hint, len(rows))}
        except Exception as exc:
            return {"items": [], "total": 0, "error": str(exc)}

    q = None if queue == "all" else queue
    res = order_service.list_ord_lines(queue=q, page=page, page_size=page_size)
    if res.get("error"):
        return res
    return res


def _fetch_admin_page_with_retry(
    fetcher,
    *,
    label: str,
    enrich: bool = True,
) -> dict[str, Any]:
    last_err: Optional[str] = None
    for attempt in range(4):
        try:
            rows, total = fetcher()
            items = _enrich_items(rows) if enrich else rows
            return {"items": items, "total": total}
        except Exception as exc:
            last_err = str(exc)
            time.sleep(0.35 * (attempt + 1))
    return {"items": [], "total": 0, "error": last_err or "fetch failed", "skip_page": True, "label": label}


def _fetch_backfill_page(queue: str, page: int, page_size: int, filter_index: int) -> dict[str, Any]:
    settings = get_settings()
    if queue == "pending_procurement":
        cap = min(page_size, PENDING_PROCUREMENT_MAX_PAGE)
        return _fetch_admin_page_with_retry(
            lambda: order_service.fetch_pending_procurement_bucket(
                filter_index=filter_index,
                page=page,
                page_size=cap,
                storage_no=settings.tangbuy_admin_storage_no,
                event_type_pending=settings.tangbuy_admin_event_type_pending,
            ),
            label=f"{queue} f{filter_index} p{page}",
        )

    def _fetch_standard() -> tuple[list[dict[str, Any]], int]:
        q = None if queue == "all" else queue
        res = order_service.list_ord_lines(queue=q, page=page, page_size=page_size)
        if res.get("error"):
            raise RuntimeError(str(res["error"]))
        items = res.get("items") or []
        return items, int(res.get("total") or len(items))

    fetched = _fetch_admin_page_with_retry(_fetch_standard, label=f"{queue} p{page}", enrich=False)
    if fetched.get("items") is not None and not fetched.get("error"):
        return fetched
    return fetched


def _fetch_queue_pages(
    q: str,
    page_size: int,
    pages: int,
) -> tuple[dict[str, int], list[str]]:
    """拉单个队列的最近若干页，合并进本地快照。返回 (stats, errors)。"""
    stats = {"added": 0, "updated": 0, "unchanged": 0, "scanned": 0, "pages": 0}
    errors: list[str] = []
    for page in range(1, max(1, pages) + 1):
        res = _fetch_page(q, page, page_size)
        if res.get("error"):
            errors.append(f"{q}: {res['error']}")
            break
        items = res.get("items") or []
        merge_stats = line_cache.merge_lines(items)
        for key in ("added", "updated", "unchanged"):
            stats[key] += int(merge_stats.get(key) or 0)
        stats["scanned"] += len(items)
        stats["pages"] += 1
        if len(items) < page_size:
            break
    return stats, errors


def _parse_iso_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def revalidate_stale_ordered_lines(
    *,
    limit: int = STALE_ORDERED_REVALIDATE_LIMIT,
    older_than_hours: float = STALE_ORDERED_HOURS,
) -> dict[str, int]:
    """本地仍为已订购(22)且超时的子单，按 itemNo 直连 Admin 回刷。

    覆盖「已从 22 跃迁到发货/签收，但未出现在本轮增量首页」的漏同步。
    """
    now = datetime.now(timezone.utc)
    candidates: list[tuple[float, str]] = []
    for row in line_cache.list_cached_lines(queue="ordered"):
        try:
            stat = int(row.get("ord_line_stat")) if row.get("ord_line_stat") is not None else None
        except (TypeError, ValueError):
            stat = None
        if stat != _ORDERED_STAT:
            continue
        key = str(row.get("ord_line_no") or "").strip()
        if not key:
            continue
        ref = _parse_iso_dt(row.get("pur_time")) or _parse_iso_dt(row.get("pay_time"))
        if not ref:
            continue
        age_h = (now - ref).total_seconds() / 3600
        if age_h < older_than_hours:
            continue
        candidates.append((age_h, key))

    candidates.sort(key=lambda x: -x[0])
    keys = [k for _, k in candidates[: max(0, limit)]]
    if not keys:
        return {"candidates": 0, "scanned": 0, "added": 0, "updated": 0, "unchanged": 0}

    totals = {"candidates": len(candidates), "scanned": 0, "added": 0, "updated": 0, "unchanged": 0}

    def _one(key: str) -> dict[str, int]:
        return refresh_ord_lines([key])

    with ThreadPoolExecutor(max_workers=min(8, len(keys))) as pool:
        for fut in as_completed({pool.submit(_one, k): k for k in keys}):
            try:
                stats = fut.result()
                for k in ("scanned", "added", "updated", "unchanged"):
                    totals[k] += int(stats.get(k) or 0)
            except Exception:
                pass
    return totals


def sync_orders_incremental(
    *,
    queue: Optional[str] = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    pages: int = INCREMENTAL_PAGES,
    pipeline_inline: bool = False,
) -> dict[str, Any]:
    """拉各队列最近若干页，合并进本地快照。多队列并发拉取。"""
    targets = [queue] if queue and queue != "all" else list(line_cache.SYNC_QUEUES)
    totals = {"added": 0, "updated": 0, "unchanged": 0, "scanned": 0, "pages": 0}
    errors: list[str] = []

    if len(targets) <= 1:
        stats, errs = _fetch_queue_pages(targets[0] if targets else "all", page_size, pages)
        for key in ("added", "updated", "unchanged", "scanned", "pages"):
            totals[key] += stats[key]
        errors.extend(errs)
    else:
        max_workers = min(len(targets), 8)
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(_fetch_queue_pages, q, page_size, pages): q
                for q in targets
            }
            for fut in as_completed(futures):
                q = futures[fut]
                try:
                    stats, errs = fut.result()
                    for key in ("added", "updated", "unchanged", "scanned", "pages"):
                        totals[key] += stats[key]
                    errors.extend(errs)
                except Exception as exc:
                    errors.append(f"{q}: {exc}")

    stale_stats: Optional[dict[str, int]] = None
    # 全量增量时：回刷本地超时仍「已订购」的子单，吃掉队列首页漏网的状态跃迁
    if not queue or queue in ("all", "ordered"):
        try:
            stale_stats = revalidate_stale_ordered_lines()
            for key in ("added", "updated", "unchanged", "scanned"):
                totals[key] = int(totals.get(key) or 0) + int(stale_stats.get(key) or 0)
        except Exception as exc:
            errors.append(f"stale_ordered: {exc}")

    state = line_cache.load_sync_state()
    state["last_incremental_at"] = _now_iso()
    state["cached_total"] = len(line_cache.load_all_lines())
    line_cache.save_sync_state(state)

    return {
        "ok": not errors,
        "mode": "incremental",
        "stats": totals,
        "stale_ordered": stale_stats,
        "cache_total": state["cached_total"],
        "errors": errors or None,
        "items": line_cache.list_cached_lines(queue=queue),
        "pipeline": _maybe_run_pipeline_after_sync(totals, inline=pipeline_inline),
    }


def refresh_ord_lines(ord_line_nos: list[str]) -> dict[str, int]:
    """按子单号从 Admin 刷新并合并进本地快照。"""
    totals = {"added": 0, "updated": 0, "unchanged": 0, "scanned": 0}
    for key in ord_line_nos:
        key = str(key).strip()
        if not key:
            continue
        result = order_service.list_ord_lines(ord_line_no=key, page=1, page_size=1)
        items = result.get("items") or []
        totals["scanned"] += len(items)
        stats = line_cache.merge_lines(items)
        for k in ("added", "updated", "unchanged"):
            totals[k] += int(stats.get(k) or 0)
    return totals


def _maybe_run_pipeline_after_sync(
    totals: dict[str, int],
    *,
    inline: bool = False,
) -> Optional[dict[str, Any]]:
    added = int(totals.get("added") or 0)
    updated = int(totals.get("updated") or 0)
    if added <= 0 and updated <= 0:
        return None
    from app.services.orders.procurement_pipeline import run_pipeline_batch

    if inline:
        return run_pipeline_batch(trigger="sync")

    from app.services.background_jobs import create_job, run_job

    job_id = create_job("pipeline", label="post-sync")
    run_job(job_id, lambda: run_pipeline_batch(trigger="sync"))
    return {"job_id": job_id, "status": "scheduled"}


def sync_orders_backfill_batch(
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
    batches: int = 1,
    pipeline_inline: bool = False,
) -> dict[str, Any]:
    """继续历史回填，按队列 + 待采购子桶分页。"""
    state = line_cache.load_sync_state()
    if state.get("backfill_complete") and not state.get("backfill_done_queues"):
        state["backfill_complete"] = False
        state["backfill_page"] = 1
        state["backfill_queue_index"] = 0
        state["backfill_filter_index"] = 0

    queues = list(BACKFILL_QUEUES)
    qi = int(state.get("backfill_queue_index") or 0) % len(queues)
    page = max(1, int(state.get("backfill_page") or 1))
    filter_index = int(state.get("backfill_filter_index") or 0)
    done_queues = set(state.get("backfill_done_queues") or [])

    totals = {"added": 0, "updated": 0, "unchanged": 0, "scanned": 0, "pages": 0}
    errors: list[str] = []
    done_batches = 0
    current_queue = queues[qi]

    while done_batches < max(1, batches):
        res = _fetch_backfill_page(current_queue, page, page_size, filter_index)
        if res.get("error"):
            if res.get("skip_page"):
                errors.append(f"{res.get('label') or current_queue}: {res['error']} (skipped)")
                page += 1
                done_batches += 1
                if current_queue == "pending_procurement":
                    admin_total = int(res.get("total") or 0)
                    cap = min(page_size, PENDING_PROCUREMENT_MAX_PAGE)
                    if admin_total and page * cap > admin_total:
                        n_filters = order_service.pending_procurement_filter_count()
                        if filter_index + 1 < n_filters:
                            filter_index += 1
                            page = 1
                        else:
                            done_queues.add(current_queue)
                            qi = (qi + 1) % len(queues)
                            page = 1
                            filter_index = 0
                elif page > 50:
                    done_queues.add(current_queue)
                    qi = (qi + 1) % len(queues)
                    page = 1
                    filter_index = 0
                continue
            errors.append(f"{current_queue} f{filter_index} p{page}: {res['error']}")
            break

        items = res.get("items") or []
        stats = line_cache.merge_lines(items)
        for key in ("added", "updated", "unchanged"):
            totals[key] += int(stats.get(key) or 0)
        totals["scanned"] += len(items)
        totals["pages"] += 1
        done_batches += 1

        admin_total = int(res.get("total") or 0)
        page_exhausted = len(items) < page_size or page * page_size >= admin_total

        if current_queue == "pending_procurement":
            n_filters = order_service.pending_procurement_filter_count()
            if page_exhausted:
                if filter_index + 1 < n_filters:
                    filter_index += 1
                    page = 1
                else:
                    done_queues.add(current_queue)
                    qi = (qi + 1) % len(queues)
                    page = 1
                    filter_index = 0
            else:
                page += 1
        elif page_exhausted:
            done_queues.add(current_queue)
            qi = (qi + 1) % len(queues)
            page = 1
            filter_index = 0
        else:
            page += 1

        state["backfill_complete"] = len(done_queues) >= len(queues)
        current_queue = queues[qi]
        if state.get("backfill_complete"):
            break

    state["backfill_queue_index"] = qi
    state["backfill_page"] = page
    state["backfill_filter_index"] = filter_index
    state["backfill_done_queues"] = sorted(done_queues)
    state["last_backfill_at"] = _now_iso()
    state["cached_total"] = len(line_cache.load_all_lines())
    line_cache.save_sync_state(state)

    return {
        "ok": not errors,
        "mode": "backfill",
        "stats": totals,
        "cache_total": state["cached_total"],
        "backfill": {
            "complete": bool(state.get("backfill_complete")),
            "queue": current_queue,
            "next_page": page,
            "filter_index": filter_index,
            "done_queues": state.get("backfill_done_queues"),
        },
        "errors": errors or None,
        "pipeline": _maybe_run_pipeline_after_sync(totals, inline=pipeline_inline),
    }


def run_backfill_until_idle(*, max_batches: int = 50, page_size: int = DEFAULT_PAGE_SIZE) -> dict[str, Any]:
    """连续回填直到完成或达到批次上限。"""
    last: dict[str, Any] = {}
    for _ in range(max(1, max_batches)):
        last = sync_orders_backfill_batch(page_size=page_size, batches=3)
        if last.get("backfill", {}).get("complete"):
            break
        if not last.get("ok"):
            break
        if int((last.get("stats") or {}).get("scanned") or 0) <= 0:
            break
    return last


# ---------------------------------------------------------------------------
# Full scan — 全队列全页扫描，保证完整性
# ---------------------------------------------------------------------------

FULL_SCAN_MAX_PAGES = 500
_FULL_SCAN_MAX_CONSECUTIVE_ERRORS = 5


def _fetch_queue_full(
    q: str,
    page_size: int,
    max_pages: int = FULL_SCAN_MAX_PAGES,
) -> tuple[dict[str, int], list[str], int]:
    """扫描单个队列的所有页面（含 pending_procurement 全部子桶）。

    与增量同步不同，此函数遍历所有页直到数据耗尽，不限制页数。
    返回 (stats, errors, admin_total_hint)。
    """
    stats = {"added": 0, "updated": 0, "unchanged": 0, "scanned": 0, "pages": 0}
    errors: list[str] = []
    admin_total_hint = 0
    consecutive_errors = 0

    if q == "pending_procurement":
        n_filters = order_service.pending_procurement_filter_count()
        cap = min(page_size, PENDING_PROCUREMENT_MAX_PAGE)
        for fi in range(n_filters):
            consecutive_errors = 0
            for page in range(1, max_pages + 1):
                res = _fetch_backfill_page(q, page, cap, fi)
                if res.get("error"):
                    errors.append(f"{q} f{fi} p{page}: {res['error']}")
                    if res.get("skip_page"):
                        consecutive_errors += 1
                        if consecutive_errors >= _FULL_SCAN_MAX_CONSECUTIVE_ERRORS:
                            errors.append(f"{q} f{fi}: 连续 {consecutive_errors} 页失败，跳过该子桶")
                            break
                        continue
                    break
                consecutive_errors = 0
                items = res.get("items") or []
                admin_total_hint = max(admin_total_hint, int(res.get("total") or 0))
                merge_stats = line_cache.merge_lines(items)
                for key in ("added", "updated", "unchanged"):
                    stats[key] += int(merge_stats.get(key) or 0)
                stats["scanned"] += len(items)
                stats["pages"] += 1
                if len(items) < cap:
                    break
    else:
        for page in range(1, max_pages + 1):
            res = _fetch_backfill_page(q, page, page_size, 0)
            if res.get("error"):
                errors.append(f"{q} p{page}: {res['error']}")
                if res.get("skip_page"):
                    consecutive_errors += 1
                    if consecutive_errors >= _FULL_SCAN_MAX_CONSECUTIVE_ERRORS:
                        errors.append(f"{q}: 连续 {consecutive_errors} 页失败，停止该队列")
                        break
                    continue
                break
            consecutive_errors = 0
            items = res.get("items") or []
            admin_total_hint = max(admin_total_hint, int(res.get("total") or 0))
            merge_stats = line_cache.merge_lines(items)
            for key in ("added", "updated", "unchanged"):
                stats[key] += int(merge_stats.get(key) or 0)
            stats["scanned"] += len(items)
            stats["pages"] += 1
            if len(items) < page_size:
                break

    return stats, errors, admin_total_hint


def sync_orders_full(
    *,
    queue: Optional[str] = None,
    page_size: int = 200,
    max_pages: int = FULL_SCAN_MAX_PAGES,
    pipeline_inline: bool = False,
) -> dict[str, Any]:
    """全队列全页扫描：遍历所有队列的所有页面，保证缓存完整性。

    与增量同步的区别：
    - 增量同步只拉每队列最近 N 页（默认 3 页）
    - 全量扫描遍历所有页直到数据耗尽
    - pending_procurement 的全部子桶都会被完整扫描

    扫描完成后自动标记 backfill 为 complete。
    """
    targets = [queue] if queue and queue != "all" else list(line_cache.SYNC_QUEUES)
    totals = {"added": 0, "updated": 0, "unchanged": 0, "scanned": 0, "pages": 0}
    errors: list[str] = []
    per_queue: dict[str, dict[str, Any]] = {}

    if len(targets) <= 1:
        q = targets[0] if targets else "all"
        stats, errs, admin_total = _fetch_queue_full(q, page_size, max_pages)
        for key in ("added", "updated", "unchanged", "scanned", "pages"):
            totals[key] += stats[key]
        errors.extend(errs)
        per_queue[q] = {**stats, "admin_total": admin_total}
    else:
        max_workers = min(len(targets), 8)
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(_fetch_queue_full, q, page_size, max_pages): q
                for q in targets
            }
            for fut in as_completed(futures):
                q = futures[fut]
                try:
                    stats, errs, admin_total = fut.result()
                    for key in ("added", "updated", "unchanged", "scanned", "pages"):
                        totals[key] += stats[key]
                    errors.extend(errs)
                    per_queue[q] = {**stats, "admin_total": admin_total}
                except Exception as exc:
                    errors.append(f"{q}: {exc}")
                    per_queue[q] = {"error": str(exc)}

    # 全量扫描完成后更新状态
    state = line_cache.load_sync_state()
    state["last_incremental_at"] = _now_iso()
    state["last_backfill_at"] = _now_iso()
    state["cached_total"] = len(line_cache.load_all_lines())
    # 全量扫描 → 标记 backfill 为完成
    if not queue or queue == "all":
        state["backfill_complete"] = True
        state["backfill_done_queues"] = list(line_cache.SYNC_QUEUES)
        state["backfill_queue_index"] = 0
        state["backfill_page"] = 1
        state["backfill_filter_index"] = 0
    else:
        # 单队列全量扫描 → 标记该队列 done
        done_queues = set(state.get("backfill_done_queues") or [])
        done_queues.add(queue)
        state["backfill_done_queues"] = sorted(done_queues)
        if len(done_queues) >= len(line_cache.SYNC_QUEUES):
            state["backfill_complete"] = True
    line_cache.save_sync_state(state)

    return {
        "ok": not errors,
        "mode": "full",
        "stats": totals,
        "cache_total": state["cached_total"],
        "per_queue": per_queue,
        "errors": errors or None,
        "items": line_cache.list_cached_lines(queue=queue),
        "pipeline": _maybe_run_pipeline_after_sync(totals, inline=pipeline_inline),
    }

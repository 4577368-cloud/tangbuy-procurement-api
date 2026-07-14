"""1688 平台下单（stat=54 → platform/order/create）。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from app.config.business_config import normalize_business_config
from app.config.store import get_business_config
from app.integrations.tangbuy_admin.alibaba_order_api import (
    create_platform_order,
    wait_generate_order_list,
)
from app.integrations.tangbuy_admin.client import TangbuyAdminError
from app.services.orders import disposition_store, place_order_store
from app.services.orders.procurement_release import _is_1688_channel
from app.services.orders.queue_filters import resolve_order_queue
from app.services.orders.service import get_ord_line

PlaceOrderTrigger = Literal["auto_place", "manual", "disposition"]

PLACED_LINE_STATS = frozenset({55, 22})


class ProcurementPlaceOrderError(Exception):
    def __init__(self, message: str, *, code: str = "place_order_failed") -> None:
        super().__init__(message)
        self.code = code


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _int_stat(row: dict[str, Any]) -> Optional[int]:
    raw = row.get("ord_line_stat")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def is_1688_place_order_eligible(row: dict[str, Any]) -> tuple[bool, str]:
    stat = _int_stat(row)
    if stat in PLACED_LINE_STATS:
        return False, "already_placed"
    if stat != 54:
        label = row.get("ord_line_stat_nm") or f"状态码 {stat}"
        return False, f"invalid_stage:{label}"
    if not _is_1688_channel(row):
        return False, "channel_not_1688"
    if not str(row.get("ord_no") or "").strip():
        return False, "missing_ord_no"
    from app.services.orders.procurement_release import evaluate_prepare_stage

    prep = evaluate_prepare_stage(row)
    if not prep.get("all_clear"):
        note_hits = [b for b in (prep.get("blockers") or []) if b.get("key") == "NOTE_BLOCK"]
        if note_hits:
            return False, "note_blocked"
        return False, "prepare_blocked"
    return True, "ok"


def _extract_wait_rows(admin_response: Any) -> list[dict[str, Any]]:
    """Admin data 可能是 {rows,total}，少数接口再包一层 data。"""
    if not isinstance(admin_response, dict):
        return []
    nested = admin_response.get("data")
    if isinstance(nested, dict):
        rows = nested.get("rows")
        if isinstance(rows, list):
            return rows
    rows = admin_response.get("rows")
    return rows if isinstance(rows, list) else []


def build_order_targets(
    wait_rows: list[dict[str, Any]],
    item_nos: set[str],
) -> list[dict[str, Any]]:
    """从 Admin 待生成列表构建 orderTargets，附带 store_id 供分组。"""
    targets: list[dict[str, Any]] = []
    for row in wait_rows:
        if not isinstance(row, dict):
            continue
        order_no = str(row.get("orderNo") or "").strip()
        store_id = str(row.get("storeId") or "").strip()
        if not order_no:
            continue
        matched: list[str] = []
        for item in row.get("items") or []:
            if not isinstance(item, dict):
                continue
            item_no = str(item.get("itemNo") or "").strip()
            if item_no and item_no in item_nos:
                matched.append(item_no)
        if matched:
            targets.append(
                {
                    "orderNo": order_no,
                    "itemNos": matched,
                    "remark": "",
                    "store_id": store_id,
                }
            )
    return targets


def group_targets_by_store(
    targets: list[dict[str, Any]],
    *,
    merge_same_store: bool,
) -> list[list[dict[str, Any]]]:
    if not targets:
        return []
    if not merge_same_store:
        return [[target] for target in targets]
    by_store: dict[str, list[dict[str, Any]]] = {}
    for target in targets:
        store_key = str(target.get("store_id") or "__unknown__")
        by_store.setdefault(store_key, []).append(target)
    return list(by_store.values())


def list_wait_generate_orders(
    *,
    page_num: int = 1,
    page_size: int = 200,
    order_no: Optional[str] = None,
) -> dict[str, Any]:
    try:
        admin_response = wait_generate_order_list(
            page_num=page_num,
            page_size=page_size,
            order_no=order_no,
        )
    except TangbuyAdminError as exc:
        raise ProcurementPlaceOrderError(f"获取待生成列表失败：{exc}", code="admin_read_failed") from exc
    rows = _extract_wait_rows(admin_response)
    return {
        "rows": rows,
        "total": len(rows),
        "admin_response": admin_response,
    }


def _refresh_ord_lines(ord_line_nos: list[str]) -> dict[str, dict[str, Any]]:
    from app.services.orders import line_cache, order_line_sync

    keys = [str(k).strip() for k in ord_line_nos if str(k).strip()]
    if keys:
        order_line_sync.refresh_ord_lines(keys)
    cache = line_cache.load_all_lines()
    return {key: cache.get(key) or get_ord_line(key) or {} for key in keys}


def _persist_place_order(
    *,
    ord_line_nos: list[str],
    store_id: str,
    order_targets: list[dict[str, Any]],
    result: str,
    trigger: PlaceOrderTrigger,
    operator: Optional[str],
    admin_response: Any = None,
    reviewer_note: Optional[str] = None,
    auto_confirmed: bool = False,
    rows_by_line: Optional[dict[str, dict[str, Any]]] = None,
) -> dict[str, Any]:
    primary = ord_line_nos[0] if ord_line_nos else ""
    row = (rows_by_line or {}).get(primary) or get_ord_line(primary) or {}
    place_id = f"po-{primary}-{int(datetime.now(timezone.utc).timestamp())}"
    record = {
        "release_id": place_id,
        "ord_line_no": primary,
        "ord_line_nos": ord_line_nos,
        "order_id": primary,
        "external_order_no": row.get("ord_no") or "",
        "product_title": row.get("item_nm") or row.get("item_nm_cn") or "",
        "release_type": "1688_place_order",
        "agent_label": "1688 下单 Agent",
        "stage_before": "pending_procurement",
        "stage_after": "pending_payment",
        "released_at": row.get("pay_time") or _now_iso(),
        "conditions": [
            {
                "key": "wait_list",
                "label": "Admin 待生成列表",
                "passed": result in ("confirmed", "auto_confirmed", "already_submitted"),
                "detail": store_id or "—",
            }
        ],
        "summary": f"1688 平台下单 · store {store_id or '—'}",
        "review_status": "confirmed" if result in ("confirmed", "auto_confirmed", "already_submitted") else "pending",
        "auto_confirmed": auto_confirmed,
        "result": result,
        "trigger": trigger,
        "operator": operator,
        "store_id": store_id,
        "order_targets": [
            {"orderNo": t["orderNo"], "itemNos": t["itemNos"], "remark": t.get("remark", "")}
            for t in order_targets
        ],
        "admin_response": admin_response,
        "reviewer_note": reviewer_note,
        "ord_line_stat_before": 54,
    }
    return place_order_store.append_place_order(record)


def submit_1688_place_order(
    ord_line_nos: list[str],
    *,
    operator: Optional[str] = None,
    trigger: PlaceOrderTrigger = "manual",
    merge_same_store: bool = True,
) -> dict[str, Any]:
    keys = [str(n).strip() for n in ord_line_nos if str(n).strip()]
    if not keys:
        raise ProcurementPlaceOrderError("缺少子单号 ord_line_nos", code="missing_ord_line_no")

    rows_by_line: dict[str, dict[str, Any]] = {}
    order_nos: set[str] = set()
    for key in keys:
        row = get_ord_line(key)
        if not row:
            raise ProcurementPlaceOrderError(f"子单不存在：{key}", code="not_found")
        queue = resolve_order_queue(row) or "pending_procurement"
        if queue != "pending_procurement":
            raise ProcurementPlaceOrderError("仅待采购队列子单可下单", code="invalid_queue")
        eligible, code = is_1688_place_order_eligible(row)
        if not eligible and code != "already_placed":
            raise ProcurementPlaceOrderError(
                f"子单 {key} 不满足 1688 下单条件（{code}）",
                code=code.split(":")[0],
            )
        if place_order_store.has_successful_place_order(key) or _int_stat(row) in PLACED_LINE_STATS:
            return {
                "ok": True,
                "code": "already_submitted",
                "ord_line_nos": keys,
                "ord_line_stat_after": _int_stat(row),
                "batches": [],
            }
        rows_by_line[key] = row
        ord_no = str(row.get("ord_no") or "").strip()
        if ord_no:
            order_nos.add(ord_no)

    item_nos_set = set(keys)
    wait_rows: list[dict[str, Any]] = []
    if len(order_nos) == 1:
        wait_result = list_wait_generate_orders(order_no=next(iter(order_nos)))
        wait_rows = wait_result.get("rows") or []
    if not wait_rows:
        wait_result = list_wait_generate_orders(page_size=200)
        wait_rows = wait_result.get("rows") or []

    targets = build_order_targets(wait_rows, item_nos_set)
    if not targets:
        raise ProcurementPlaceOrderError(
            "子单未出现在 Admin 待生成列表，请稍后重试",
            code="not_in_wait_list",
        )

    missing = item_nos_set - {item for t in targets for item in t["itemNos"]}
    if missing:
        raise ProcurementPlaceOrderError(
            f"部分子单未在待生成列表：{', '.join(sorted(missing))}",
            code="not_in_wait_list",
        )

    batches = group_targets_by_store(targets, merge_same_store=merge_same_store)
    submitted_batches: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    for batch in batches:
        batch_item_nos = [item for t in batch for item in t["itemNos"]]
        store_id = str(batch[0].get("store_id") or "")
        api_targets = [
            {"orderNo": t["orderNo"], "itemNos": t["itemNos"], "remark": t.get("remark", "")}
            for t in batch
        ]
        try:
            admin_response = create_platform_order(api_targets)
            admin_result = "ok"
        except TangbuyAdminError as exc:
            _persist_place_order(
                ord_line_nos=batch_item_nos,
                store_id=store_id,
                order_targets=api_targets,
                result="admin_failed",
                trigger=trigger,
                operator=operator,
                reviewer_note=str(exc),
                rows_by_line=rows_by_line,
            )
            for item_no in batch_item_nos:
                disposition_store.append_audit(
                    {
                        "ord_line_no": item_no,
                        "action_key": "place_1688_order",
                        "action_label": "1688 下单",
                        "stage_before": "pending_procurement",
                        "stage_after": "pending_procurement",
                        "admin_write": "failed",
                        "error": str(exc),
                        "operator": operator,
                        "trigger": trigger,
                        "at": _now_iso(),
                    }
                )
            errors.append({"store_id": store_id, "error": str(exc)})
            continue

        refreshed = _refresh_ord_lines(batch_item_nos)
        auto_confirmed = trigger == "auto_place"
        record = _persist_place_order(
            ord_line_nos=batch_item_nos,
            store_id=store_id,
            order_targets=api_targets,
            result="auto_confirmed" if auto_confirmed else "confirmed",
            trigger=trigger,
            operator=operator,
            admin_response=admin_response,
            auto_confirmed=auto_confirmed,
            rows_by_line={**rows_by_line, **refreshed},
        )
        for item_no in batch_item_nos:
            refreshed_row = refreshed.get(item_no) or rows_by_line.get(item_no) or {}
            disposition_store.append_audit(
                {
                    "ord_line_no": item_no,
                    "ord_no": refreshed_row.get("ord_no"),
                    "action_key": "place_1688_order",
                    "action_label": "1688 下单",
                    "stage_before": "pending_procurement",
                    "stage_after": resolve_order_queue(refreshed_row) or "pending_payment",
                    "admin_write": admin_result,
                    "operator": operator,
                    "trigger": trigger,
                    "auto_confirmed": auto_confirmed,
                    "ord_line_stat_before": 54,
                    "ord_line_stat_after": _int_stat(refreshed_row),
                    "store_id": store_id,
                    "at": _now_iso(),
                }
            )
        submitted_batches.append(
            {
                "store_id": store_id,
                "ord_line_nos": batch_item_nos,
                "order_targets": api_targets,
                "admin_write": admin_result,
                "release": record,
            }
        )

    if errors and not submitted_batches:
        raise ProcurementPlaceOrderError(
            errors[0]["error"],
            code="admin_write_failed",
        )

    final_refreshed = _refresh_ord_lines(keys)
    try:
        from app.services.orders.platform_order_sync import sync_platform_orders_for_lines

        sync_platform_orders_for_lines(keys, rows_by_line={**rows_by_line, **final_refreshed})
    except Exception:
        pass
    primary_stat = _int_stat(final_refreshed.get(keys[0]) or rows_by_line.get(keys[0], {}))
    return {
        "ok": True,
        "code": "submitted" if submitted_batches else "partial_failed",
        "ord_line_nos": keys,
        "ord_line_stat_after": primary_stat,
        "batches": submitted_batches,
        "errors": errors or None,
    }


def auto_place_order_candidates(rows: list[dict[str, Any]]) -> list[str]:
    cfg = normalize_business_config(get_business_config())
    if not cfg.get("auto_1688_place_order_enabled", False):
        return []
    out: list[str] = []
    for row in rows:
        key = str(row.get("ord_line_no") or "").strip()
        if not key:
            continue
        if _int_stat(row) != 54:
            continue
        if place_order_store.has_successful_place_order(key):
            continue
        eligible, _ = is_1688_place_order_eligible(row)
        if eligible:
            out.append(key)
    return out


def run_auto_place_order_batch(ord_line_nos: list[str]) -> dict[str, Any]:
    if not ord_line_nos:
        return {"candidates": 0, "submitted": [], "skipped": [], "errors": []}
    try:
        result = submit_1688_place_order(
            ord_line_nos,
            operator="system",
            trigger="auto_place",
            merge_same_store=True,
        )
        submitted = list(ord_line_nos) if result.get("ok") else []
        return {
            "candidates": len(ord_line_nos),
            "submitted": submitted,
            "skipped": [],
            "errors": result.get("errors"),
            "batches": result.get("batches"),
        }
    except ProcurementPlaceOrderError as exc:
        return {
            "candidates": len(ord_line_nos),
            "submitted": [],
            "skipped": list(ord_line_nos),
            "errors": [{"error": str(exc), "code": exc.code}],
        }


def list_place_order_audits(*, limit: int = 200) -> list[dict[str, Any]]:
    return place_order_store.list_place_orders(limit=limit)

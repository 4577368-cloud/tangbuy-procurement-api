"""采购准入放行与 1688 预订购。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from app.config.business_config import normalize_business_config
from app.config.store import get_business_config
from app.integrations.tangbuy_admin.alibaba_order_api import alibaba_pre_purchase
from app.integrations.tangbuy_admin.client import TangbuyAdminError
from app.services.orders import disposition_store, release_store
from app.services.orders.order_sku_check import check_sku_alignment
from app.services.orders.purchase_cost import resolve_purchase_cost_basis
from app.services.orders.queue_filters import resolve_order_queue
from app.services.orders.service import get_ord_line
from app.services.products.store import find_product_for_ord_line

ReleaseReviewStatus = Literal["pending", "confirmed", "flagged"]
ReleaseTrigger = Literal["auto_release", "manual", "disposition", "pipeline"]

SUBMITTED_LINE_STATS = frozenset({54, 55, 22})
GENERIC_CATEGORIES = frozenset({"其它", "其他", "待映射", "—", "-", ""})


class ProcurementReleaseError(Exception):
    def __init__(self, message: str, *, code: str = "release_failed") -> None:
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


def _num(v: Any, fallback: float = 0.0) -> float:
    try:
        n = float(v)
        return n if n == n else fallback
    except (TypeError, ValueError):
        return fallback


def _money(v: Any, fallback: float = 0.0) -> float:
    return round(_num(v, fallback), 2)


def format_currency(amount: Any) -> str:
    return f"¥{_money(amount):.2f}"


def _resolve_customer_paid(row: dict[str, Any]) -> float:
    product = _num(row.get("pur_amt"))
    if product <= 0:
        product = _num(row.get("pur_prc")) * max(1.0, _num(row.get("ord_cnt"), 1))
    shipping = _num(row.get("post_fee"))
    payable = round(product + shipping, 2)
    raw = _num(row.get("ds_ord_amt"))
    if payable <= 0:
        return raw if raw > 0 else 0.0
    if raw <= 0:
        return payable
    if shipping > 0 and abs(raw - product) < 0.02 and abs(raw - payable) > 0.02:
        return payable
    if abs(raw - payable) < 0.02:
        return payable
    return raw if raw >= payable - 0.02 else payable


def _is_1688_channel(row: dict[str, Any]) -> bool:
    store = str(row.get("_store_source") or "").lower()
    platform = str(row.get("shop_pltf_cd") or "").strip()
    return store == "alibaba" or platform == "1688"


def is_1688_pre_purchase_eligible(row: dict[str, Any]) -> tuple[bool, str]:
    stat = _int_stat(row)
    if stat in SUBMITTED_LINE_STATS:
        return False, "already_submitted"
    if stat != 23:
        label = row.get("ord_line_stat_nm") or f"状态码 {stat}"
        return False, f"invalid_stage:{label}"
    if not _is_1688_channel(row):
        return False, "channel_not_1688"
    category = str(row.get("lvl1_ctgy_nm") or "").strip()
    if not category or category in GENERIC_CATEGORIES:
        return False, "category_blocked"
    return True, "ok"


def _condition(
    key: str,
    label: str,
    passed: bool,
    detail: Optional[str] = None,
) -> dict[str, Any]:
    return {"key": key, "label": label, "passed": passed, "detail": detail}


def evaluate_procurement_pass(
    row: dict[str, Any],
    *,
    product: Optional[dict[str, Any]] = None,
    config: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    cfg = normalize_business_config(config or get_business_config())
    product = product if product is not None else find_product_for_ord_line(row)
    conditions: list[dict[str, Any]] = []

    channel_ok = _is_1688_channel(row)
    channel_detail = f"{row.get('shop_pltf_cd') or '—'}"
    if row.get("splr_item_id"):
        channel_detail += f" · offer {row.get('splr_item_id')}"
    conditions.append(_condition("channel", "渠道已识别", channel_ok, channel_detail))

    sku = check_sku_alignment(row)
    sku_ok = not sku.get("sku_mismatch")
    sku_detail = "规格一致"
    if not sku_ok:
        reasons = sku.get("sku_mismatch_reasons") or []
        sku_detail = reasons[0] if reasons else "规格不一致"
    elif row.get("front_sku_attr_desc") and row.get("item_attr"):
        sku_detail = f"用户「{row.get('front_sku_attr_desc')}」→ 货源「{row.get('item_attr')}」"
    conditions.append(_condition("sku", "SKU 规格一致", sku_ok, sku_detail))

    category = str(row.get("lvl1_ctgy_nm") or "").strip()
    hs = product.get("hs_mapping") if isinstance(product, dict) else None
    title = str(row.get("item_nm") or row.get("item_nm_cn") or "").strip()

    from app.services.category_mapping.mapping_quality import mapping_aligns_with_title

    mapped_cn = str(row.get("procurement_category_cn") or (hs or {}).get("category_cn_name") or "").strip()
    category_ok = bool(category) and category not in GENERIC_CATEGORIES
    category_detail = category or "未映射"

    if isinstance(hs, dict) and hs.get("hs_code"):
        ok, align_detail, _ = mapping_aligns_with_title(title, hs) if title else (True, "", 1.0)
        display_cn = mapped_cn or str(hs.get("category_cn_name") or category)
        category_detail = f"{display_cn} · HS {hs.get('hs_code')}"
        if title and not ok:
            category_ok = False
            category_detail = f"{category_detail}（{align_detail}）"
        elif mapped_cn and mapped_cn != category:
            category_detail = f"{category} → {display_cn} · HS {hs.get('hs_code')}"
    conditions.append(_condition("category", "品类已映射", category_ok, category_detail))

    qty = max(1, int(_num(row.get("ord_cnt"), 1)))
    moq = 1
    if cfg.get("moq", {}).get("enabled"):
        moq = max(1, int(_num(product.get("min_order_qty") if product else None, cfg["moq"]["default_min"])))
    moq_ok = qty >= moq
    conditions.append(
        _condition("moq", "起批量满足", moq_ok, f"订购 {qty} {'≥' if moq_ok else '<'} MOQ {moq}")
    )

    customer_paid = _resolve_customer_paid(row)
    cost = resolve_purchase_cost_basis(row)
    payable = _num(cost.get("purchase_payable"))
    margin = round(customer_paid - payable, 2)
    margin_pct = (margin / customer_paid * 100) if customer_paid > 0 else 0.0
    threshold = float(cfg.get("gross_margin_threshold") or 15)
    try:
        from app.services.evolution.patch_generator import get_active_patches
        from app.services.evolution.policy_apply import resolve_threshold_for_skill

        line_key = str(row.get("ord_line_no") or "")
        threshold = resolve_threshold_for_skill(
            "auto-release",
            threshold,
            line_key,
            get_active_patches(),
            threshold_key="gross_margin_threshold",
        )
    except Exception:
        pass
    block_margin = bool(cfg.get("rules", {}).get("block_negative_margin", True))
    margin_ok = margin_pct >= threshold if customer_paid > 0 else margin >= 0
    if block_margin and margin < -0.02:
        margin_ok = False
    margin_detail = (
        f"毛利率 {margin_pct:.2f}%（{format_currency(margin)} / 实付 {format_currency(customer_paid)}）"
        f"{'≥' if margin_ok else '<'} {threshold:.2f}%"
    )
    conditions.append(_condition("margin", "毛利条件符合", margin_ok, margin_detail))

    note_block = bool(row.get("note_block_procurement"))
    note_detail = "无改价备注" if not note_block else str(row.get("note_classify_reason") or "备注需人工核对")
    conditions.append(_condition("price_note", "无改价待处理", not note_block, note_detail))

    stock_ok = True
    stock_detail = "在售 · 库存充足"
    if product:
        status = str(product.get("stock_status") or "").lower()
        inv = int(_num(product.get("inventory_total")))
        if status == "out_of_stock" or inv <= 0:
            stock_ok = False
            stock_detail = "缺货或库存为 0"
        else:
            stock_detail = f"在售 · 库存 {inv}"
    conditions.append(_condition("stock", "无缺货风险", stock_ok, stock_detail))

    fields_ok = bool(row.get("item_url")) and payable > 0 and bool(row.get("usr_id"))
    fields_detail = (
        f"{'1688' if channel_ok else '货源'}链接、应付 {format_currency(payable)}、用户 {row.get('usr_id') or '—'}"
    )
    conditions.append(_condition("fields", "关键字段完整", fields_ok, fields_detail))

    passed_count = sum(1 for c in conditions if c["passed"])
    all_passed = passed_count == len(conditions) and len(conditions) > 0
    eligible, eligibility_code = is_1688_pre_purchase_eligible(row)

    return {
        "conditions": conditions,
        "passed_count": passed_count,
        "total_count": len(conditions),
        "all_passed": all_passed,
        "eligible": eligible,
        "eligibility_code": eligibility_code,
        "margin_rate": round(margin_pct / 100, 4) if customer_paid > 0 else 0.0,
        "summary": _build_summary(all_passed, eligible, eligibility_code),
    }


def _build_summary(all_passed: bool, eligible: bool, eligibility_code: str) -> str:
    if not eligible:
        if eligibility_code == "already_submitted":
            return "已提交 1688 预订购"
        if eligibility_code.startswith("invalid_stage"):
            return "当前子单状态不可预订购"
        if eligibility_code == "category_blocked":
            return "品类未映射或为「其他」，需先完成品类映射"
        if eligibility_code == "channel_not_1688":
            return "非 1688 渠道，不可调用预订购"
    if all_passed:
        return "全部准入条件满足，自动进入 1688 预订购"
    return "存在未通过条件，需人工复核后放行"


def _blocker(
    key: str,
    label: str,
    stage: str,
    *,
    auto_resolvable: bool = False,
    requires_ack: bool = False,
    detail: Optional[str] = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "stage": stage,
        "auto_resolvable": auto_resolvable,
        "requires_ack": requires_ack,
        "detail": detail,
        "at": _now_iso(),
    }


def evaluate_prepare_stage(
    row: dict[str, Any],
    *,
    product: Optional[dict[str, Any]] = None,
    config: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """处理中阶段拦截：品类、备注、渠道、关键字段；不拦毛利/MOQ/库存。"""
    from app.services.orders import pipeline_store

    cfg = normalize_business_config(config or get_business_config())
    product = product if product is not None else find_product_for_ord_line(row)
    blockers: list[dict[str, Any]] = []
    conditions: list[dict[str, Any]] = []
    acked = pipeline_store.list_acked_keys(str(row.get("ord_line_no") or ""))

    channel_ok = _is_1688_channel(row)
    conditions.append(_condition("channel", "渠道已识别", channel_ok))
    if not channel_ok:
        blockers.append(
            _blocker("CHANNEL", "渠道未识别", "prepare", detail=str(row.get("shop_pltf_cd") or "—"))
        )

    category = str(row.get("lvl1_ctgy_nm") or "").strip()
    category_ok = bool(category) and category not in GENERIC_CATEGORIES
    conditions.append(_condition("category", "品类已映射", category_ok, category or "未映射"))
    if not category_ok:
        auto_map = bool(cfg.get("rules", {}).get("auto_category_mapping", True))
        blockers.append(
            _blocker(
                "CATEGORY_OTHER",
                "品类未映射",
                "prepare",
                auto_resolvable=auto_map,
                detail=category or "其他",
            )
        )

    note_block = bool(row.get("note_block_procurement"))
    note_tier = str(row.get("note_tier") or "").lower()
    note_detail = str(row.get("note_classify_reason") or row.get("usr_rmk") or "备注需人工核对")
    note_ok = not note_block or "NOTE_BLOCK" in acked
    conditions.append(_condition("price_note", "备注已处理", note_ok, note_detail if note_block else "无阻塞备注"))
    if note_block and "NOTE_BLOCK" not in acked:
        requires_ack = note_tier in ("high", "block") or note_tier != "low"
        blockers.append(
            _blocker(
                "NOTE_BLOCK",
                "备注待核",
                "prepare",
                requires_ack=requires_ack,
                auto_resolvable=note_tier == "low",
                detail=note_detail,
            )
        )

    sku = check_sku_alignment(row)
    sku_mismatch = bool(sku.get("sku_mismatch"))
    conditions.append(
        _condition(
            "sku",
            "SKU 规格提示",
            True,
            (sku.get("sku_mismatch_reasons") or ["规格不一致"])[0] if sku_mismatch else "一致",
        )
    )

    cost = resolve_purchase_cost_basis(row)
    payable = _num(cost.get("purchase_payable"))
    fields_ok = bool(row.get("item_url")) and payable > 0 and bool(row.get("usr_id"))
    conditions.append(_condition("fields", "关键字段完整", fields_ok))
    if not fields_ok:
        blockers.append(_blocker("FIELDS", "关键字段不完整", "prepare", detail=f"应付 {format_currency(payable)}"))

    active_blockers = [b for b in blockers if b["key"] not in acked]
    return {
        "conditions": conditions,
        "blockers": active_blockers,
        "all_clear": len(active_blockers) == 0,
        "passed_count": sum(1 for c in conditions if c["passed"]),
        "total_count": len(conditions),
    }


def evaluate_pre_purchase_gate(row: dict[str, Any]) -> dict[str, Any]:
    """预订购前最小门禁：渠道 + 品类 + 字段 + stat=23。"""
    eligible, code = is_1688_pre_purchase_eligible(row)
    prep = evaluate_prepare_stage(row)
    ok = eligible and prep.get("all_clear")
    return {
        "ok": ok,
        "eligible": eligible,
        "eligibility_code": code,
        "prepare": prep,
    }


def _map_review_status(
    *,
    result: str,
    all_passed: bool,
    auto: bool,
) -> ReleaseReviewStatus:
    if result in ("flagged", "admin_failed"):
        return "flagged"
    if result in ("confirmed", "auto_confirmed", "already_submitted"):
        return "confirmed"
    if all_passed and auto:
        return "confirmed"
    return "pending"


def _to_agent_release_record(
    row: dict[str, Any],
    evaluation: dict[str, Any],
    *,
    release_id: str,
    result: str,
    trigger: ReleaseTrigger,
    operator: Optional[str] = None,
    reviewer_note: Optional[str] = None,
    auto_confirmed: bool = False,
    reviewed_at: Optional[str] = None,
) -> dict[str, Any]:
    stat = _int_stat(row) or 23
    review_status = _map_review_status(
        result=result,
        all_passed=bool(evaluation.get("all_passed")),
        auto=auto_confirmed,
    )
    return {
        "release_id": release_id,
        "ord_line_no": row.get("ord_line_no"),
        "order_id": row.get("ord_line_no"),
        "external_order_no": row.get("ord_no") or row.get("out_ord_no") or row.get("ord_line_no"),
        "product_title": row.get("item_nm") or row.get("item_nm_cn") or "",
        "release_type": "procurement_pass",
        "agent_label": "采购准入 Agent",
        "stage_before": "pending_procurement",
        "stage_after": "pending_payment" if result in ("confirmed", "auto_confirmed") else "pending_procurement",
        "released_at": row.get("pay_time") or _now_iso(),
        "conditions": evaluation.get("conditions") or [],
        "summary": evaluation.get("summary") or "",
        "margin_rate": evaluation.get("margin_rate"),
        "review_status": review_status,
        "auto_confirmed": auto_confirmed,
        "reviewed_at": reviewed_at or (_now_iso() if review_status != "pending" else None),
        "reviewer_note": reviewer_note,
        "result": result,
        "trigger": trigger,
        "operator": operator,
        "ord_line_stat_before": stat,
        "ord_line_stat_after": row.get("ord_line_stat"),
    }


def submit_1688_pre_purchase(
    ord_line_no: str,
    *,
    operator: Optional[str] = None,
    trigger: ReleaseTrigger = "manual",
    force: bool = False,
) -> dict[str, Any]:
    key = ord_line_no.strip()
    if not key:
        raise ProcurementReleaseError("缺少子单号 ord_line_no", code="missing_ord_line_no")

    row = get_ord_line(key)
    if not row:
        raise ProcurementReleaseError(f"子单不存在：{key}", code="not_found")

    queue = resolve_order_queue(row) or "pending_procurement"
    if queue != "pending_procurement":
        raise ProcurementReleaseError("仅待下单子单可预订购", code="invalid_queue")

    evaluation = evaluate_procurement_pass(row)
    gate = evaluate_pre_purchase_gate(row)
    eligible = bool(evaluation.get("eligible"))
    all_passed = bool(evaluation.get("all_passed"))
    prepare_clear = bool(gate.get("ok"))
    stat_before = _int_stat(row)

    if stat_before in SUBMITTED_LINE_STATS or release_store.has_successful_release(key):
        record = _persist_release(
            row,
            evaluation,
            result="already_submitted",
            trigger=trigger,
            operator=operator,
            auto_confirmed=True,
        )
        return {
            "ok": True,
            "code": "already_submitted",
            "ord_line_no": key,
            "ord_line_stat_before": stat_before,
            "ord_line_stat_after": stat_before,
            "auto_confirmed": True,
            "release": record,
        }

    if not eligible:
        code = str(evaluation.get("eligibility_code") or "ineligible")
        raise ProcurementReleaseError(
            evaluation.get("summary") or "子单不满足 1688 预订购条件",
            code=code.split(":")[0],
        )

    pipeline_mode = trigger in ("auto_release", "pipeline")
    if pipeline_mode and not prepare_clear:
        prep = gate.get("prepare") or {}
        record = _persist_release(
            row,
            evaluation,
            result="needs_review",
            trigger=trigger if trigger != "pipeline" else "auto_release",
            operator=operator,
            reviewer_note="; ".join(b.get("label", "") for b in prep.get("blockers") or []),
        )
        return {
            "ok": False,
            "code": "blocked_prepare",
            "ord_line_no": key,
            "blockers": prep.get("blockers"),
            "conditions": prep.get("conditions"),
            "release": record,
        }

    if not all_passed and not force and trigger not in ("disposition", "pipeline"):
        record = _persist_release(
            row,
            evaluation,
            result="needs_review",
            trigger=trigger,
            operator=operator,
        )
        return {
            "ok": False,
            "code": "needs_review",
            "ord_line_no": key,
            "conditions": evaluation.get("conditions"),
            "release": record,
        }

    try:
        alibaba_pre_purchase([key])
        admin_result = "ok"
    except TangbuyAdminError as exc:
        record = _persist_release(
            row,
            evaluation,
            result="admin_failed",
            trigger=trigger,
            operator=operator,
            reviewer_note=str(exc),
        )
        disposition_store.append_audit(
            {
                "ord_line_no": key,
                "action_key": "generate_1688_order",
                "action_label": "1688 预订购",
                "stage_before": "pending_procurement",
                "stage_after": "pending_procurement",
                "admin_write": "failed",
                "error": str(exc),
                "operator": operator,
                "trigger": trigger,
                "at": _now_iso(),
            }
        )
        raise ProcurementReleaseError(f"1688 预订购失败：{exc}", code="admin_write_failed") from exc

    refreshed = _refresh_ord_line(key) or row
    stat_after = _int_stat(refreshed)
    auto_confirmed = trigger == "auto_release" and all_passed
    record = _persist_release(
        refreshed,
        evaluation,
        result="auto_confirmed" if auto_confirmed else "confirmed",
        trigger=trigger,
        operator=operator,
        auto_confirmed=auto_confirmed,
    )
    disposition_store.append_audit(
        {
            "ord_line_no": key,
            "ord_no": row.get("ord_no"),
            "action_key": "generate_1688_order",
            "action_label": "1688 预订购",
            "stage_before": "pending_procurement",
            "stage_after": resolve_order_queue(refreshed) or "pending_procurement",
            "admin_write": admin_result,
            "operator": operator,
            "trigger": trigger,
            "auto_confirmed": auto_confirmed,
            "ord_line_stat_before": stat_before,
            "ord_line_stat_after": stat_after,
            "at": _now_iso(),
        }
    )
    return {
        "ok": True,
        "code": "submitted",
        "ord_line_no": key,
        "ord_line_stat_before": stat_before,
        "ord_line_stat_after": stat_after,
        "admin_write": admin_result,
        "auto_confirmed": auto_confirmed,
        "conditions": evaluation.get("conditions"),
        "release": record,
    }


def _persist_release(
    row: dict[str, Any],
    evaluation: dict[str, Any],
    *,
    result: str,
    trigger: ReleaseTrigger,
    operator: Optional[str],
    reviewer_note: Optional[str] = None,
    auto_confirmed: bool = False,
) -> dict[str, Any]:
    release_id = f"rel-{row.get('ord_line_no')}-{int(datetime.now(timezone.utc).timestamp())}"
    record = _to_agent_release_record(
        row,
        evaluation,
        release_id=release_id,
        result=result,
        trigger=trigger,
        operator=operator,
        reviewer_note=reviewer_note,
        auto_confirmed=auto_confirmed,
    )
    saved = release_store.append_release(record)
    try:
        from app.services.workflow.hooks import trace_release_gate

        trace_release_gate(
            str(row.get("ord_line_no") or ""),
            {**evaluation, "ord_no": row.get("ord_no")},
            result=result,
            trigger=trigger,
        )
    except Exception:
        pass
    return saved


def _refresh_ord_line(ord_line_no: str) -> Optional[dict[str, Any]]:
    from app.services.orders import line_cache, order_line_sync

    order_line_sync.refresh_ord_lines([ord_line_no])
    return line_cache.load_all_lines().get(ord_line_no.strip()) or get_ord_line(ord_line_no)


def auto_release_candidates(rows: list[dict[str, Any]]) -> list[str]:
    cfg = normalize_business_config(get_business_config())
    if not cfg.get("auto_1688_pre_purchase_enabled", True):
        return []
    out: list[str] = []
    for row in rows:
        key = str(row.get("ord_line_no") or "").strip()
        if not key:
            continue
        if resolve_order_queue(row) != "pending_procurement":
            continue
        if release_store.has_successful_release(key):
            continue
        evaluation = evaluate_procurement_pass(row)
        if not evaluation.get("eligible"):
            continue
        gate = evaluate_pre_purchase_gate(row)
        if not gate.get("ok"):
            prep = gate.get("prepare") or {}
            if prep.get("blockers"):
                _persist_release(
                    row,
                    evaluation,
                    result="needs_review",
                    trigger="auto_release",
                    operator="system",
                    reviewer_note="; ".join(b.get("label", "") for b in prep.get("blockers") or []),
                )
            continue
        if not evaluation.get("all_passed"):
            _persist_release(
                row,
                evaluation,
                result="needs_review",
                trigger="auto_release",
                operator="system",
            )
            continue
        out.append(key)
    return out


def run_auto_release_batch(ord_line_nos: list[str]) -> dict[str, Any]:
    submitted: list[str] = []
    skipped: list[str] = []
    errors: list[dict[str, str]] = []
    for key in ord_line_nos:
        try:
            result = submit_1688_pre_purchase(key, operator="system", trigger="auto_release")
            if result.get("ok"):
                submitted.append(key)
            else:
                skipped.append(key)
        except ProcurementReleaseError as exc:
            errors.append({"ord_line_no": key, "error": str(exc), "code": exc.code})
    return {"submitted": submitted, "skipped": skipped, "errors": errors}


def list_auto_releases(*, limit: int = 200) -> list[dict[str, Any]]:
    return release_store.list_releases(limit=limit)


def flag_release(
    ord_line_no: str,
    *,
    note: Optional[str] = None,
    operator: Optional[str] = None,
) -> dict[str, Any]:
    key = ord_line_no.strip()
    row = get_ord_line(key)
    if not row:
        raise ProcurementReleaseError(f"子单不存在：{key}", code="not_found")

    stat = _int_stat(row)
    admin_submitted = stat in SUBMITTED_LINE_STATS or release_store.has_successful_release(key)

    disposition_store.revert_procurement_pass(
        key,
        ord_no=str(row.get("ord_no") or ""),
        note=note,
        operator=operator,
    )

    try:
        from app.services.orders import line_cache

        cached = line_cache.load_all_lines().get(key)
        if cached:
            line_cache.merge_lines([disposition_store.apply_row_override(cached)])
    except Exception:
        pass

    evaluation = evaluate_procurement_pass(row)
    record = _persist_release(
        row,
        evaluation,
        result="flagged",
        trigger="manual",
        operator=operator,
        reviewer_note=note or "不认可放行",
    )
    disposition_store.append_audit(
        {
            "ord_line_no": key,
            "ord_no": row.get("ord_no"),
            "action_key": "flag_release",
            "action_label": "不认可放行",
            "stage_before": "pending_payment",
            "stage_after": "pending_procurement",
            "operator": operator,
            "note": note,
            "feedback_type": "override",
            "admin_submitted": admin_submitted,
            "at": _now_iso(),
        }
    )
    return {
        "ok": True,
        "release": record,
        "reverted_to": "pending_procurement",
        "admin_submitted": admin_submitted,
        "message": (
            "已退回待下单"
            if not admin_submitted
            else "已标记不认可并退回待下单；1688 侧可能已预订购，请到订单中心核对"
        ),
    }


def acknowledge_release(
    ord_line_no: str,
    *,
    operator: Optional[str] = None,
    note: Optional[str] = None,
) -> dict[str, Any]:
    """人工认可放行决策（不重复执行采购动作，仅记审计）。"""
    key = ord_line_no.strip()
    latest = release_store.latest_release(key)
    if not latest:
        raise ProcurementReleaseError(f"未找到放行记录：{key}", code="not_found")
    record = {
        **latest,
        "audit_acknowledged": True,
        "audit_acknowledged_at": _now_iso(),
        "released_at": _now_iso(),
        "reviewer_note": note or latest.get("reviewer_note"),
        "operator": operator or latest.get("operator"),
    }
    release_store.append_release(record)
    disposition_store.append_audit(
        {
            "ord_line_no": key,
            "ord_no": latest.get("external_order_no") or latest.get("ord_no"),
            "action_key": "acknowledge_release",
            "action_label": "认可放行",
            "stage_before": latest.get("stage_before"),
            "stage_after": latest.get("stage_after"),
            "operator": operator,
            "note": note,
            "feedback_type": "adopted",
            "at": _now_iso(),
        }
    )
    return {"ok": True, "release": record}

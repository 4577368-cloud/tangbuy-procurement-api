"""采购助手 — 订单/系统数据只读查询（防编造，走真实 API）。"""

from __future__ import annotations

import re
from typing import Any, Optional

from app.services.command_center.briefing import get_command_center_stats
from app.services.data_center import QUEUE_LABELS, get_data_center_snapshot
from app.services.orders import service as order_service
from app.services.orders.queue_filters import resolve_order_queue
from app.services.tasks.store import get_agent_operation_stats, get_task_stats

_ORDER_ID_RE = re.compile(r"\b\d{10,22}\b")
_TI_PREFIX_RE = re.compile(r"\bTI[\d\-]+\b", re.I)
_TO_PREFIX_RE = re.compile(r"\bTO[\d\-]+\b", re.I)

QUEUE_ALIASES: dict[str, str] = {
    "待下单": "pending_procurement",
    "待采购": "pending_procurement",
    "待支付": "pending_payment",
    "已订购": "ordered",
    "待发货": "ordered",
    "已发货": "shipped",
    "已到仓": "in_warehouse",
    "已发出": "dispatched",
    "异常": "exception",
    "逆向": "reverse",
    "退款": "reverse",
    "全部": "all",
}

SIGNAL_LABELS: dict[str, str] = {
    "PAY_AMOUNT_GAP": "补款",
    "SKU_MISMATCH": "SKU 不符",
    "NOTE_RISK": "备注风险",
    "SHIP_OVERDUE": "超时未发",
    "PROCUREMENT_BLOCK": "采购阻塞",
    "CATEGORY_UNMAPPED": "品类未映射",
}


def _int(val: Optional[str], default: int, *, cap: int = 20) -> int:
    try:
        n = int(val) if val else default
    except ValueError:
        n = default
    return max(1, min(n, cap))


def _num(v: Any, default: float = 0.0) -> float:
    try:
        n = float(v)
        return n if n == n else default
    except (TypeError, ValueError):
        return default


def _row_card(row: dict[str, Any]) -> dict[str, Any]:
    queue = resolve_order_queue(row) or "pending_procurement"
    product_amt = _num(row.get("pur_amt"), _num(row.get("pur_prc")) * _num(row.get("ord_cnt"), 1))
    shipping = _num(row.get("post_fee"))
    customer_paid = _num(row.get("ds_ord_amt"), product_amt + shipping)
    abn = int(row.get("abn_type_cd") or 0)
    health = "needs_action" if abn or queue in ("exception", "pending_procurement", "pending_payment") else "normal"
    return {
        "ord_line_no": row.get("ord_line_no") or "",
        "ord_no": row.get("ord_no") or row.get("out_ord_no") or "",
        "pur_no": row.get("pur_no") or "",
        "item_nm": row.get("item_nm") or row.get("item_nm_cn") or "",
        "item_img": row.get("item_img") or "",
        "ord_cnt": row.get("ord_cnt"),
        "queue": queue,
        "queue_label": QUEUE_LABELS.get(queue, queue),
        "ord_line_stat_nm": row.get("ord_line_stat_nm") or "",
        "ord_stat_nm": row.get("ord_stat_nm") or row.get("ds_ord_stat_nm") or "",
        "usr_nm": row.get("usr_nm") or "",
        "splr_shop_nm": row.get("splr_shop_nm") or "",
        "pay_time": row.get("pay_time") or "",
        "customer_paid_amount": round(customer_paid, 2),
        "health": health,
    }


def _match_keyword(row: dict[str, Any], keyword: str) -> bool:
    kw = keyword.strip().lower()
    if not kw:
        return True
    hay = " ".join(
        str(row.get(k) or "")
        for k in (
            "ord_line_no",
            "ord_no",
            "pur_no",
            "out_ord_no",
            "item_nm",
            "item_nm_cn",
            "usr_nm",
            "splr_shop_nm",
            "bd_usr_nm",
        )
    ).lower()
    return kw in hay


def resolve_queue_from_text(text: str) -> Optional[str]:
    for label, code in QUEUE_ALIASES.items():
        if label in text:
            return code
    for code in QUEUE_LABELS:
        if code in text:
            return code
    return None


def extract_lookup_ids(text: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for m in _TI_PREFIX_RE.finditer(text):
        v = m.group(0).upper().replace("-", "")
        if v not in seen:
            seen.add(v)
            found.append(v)
    for m in _TO_PREFIX_RE.finditer(text):
        v = m.group(0).upper().replace("-", "")
        if v not in seen:
            seen.add(v)
            found.append(v)
    for m in _ORDER_ID_RE.finditer(text):
        v = m.group(0)
        if v not in seen:
            seen.add(v)
            found.append(v)
    return found


def _lookup_one(order_id: str) -> Optional[dict[str, Any]]:
    oid = order_id.strip()
    if not oid:
        return None

    # 子单号
    for candidate in (oid, oid.upper()):
        row = order_service.get_ord_line(candidate)
        if row:
            return row

    # 主单号
    result = order_service.list_ord_lines(ord_no=oid, page=1, page_size=5)
    items = result.get("items") or []
    if items:
        return items[0]

    # 1688 采购单号 — 按队列扫描
    queues = list(QUEUE_LABELS.keys()) + ["all"]
    for q in queues:
        listed = order_service.list_ord_lines(
            queue=None if q == "all" else q,
            page=1,
            page_size=80,
        )
        for row in listed.get("items") or []:
            if str(row.get("pur_no") or "") == oid:
                return row
            if str(row.get("ord_no") or "") == oid:
                return row
            if str(row.get("ord_line_no") or "") == oid:
                return row
    return None


def execute_procurement_stats(args: dict[str, str]) -> dict[str, Any]:
    scope = (args.get("scope") or "orders").strip().lower()
    queue_filter = (args.get("queue") or "").strip() or None

    if scope in ("overview", "all", "system"):
        return _build_overview_stats(queue_filter)

    if scope in ("signals", "command", "risk"):
        return _build_signal_stats()

    return _build_order_stats(queue_filter)


def _build_order_stats(queue_filter: Optional[str]) -> dict[str, Any]:
    summary = order_service.queue_summary()
    if summary.get("error"):
        return {
            "success": False,
            "error": summary.get("error"),
            "markdown": f"❌ 订单统计不可用：{summary.get('error')}",
        }

    counts = summary.get("counts") if isinstance(summary.get("counts"), dict) else {}
    groups = [
        {
            "label": "履约队列",
            "value": int(counts.get("all") or 0),
            "highlight": True,
            "breakdown": [
                {
                    "label": QUEUE_LABELS.get(q, q),
                    "value": int(counts.get(q) or 0),
                    "tone": "rose" if q == "exception" else "amber" if q in ("pending_procurement", "pending_payment") else "default",
                }
                for q in QUEUE_LABELS
            ],
        }
    ]

    if queue_filter and queue_filter in QUEUE_LABELS:
        n = int(counts.get(queue_filter) or 0)
        label = QUEUE_LABELS[queue_filter]
        summary_text = f"{label} {n} 单"
        markdown = f"**{label}**：{n} 单（全库 {int(counts.get('all') or 0)} 单）"
    else:
        pending = int(counts.get("pending_procurement") or 0) + int(counts.get("pending_payment") or 0)
        summary_text = f"待处理 {pending} 单 · 全库 {int(counts.get('all') or 0)} 单"
        markdown = (
            "**订单队列统计**\n\n"
            + "\n".join(
                f"- {QUEUE_LABELS[q]}：{int(counts.get(q) or 0)}"
                for q in QUEUE_LABELS
            )
            + f"\n- **合计**：{int(counts.get('all') or 0)}"
        )

    return {
        "success": True,
        "summary": summary_text,
        "markdown": markdown,
        "data": {"kind": "stats", "scope": "orders", "groups": groups, "counts": counts},
    }


def _build_signal_stats() -> dict[str, Any]:
    try:
        stats = get_command_center_stats()
    except Exception as exc:
        return {"success": False, "error": str(exc), "markdown": f"❌ 信号统计不可用：{exc}"}

    signal_counts = stats.get("signal_counts") if isinstance(stats.get("signal_counts"), dict) else {}
    queue_counts = stats.get("queue_counts") if isinstance(stats.get("queue_counts"), dict) else {}
    breakdown = [
        {"label": SIGNAL_LABELS.get(k, k), "value": int(v or 0), "tone": "rose" if int(v or 0) > 0 else "default"}
        for k, v in sorted(signal_counts.items(), key=lambda x: -int(x[1] or 0))
        if int(v or 0) > 0
    ][:8]
    if not breakdown:
        breakdown = [{"label": "暂无待处理信号", "value": 0}]

    groups = [
        {
            "label": "异常信号",
            "value": sum(int(v or 0) for v in signal_counts.values()),
            "alert": True,
            "breakdown": breakdown,
        },
        {
            "label": "扫描样本",
            "value": int(stats.get("scanned_rows") or 0),
            "breakdown": [
                {"label": "超时未发(估)", "value": int(stats.get("ship_overdue_estimated") or 0), "tone": "amber"},
            ],
        },
    ]

    summary = f"{sum(int(v or 0) for v in signal_counts.values())} 条异常信号"
    lines = [f"- {SIGNAL_LABELS.get(k, k)}：{int(v or 0)}" for k, v in signal_counts.items() if int(v or 0) > 0]
    markdown = "**指挥中心信号**\n\n" + ("\n".join(lines) if lines else "暂无待处理信号")

    return {
        "success": True,
        "summary": summary,
        "markdown": markdown,
        "data": {
            "kind": "stats",
            "scope": "signals",
            "groups": groups,
            "queue_counts": queue_counts,
            "signal_counts": signal_counts,
        },
    }


def _build_overview_stats(queue_filter: Optional[str]) -> dict[str, Any]:
    order_part = _build_order_stats(queue_filter)
    if not order_part.get("success"):
        return order_part

    task_stats = get_task_stats()
    agent_ops = get_agent_operation_stats()
    try:
        dc = get_data_center_snapshot()
        ai_quality = dc.get("ai_quality") if isinstance(dc.get("ai_quality"), dict) else {}
    except Exception:
        ai_quality = {}

    groups = list((order_part.get("data") or {}).get("groups") or [])
    groups.append(
        {
            "label": "Agent 任务",
            "value": int(task_stats.get("in_progress") or 0),
            "breakdown": [
                {"label": "进行中", "value": int(task_stats.get("in_progress") or 0), "tone": "amber"},
                {"label": "待复核", "value": int(task_stats.get("needs_review") or 0), "tone": "amber"},
                {"label": "已完成", "value": int(task_stats.get("completed") or 0), "tone": "emerald"},
            ],
        }
    )
    if int(agent_ops.get("active") or 0) > 0:
        groups.append(
            {
                "label": "自动操作",
                "value": int(agent_ops.get("active") or 0),
                "alert": True,
            }
        )

    pending_review = int(ai_quality.get("pending_review") or 0)
    summary = f"{order_part.get('summary')} · 任务进行中 {int(task_stats.get('in_progress') or 0)}"
    markdown = order_part.get("markdown", "") + f"\n\n**任务**：进行中 {int(task_stats.get('in_progress') or 0)} · 已完成 {int(task_stats.get('completed') or 0)}"
    if pending_review:
        markdown += f"\n**AI 待确认**：{pending_review}"

    return {
        "success": True,
        "summary": summary,
        "markdown": markdown,
        "data": {"kind": "stats", "scope": "overview", "groups": groups},
    }


def execute_order_query(args: dict[str, str]) -> dict[str, Any]:
    mode = (args.get("mode") or "lookup").strip().lower()
    if mode == "list":
        return _execute_order_list(args)
    return _execute_order_lookup(args)


def _execute_order_lookup(args: dict[str, str]) -> dict[str, Any]:
    order_id = (args.get("order_id") or "").strip()
    if not order_id:
        return {"success": False, "markdown": "❌ 需要 order_id（子单号 / 主单号 / 1688 采购单号）"}

    ids = extract_lookup_ids(order_id) or [order_id]
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for oid in ids:
        row = _lookup_one(oid)
        if not row:
            continue
        key = str(row.get("ord_line_no") or "")
        if key and key not in seen:
            seen.add(key)
            rows.append(row)

    if not rows:
        return {
            "success": False,
            "markdown": f"❌ 未找到订单 `{order_id}`。请确认子单号 / 主单号 / 1688 采购单号是否正确。",
            "data": {"kind": "orders", "rows": [], "total": 0},
        }

    cards = [_row_card(r) for r in rows]
    if len(cards) == 1:
        c = cards[0]
        summary = f"已找到 {c['ord_line_no']}"
        markdown = (
            f"**{c['item_nm'] or '—'}**\n\n"
            f"- 子单：{c['ord_line_no']}\n"
            f"- 主单：{c['ord_no']}\n"
            f"- 队列：{c['queue_label']} · {c['ord_line_stat_nm']}\n"
            f"- 用户：{c['usr_nm'] or '—'}"
        )
    else:
        summary = f"找到 {len(cards)} 条订单"
        markdown = summary + "。点击下方卡片查看详情。"

    return {
        "success": True,
        "summary": summary,
        "markdown": markdown,
        "data": {"kind": "orders", "rows": cards, "total": len(cards)},
    }


def _execute_order_list(args: dict[str, str]) -> dict[str, Any]:
    queue = (args.get("queue") or "").strip() or None
    keyword = (args.get("keyword") or "").strip()
    limit = _int(args.get("limit"), 5)

    if queue == "all":
        queue = None

    result = order_service.list_ord_lines(
        queue=queue,
        page=1,
        page_size=max(limit * 4, 20),
    )
    if result.get("error"):
        return {
            "success": False,
            "error": result.get("error"),
            "markdown": f"❌ 订单列表不可用：{result.get('error')}",
        }

    items = result.get("items") or []
    if keyword:
        items = [r for r in items if _match_keyword(r, keyword)]

    items = items[:limit]
    cards = [_row_card(r) for r in items]
    total = int(result.get("total") or len(cards))
    queue_label = QUEUE_LABELS.get(queue, "全部") if queue else "全部"

    if not cards:
        hint = f"{queue_label}队列" + (f" · 关键词「{keyword}」" if keyword else "")
        return {
            "success": True,
            "summary": f"{hint}无匹配",
            "markdown": f"{hint}暂无匹配订单。",
            "data": {"kind": "orders", "rows": [], "total": 0, "queue": queue, "keyword": keyword},
        }

    summary = f"{queue_label} {len(cards)} 条" + (f"（共约 {total} 单）" if total > len(cards) else "")
    markdown = f"**{queue_label}** 共约 {total} 单，展示 {len(cards)} 条。点击卡片跳转订单详情。"

    return {
        "success": True,
        "summary": summary,
        "markdown": markdown,
        "data": {
            "kind": "orders",
            "rows": cards,
            "total": total,
            "queue": queue,
            "keyword": keyword or None,
        },
    }

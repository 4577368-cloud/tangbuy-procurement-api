"""履约简报兜底：无 LLM 或 LLM 失败时由事实 JSON 生成固定结构 Markdown。"""

from __future__ import annotations

from typing import Any

QUEUE_LABELS: dict[str, str] = {
    "pending_procurement": "待下单",
    "pending_payment": "待支付",
    "ordered": "已订购",
    "shipped": "已发货",
    "in_warehouse": "已到仓",
    "dispatched": "已发出",
    "exception": "异常",
    "reverse": "逆向",
}

SIGNAL_LABELS: dict[str, str] = {
    "PAY_AMOUNT_GAP": "负毛利",
    "ZERO_MARGIN": "零毛利",
    "LOW_MARGIN": "低毛利",
    "SKU_MISMATCH": "规格不符",
    "SHIP_OVERDUE": "超时发货",
    "STOCKOUT": "无货",
    "NOTE_REVIEW": "备注待核",
    "SUGGESTED_PRICE_GAP": "建议价偏高",
    "OTHER": "其他",
}

DISPOSITION_LABELS: dict[str, str] = {
    "manual_confirm": "人工放行",
    "change_seller": "换供",
    "to_exception": "转异常",
    "request_topup": "发起补款",
}

TASK_LABELS: dict[str, str] = {
    "auto_release": "自动下单",
    "category_mapping": "品类映射",
    "order_followup": "催单",
    "inquiry_1688": "1688询盘",
    "sourcing_inquiry": "寻源询盘",
    "newton_agent": "智能咨询",
}


def _fmt_delta(n: int) -> str:
    if n > 0:
        return f"+{n}"
    return str(n)


def render_briefing_fallback(*, facts: dict[str, Any], delta: dict[str, Any]) -> str:
    sections: list[str] = []

    # 距上次刷新
    since_lines: list[str] = []
    if delta.get("is_first"):
        since_lines.append("- 首次生成，暂无对比基线")
    else:
        mins = delta.get("interval_minutes")
        if mins is not None:
            since_lines.append(f"- 距上次约 {mins} 分钟")
        qd = delta.get("queue_counts") if isinstance(delta.get("queue_counts"), dict) else {}
        for key, label in QUEUE_LABELS.items():
            d = int(qd.get(key) or 0)
            if d != 0:
                since_lines.append(f"- {label} {_fmt_delta(d)}件")
        sd = (
            delta.get("board_signal_counts_action")
            if isinstance(delta.get("board_signal_counts_action"), dict)
            else {}
        )
        if not sd:
            sd = delta.get("signal_counts") if isinstance(delta.get("signal_counts"), dict) else {}
        for key, label in SIGNAL_LABELS.items():
            d = int(sd.get(key) or 0)
            if d != 0:
                since_lines.append(f"- {label} {_fmt_delta(d)}件")
        ship_d = int(delta.get("ship_overdue_estimated") or 0)
        if ship_d != 0:
            since_lines.append(f"- 超时发货 {_fmt_delta(ship_d)}件")
    if not since_lines:
        since_lines.append("- 各队列与信号数量较上次无明显变化")
    sections.append("## 距上次刷新\n" + "\n".join(since_lines))

    # 今日累计
    today_lines: list[str] = []
    qc = facts.get("queue_counts") if isinstance(facts.get("queue_counts"), dict) else {}
    pending = int(qc.get("pending_procurement") or 0) + int(qc.get("pending_payment") or 0)
    today_lines.append(f"- 待处理合计 {pending}件（待下单 {int(qc.get('pending_procurement') or 0)} · 待支付 {int(qc.get('pending_payment') or 0)}）")
    today_lines.append(f"- 全库在途 {int(qc.get('all') or 0)}件")
    disp = facts.get("disposition_today") if isinstance(facts.get("disposition_today"), dict) else {}
    for key, n in disp.items():
        if int(n or 0) > 0:
            today_lines.append(f"- 今日{DISPOSITION_LABELS.get(key, key)} {int(n)}次")
    tasks = facts.get("tasks_completed_today") if isinstance(facts.get("tasks_completed_today"), dict) else {}
    task_done = sum(int(v or 0) for v in tasks.values())
    if task_done > 0:
        today_lines.append(f"- Agent 任务今日完成 {task_done}次")
    cm = facts.get("category_mapping") if isinstance(facts.get("category_mapping"), dict) else {}
    if int(cm.get("today_mapped") or 0) > 0:
        today_lines.append(f"- 品类映射今日完成 {int(cm.get('today_mapped') or 0)}件")
    sections.append("## 今日累计\n" + "\n".join(today_lines[:4]))

    # 昨日遗留
    yc = facts.get("yesterday_carryover") if isinstance(facts.get("yesterday_carryover"), dict) else {}
    yest_lines: list[str] = []
    for key, label in SIGNAL_LABELS.items():
        n = int(yc.get(key) or 0)
        if n > 0:
            yest_lines.append(f"- {label} {n}件")
    if not yest_lines:
        yest_lines.append("- 暂无昨日遗留待处理信号")
    sections.append("## 昨日遗留\n" + "\n".join(yest_lines[:4]))

    # 重点关注（与订单处理看板「待处理」卡片同源）
    sc = (
        facts.get("board_signal_counts_action")
        if isinstance(facts.get("board_signal_counts_action"), dict)
        else {}
    )
    focus_lines: list[str] = []
    pay_gap = int(sc.get("PAY_AMOUNT_GAP") or 0)
    ship_over = int(sc.get("SHIP_OVERDUE") or 0) or int(
        facts.get("ship_overdue_estimated") or 0
    )
    if pay_gap > 0:
        focus_lines.append(f"- 负毛利 {pay_gap}件，建议优先复核补款与放行")
    if ship_over > 0:
        focus_lines.append(f"- 超时发货 {ship_over}件，建议跟进催单")
    for key, label in SIGNAL_LABELS.items():
        if key in ("PAY_AMOUNT_GAP",):
            continue
        n = int(sc.get(key) or 0)
        if n > 0 and len(focus_lines) < 4:
            focus_lines.append(f"- {label} {n}件")
    exc = int(qc.get("exception") or 0)
    if exc > 0 and len(focus_lines) < 4:
        focus_lines.append(f"- 异常队列 {exc}件")
    agent_active = int(facts.get("agent_active") or 0)
    if agent_active > 0 and len(focus_lines) < 4:
        focus_lines.append(f"- Agent 自动操作进行中 {agent_active}件")
    if not focus_lines:
        focus_lines.append("- 当前无高优先级异常信号")
    sections.append("## 重点关注\n" + "\n".join(focus_lines[:4]))

    return "\n\n".join(sections)

"""采购助手 — 订单查询语义：时间维度、字段口径、可组合筛选。"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

from app.services.orders.exception_rules import classify_exception
from app.services.orders.queue_filters import resolve_order_queue

_SH_TZ = ZoneInfo("Asia/Shanghai")

TIME_FIELDS: dict[str, dict[str, Any]] = {
    "pay_time": {
        "label": "支付时间",
        "aliases": ("支付", "付款", "已支付", "付的", "付了"),
    },
    "pur_time": {
        "label": "订购时间",
        "aliases": ("订购", "采购下单", "已订购", "下单采购"),
    },
    "wh_stock_in_time": {
        "label": "入库时间",
        "aliases": ("入仓", "入库", "到仓", "进仓"),
    },
    "pkg_snd_time": {
        "label": "发货时间",
        "aliases": ("发货", "包裹发出", "已发货"),
    },
    "crt_time": {
        "label": "创建时间",
        "aliases": ("创建", "下单创建", "订单创建"),
    },
}

TIME_PRESETS: dict[str, dict[str, Any]] = {
    "today": {"label": "今天", "aliases": ("今天", "今日", "当天")},
    "yesterday": {"label": "昨天", "aliases": ("昨天", "昨日")},
    "day_before_yesterday": {"label": "前天", "aliases": ("前天",)},
    "this_week": {"label": "本周", "aliases": ("本周", "这周", "本星期")},
    "last_week": {"label": "上周", "aliases": ("上周", "上星期")},
    "this_month": {"label": "本月", "aliases": ("本月", "这个月")},
    "last_month": {"label": "上月", "aliases": ("上月", "上个月")},
    "all": {"label": "累计", "aliases": ("累计", "全部", "所有", "历史")},
}


def get_order_query_capabilities() -> dict[str, Any]:
    """供 LLM / 前端展示：当前可统计、可筛选的字段组合。"""
    return {
        "time_fields": [
            {"key": k, "label": v["label"], "aliases": list(v["aliases"])}
            for k, v in TIME_FIELDS.items()
        ],
        "time_presets": [
            {"key": k, "label": v["label"], "aliases": list(v["aliases"])}
            for k, v in TIME_PRESETS.items()
        ],
        "dimensions": [
            {
                "key": "queue",
                "label": "当前履约队列",
                "values": [
                    "pending_procurement",
                    "pending_payment",
                    "ordered",
                    "shipped",
                    "in_warehouse",
                    "dispatched",
                    "exception",
                    "reverse",
                ],
            },
            {"key": "bd_owner", "label": "BD 对接人", "field": "bd_usr_nm"},
            {"key": "user_keyword", "label": "客户昵称/邮箱", "field": "usr_nm"},
            {"key": "keyword", "label": "商品/店铺/单号关键词"},
            {
                "key": "health",
                "label": "健康度",
                "values": ["all", "needs_action", "normal"],
                "default": "all",
            },
        ],
        "rules": [
            "时间筛选默认包含正常与异常订单，不限当前队列，除非用户另指定队列",
            "「昨天支付的订单」= time_field=pay_time + time_preset=yesterday",
            "「本周入仓」= time_field=wh_stock_in_time + time_preset=this_week",
            "队列=exception 表示当前有异常需处理或状态为异常，与支付时间筛选可组合",
        ],
    }


def capabilities_markdown() -> str:
    cap = get_order_query_capabilities()
    lines = ["## 订单查询可组合维度", ""]
    lines.append("**时间字段**（time_field）：")
    for f in cap["time_fields"]:
        lines.append(f"- `{f['key']}` {f['label']}（{' / '.join(f['aliases'][:4])}）")
    lines.append("")
    lines.append("**时间范围**（time_preset）：")
    for p in cap["time_presets"]:
        lines.append(f"- `{p['key']}` {p['label']}")
    lines.append("")
    lines.append("**其他**：queue、bd_owner、user_keyword、keyword、health(all/needs_action/normal)")
    lines.append("")
    for r in cap["rules"]:
        lines.append(f"- {r}")
    return "\n".join(lines)


def _parse_iso(value: Any) -> Optional[datetime]:
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
            dt = dt.replace(tzinfo=_SH_TZ)
        return dt.astimezone(_SH_TZ)
    except ValueError:
        return None


def resolve_time_preset(text: str) -> Optional[str]:
    for key, meta in TIME_PRESETS.items():
        if any(a in text for a in meta["aliases"]):
            return key
    return None


def resolve_time_field(text: str, explicit: Optional[str] = None) -> Optional[str]:
    if explicit and explicit in TIME_FIELDS:
        return explicit
    # 按别名长度降序，避免「发货」误匹配
    hits: list[tuple[int, str]] = []
    for key, meta in TIME_FIELDS.items():
        for alias in meta["aliases"]:
            if alias in text:
                hits.append((len(alias), key))
    if hits:
        hits.sort(reverse=True)
        return hits[0][1]
    if re.search(r"支付|付款", text):
        return "pay_time"
    return None


def resolve_time_range(
    preset: str,
    *,
    time_from: Optional[str] = None,
    time_to: Optional[str] = None,
) -> tuple[Optional[datetime], Optional[datetime], str]:
    """返回 (start, end_exclusive, label)。"""
    if time_from or time_to:
        start = _parse_iso(time_from) if time_from else None
        end = _parse_iso(time_to) if time_to else None
        if end and end.hour == 0 and end.minute == 0:
            end = end + timedelta(days=1)
        label = f"{time_from or '…'} ~ {time_to or '…'}"
        return start, end, label

    now = datetime.now(_SH_TZ)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    if preset in ("all", "", "cumulative"):
        return None, None, TIME_PRESETS["all"]["label"]

    if preset == "today":
        return today_start, now + timedelta(seconds=1), TIME_PRESETS["today"]["label"]
    if preset == "yesterday":
        start = today_start - timedelta(days=1)
        return start, today_start, TIME_PRESETS["yesterday"]["label"]
    if preset == "day_before_yesterday":
        end = today_start - timedelta(days=1)
        start = today_start - timedelta(days=2)
        return start, end, TIME_PRESETS["day_before_yesterday"]["label"]
    if preset == "this_week":
        weekday = today_start.weekday()
        start = today_start - timedelta(days=weekday)
        return start, now + timedelta(seconds=1), TIME_PRESETS["this_week"]["label"]
    if preset == "last_week":
        weekday = today_start.weekday()
        this_mon = today_start - timedelta(days=weekday)
        end = this_mon
        start = this_mon - timedelta(days=7)
        return start, end, TIME_PRESETS["last_week"]["label"]
    if preset == "this_month":
        start = today_start.replace(day=1)
        return start, now + timedelta(seconds=1), TIME_PRESETS["this_month"]["label"]
    if preset == "last_month":
        first_this = today_start.replace(day=1)
        end = first_this
        if first_this.month == 1:
            start = first_this.replace(year=first_this.year - 1, month=12, day=1)
        else:
            start = first_this.replace(month=first_this.month - 1, day=1)
        return start, end, TIME_PRESETS["last_month"]["label"]

    return None, None, preset


def extract_bd_owner(text: str) -> Optional[str]:
    m = re.search(
        r"(?:BD\s*)?([a-z][a-z0-9_]{2,20})\s*(?:的|负责|对接)",
        text,
        re.I,
    )
    if m:
        return m.group(1).strip()
    m = re.search(r"([a-z]+_[a-z]+)\s*的", text, re.I)
    if m:
        return m.group(1).strip()
    return None


def extract_user_keyword(text: str) -> Optional[str]:
    m = re.search(r"[「\"']([^」\"']+)[」\"']\s*(?:客户|用户)", text)
    if m:
        return m.group(1).strip()
    m = re.search(r"([\w.@+-]+@[\w.-]+)\s*(?:客户|用户)?", text)
    if m:
        return m.group(1).strip()
    m = re.search(
        r"(?:客户|用户)\s*[「\"']?([^「」\"'\s，,。]{2,32})[」\"']?",
        text,
    )
    if m:
        val = m.group(1).strip()
        if val not in ("订单", "的", "多少", "有哪些", "统计"):
            return val
    return None


def build_query_filters_from_text(text: str) -> dict[str, str]:
    """从自然语言提取结构化筛选参数（确定性路由用）。"""
    from app.services.agent.data_query import resolve_queue_from_text

    out: dict[str, str] = {}
    preset = resolve_time_preset(text)
    if preset:
        out["time_preset"] = preset

    field = resolve_time_field(text)
    if field:
        out["time_field"] = field
    elif preset and re.search(r"支付|付款", text):
        out["time_field"] = "pay_time"

    queue = resolve_queue_from_text(text)
    if queue:
        out["queue"] = queue

    bd = extract_bd_owner(text)
    if bd:
        out["bd_owner"] = bd

    user_kw = extract_user_keyword(text)
    if user_kw:
        out["user_keyword"] = user_kw

    if re.search(r"同步|列出|有哪些|哪些订单", text):
        out["mode"] = "list"
    if re.search(r"统计|多少|几个|共", text):
        out["count_only"] = "1"

    return out


def normalize_query_args(args: dict[str, str]) -> dict[str, Any]:
    """合并工具参数为统一筛选结构。"""
    time_preset = (args.get("time_preset") or "").strip() or "all"
    time_field = (args.get("time_field") or "").strip() or None
    if time_preset != "all" and not time_field:
        time_field = "pay_time"

    start, end, range_label = resolve_time_range(
        time_preset,
        time_from=(args.get("time_from") or "").strip() or None,
        time_to=(args.get("time_to") or "").strip() or None,
    )

    queue = (args.get("queue") or "").strip() or None
    if queue == "all":
        queue = None

    health = (args.get("health") or "all").strip().lower()
    if health not in ("all", "needs_action", "normal"):
        health = "all"

    field_label = TIME_FIELDS.get(time_field or "", {}).get("label") if time_field else None

    return {
        "queue": queue,
        "keyword": (args.get("keyword") or "").strip() or None,
        "bd_owner": (args.get("bd_owner") or "").strip() or None,
        "user_keyword": (args.get("user_keyword") or "").strip() or None,
        "health": health,
        "time_field": time_field,
        "time_start": start,
        "time_end": end,
        "time_range_label": range_label if time_field or time_preset != "all" else None,
        "time_field_label": field_label,
        "count_only": (args.get("count_only") or "").strip() in ("1", "true", "yes"),
    }


def _row_time(row: dict[str, Any], field: str) -> Optional[datetime]:
    return _parse_iso(row.get(field))


def row_health(row: dict[str, Any]) -> str:
    if classify_exception(row):
        return "needs_action"
    return "normal"


def row_matches_queue_filter(row: dict[str, Any], queue: Optional[str]) -> bool:
    if not queue or queue == "all":
        return True
    if queue == "exception":
        return classify_exception(row) is not None or resolve_order_queue(row) == "exception"
    return resolve_order_queue(row) == queue


def _match_keyword_row(row: dict[str, Any], keyword: str) -> bool:
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


def filter_ord_lines(rows: list[dict[str, Any]], filters: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    time_field = filters.get("time_field")
    time_start = filters.get("time_start")
    time_end = filters.get("time_end")

    for row in rows:
        if not row_matches_queue_filter(row, filters.get("queue")):
            continue

        health = filters.get("health") or "all"
        if health != "all" and row_health(row) != health:
            continue

        bd = filters.get("bd_owner")
        if bd and bd.lower() not in str(row.get("bd_usr_nm") or "").lower():
            continue

        user_kw = filters.get("user_keyword")
        if user_kw:
            usr = str(row.get("usr_nm") or "").lower()
            if user_kw.lower() not in usr and user_kw.lower() not in str(row.get("usr_id") or "").lower():
                continue

        kw = filters.get("keyword")
        if kw and not _match_keyword_row(row, kw):
            continue

        if time_field and (time_start or time_end):
            dt = _row_time(row, time_field)
            if not dt:
                continue
            if time_start and dt < time_start:
                continue
            if time_end and dt >= time_end:
                continue

        out.append(row)
    return out


def load_ord_lines_pool() -> tuple[list[dict[str, Any]], str]:
    """优先 line_cache，空则返回空（由调用方决定是否打 Admin）。"""
    from app.services.orders.line_cache import load_all_lines

    all_lines = load_all_lines()
    if all_lines:
        return list(all_lines.values()), "line_cache"
    return [], "none"


def filters_summary(filters: dict[str, Any]) -> str:
    parts: list[str] = []
    if filters.get("time_range_label") and filters.get("time_field_label"):
        parts.append(f"{filters['time_range_label']} · {filters['time_field_label']}")
    elif filters.get("time_range_label"):
        parts.append(str(filters["time_range_label"]))
    if filters.get("bd_owner"):
        parts.append(f"BD {filters['bd_owner']}")
    if filters.get("user_keyword"):
        parts.append(f"客户 {filters['user_keyword']}")
    if filters.get("queue"):
        from app.services.data_center import QUEUE_LABELS

        parts.append(QUEUE_LABELS.get(filters["queue"], filters["queue"]))
    if filters.get("keyword"):
        parts.append(f"「{filters['keyword']}」")
    return " · ".join(parts) if parts else "全库"

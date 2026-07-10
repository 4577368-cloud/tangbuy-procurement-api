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
        "db_field": "pay_time",
        "meaning": "用户完成支付的时刻；筛选「昨天付的款」用这个，不是订购/入仓时间",
        "queryable": True,
        "aliases": (
            "支付时间", "付款时间", "支付", "付款", "已支付", "付的", "付了", "付过款",
            "客户付款", "用户付款", "用户支付", "付款的", "支付的",
        ),
    },
    "pur_time": {
        "label": "订购时间",
        "db_field": "pur_time",
        "meaning": "向供应商/1688 下单采购的时刻",
        "queryable": True,
        "aliases": (
            "订购时间", "采购时间", "订购", "已订购", "采购下单", "下单采购",
            "向供应商下单", "1688下单", "采购下单",
        ),
    },
    "wh_stock_in_time": {
        "label": "入库时间",
        "db_field": "wh_stock_in_time",
        "meaning": "货物进入仓库的时刻；「入仓/到仓」统计用这个",
        "queryable": True,
        "aliases": (
            "入库时间", "入仓时间", "到仓时间", "入仓", "入库", "到仓", "进仓",
            "到货", "到库", "仓库收到", "进仓库",
        ),
    },
    "pkg_snd_time": {
        "label": "发货时间",
        "db_field": "pkg_snd_time",
        "meaning": "包裹从供应商/国内仓发出的时刻",
        "queryable": True,
        "aliases": (
            "发货时间", "发货", "包裹发出", "已发货", "发出包裹", "快递发出", "国内发货",
        ),
    },
    "crt_time": {
        "label": "创建时间",
        "db_field": "crt_time",
        "meaning": "DS 订单行创建时间；用户下单进入系统",
        "queryable": True,
        "aliases": (
            "创建时间", "下单时间", "创建", "订单创建", "下单创建", "生成订单", "进系统",
        ),
    },
    "sign_time": {
        "label": "签收时间",
        "db_field": "sign_time",
        "meaning": "海外/末端签收时间",
        "queryable": False,
        "aliases": ("签收", "签收时间", "已签收"),
    },
    "snd_ovs_time": {
        "label": "海外发出时间",
        "db_field": "snd_ovs_time",
        "meaning": "包裹从国内仓发往海外",
        "queryable": False,
        "aliases": ("海外发出", "发出海外", "国际发货"),
    },
}

TIME_PRESETS: dict[str, dict[str, Any]] = {
    "today": {"label": "今天", "aliases": ("今天", "今日", "当天", "本日")},
    "yesterday": {"label": "昨天", "aliases": ("昨天", "昨日", "前一天")},
    "day_before_yesterday": {"label": "前天", "aliases": ("前天", "前日")},
    "this_week": {"label": "本周", "aliases": ("本周", "这周", "本星期", "这个星期")},
    "last_week": {"label": "上周", "aliases": ("上周", "上星期", "上个星期")},
    "last_7_days": {"label": "近7天", "aliases": ("近7天", "最近7天", "近一周", "最近一周", "过去7天")},
    "last_30_days": {"label": "近30天", "aliases": ("近30天", "最近30天", "近一个月", "最近一个月", "过去30天")},
    "this_month": {"label": "本月", "aliases": ("本月", "这个月", "当月")},
    "last_month": {"label": "上月", "aliases": ("上月", "上个月", "前一个月")},
    "all": {"label": "累计", "aliases": ("累计", "全部", "所有", "历史", "迄今", "一共以来")},
}

QUEUE_DIMENSION: dict[str, dict[str, Any]] = {
    "pending_procurement": {"label": "待下单", "aliases": ("待下单", "待采购", "还没下单", "未采购")},
    "pending_payment": {"label": "待支付", "aliases": ("待支付", "待付款", "未支付")},
    "ordered": {"label": "已订购", "aliases": ("已订购", "已下单", "待发货", "还没发货")},
    "shipped": {"label": "已发货", "aliases": ("已发货", "在途", "运输中")},
    "in_warehouse": {"label": "已到仓", "aliases": ("已到仓", "在仓", "仓库里", "已入库")},
    "dispatched": {"label": "已发出", "aliases": ("已发出", "海外仓发出", "国际段")},
    "exception": {
        "label": "异常",
        "aliases": ("异常", "有问题", "要处理", "卡住的", "需人工"),
        "meaning": "当前需人工介入或状态异常（可与时间筛选组合）",
    },
    "reverse": {"label": "逆向", "aliases": ("逆向", "退款", "退货", "售后", "退换货")},
}

DIMENSION_FIELDS: dict[str, dict[str, Any]] = {
    "bd_owner": {
        "label": "BD 对接人",
        "db_field": "bd_usr_nm",
        "meaning": "对接 BD 账号，如 jody_zeng",
        "queryable": True,
    },
    "user_keyword": {
        "label": "客户",
        "db_field": "usr_nm",
        "meaning": "客户昵称或邮箱关键词",
        "queryable": True,
    },
    "pur_owner": {
        "label": "采购员",
        "db_field": "pur_usr_nm",
        "meaning": "负责采购的采购员",
        "queryable": False,
    },
    "keyword": {
        "label": "关键词",
        "db_field": "item_nm,splr_shop_nm,ord_line_no,...",
        "meaning": "商品名/店铺/单号模糊搜索",
        "queryable": True,
    },
    "health": {
        "label": "健康度",
        "meaning": "all=全部(默认) needs_action=需处理 normal=正常",
        "queryable": True,
        "values": ("all", "needs_action", "normal"),
    },
}

# 同一意图的多种说法 → 归一输出
OUTPUT_MODE_ALIASES: dict[str, tuple[str, ...]] = {
    "list": (
        "列出", "列一下", "列给我", "有哪些", "哪些订单", "给我", "发我", "同步",
        "拉取", "拉给我", "给我看", "查一下", "查询", "导出", "清单", "列表",
        "都是什么", "都有哪些", "包含哪些",
    ),
    "count": (
        "多少", "几个", "几条", "统计", "汇总", "共有", "一共", "总共", "总数",
        "数量", "概况", "有多少", "几单", "多少单", "几笔",
    ),
    "lookup": ("单号", "这条单", "这个单", "查订单", "订单状态", "详情"),
}

# 时间字段歧义消解：出现这些词组时强制字段
TIME_FIELD_PHRASES: tuple[tuple[str, str], ...] = (
    (r"支付|付款|付的款|付过款", "pay_time"),
    (r"入仓|入库|到仓|进仓|到库", "wh_stock_in_time"),
    (r"订购|采购下单|向供应商", "pur_time"),
    (r"发货|发出包裹", "pkg_snd_time"),
    (r"创建|下单时间|进系统", "crt_time"),
)


def get_order_query_capabilities() -> dict[str, Any]:
    """供 LLM / 前端展示：字段含义 + 可组合维度 + 同义表述规则。"""
    return {
        "grain": "ord_line_no",
        "table": "ads_ops_ord_line_rel_td",
        "time_fields": [
            {
                "key": k,
                "db_field": v["db_field"],
                "label": v["label"],
                "meaning": v.get("meaning", ""),
                "queryable": v.get("queryable", False),
                "aliases": list(v["aliases"]),
            }
            for k, v in TIME_FIELDS.items()
        ],
        "time_presets": [
            {"key": k, "label": v["label"], "aliases": list(v["aliases"])}
            for k, v in TIME_PRESETS.items()
        ],
        "queues": [
            {
                "key": k,
                "label": v["label"],
                "aliases": list(v.get("aliases", ())),
                "meaning": v.get("meaning", "当前履约阶段"),
            }
            for k, v in QUEUE_DIMENSION.items()
        ],
        "dimensions": [
            {
                "key": k,
                "db_field": v.get("db_field"),
                "label": v["label"],
                "meaning": v.get("meaning", ""),
                "queryable": v.get("queryable", False),
                "values": list(v["values"]) if "values" in v else None,
            }
            for k, v in DIMENSION_FIELDS.items()
        ],
        "output_modes": {
            "list": {
                "meaning": "返回订单列表（可点击卡片）",
                "alias_examples": list(OUTPUT_MODE_ALIASES["list"][:8]),
            },
            "count": {
                "meaning": "只返回数量/分布统计",
                "alias_examples": list(OUTPUT_MODE_ALIASES["count"][:8]),
            },
            "lookup": {"meaning": "按单号查一条/多条详情"},
        },
        "rules": [
            "时间筛选 = time_preset + time_field；默认含正常+异常，除非另指定 queue 或 health",
            "「昨天支付的订单」与「昨日付款的单」同一意图：yesterday + pay_time + list",
            "「本周入仓」与「这周到仓的」同一意图：this_week + wh_stock_in_time",
            "queue=exception 是「当前有问题」视图；pay_time=昨天 是「昨天付过款」——可组合",
            "不确定字段时先调 order_query_capabilities",
        ],
    }


def capabilities_markdown() -> str:
    cap = get_order_query_capabilities()
    lines = [
        "## 订单查询字段目录（粒度 ord_line_no）",
        "",
        "### 时间字段 time_field",
    ]
    for f in cap["time_fields"]:
        flag = "✓" if f["queryable"] else "○待接入"
        lines.append(
            f"- `{f['key']}`（{f['db_field']}）{f['label']} [{flag}]"
        )
        if f.get("meaning"):
            lines.append(f"  - {f['meaning']}")
        lines.append(f"  - 用户常说：{' / '.join(f['aliases'][:6])}")
    lines.extend(["", "### 时间范围 time_preset"])
    for p in cap["time_presets"]:
        lines.append(f"- `{p['key']}` {p['label']}（{' / '.join(p['aliases'][:5])}）")
    lines.extend(["", "### 履约队列 queue（当前阶段）"])
    for q in cap["queues"]:
        lines.append(f"- `{q['key']}` {q['label']}（{' / '.join(q['aliases'][:4])}）")
    lines.extend(["", "### 其他维度"])
    for d in cap["dimensions"]:
        flag = "✓" if d["queryable"] else "○待接入"
        lines.append(f"- `{d['key']}` {d['label']} [{flag}] — {d.get('meaning', '')}")
    lines.extend(["", "### 输出意图（同义表述归一）"])
    for mode, meta in cap["output_modes"].items():
        examples = meta.get("alias_examples") or []
        lines.append(f"- **{mode}**：{meta['meaning']}；如：{' / '.join(examples)}")
    lines.extend(["", "### 组合规则"])
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
    for pattern, field in TIME_FIELD_PHRASES:
        if re.search(pattern, text):
            meta = TIME_FIELDS.get(field, {})
            if meta.get("queryable", True):
                return field
    hits: list[tuple[int, str]] = []
    for key, meta in TIME_FIELDS.items():
        if not meta.get("queryable", True):
            continue
        for alias in meta["aliases"]:
            if alias in text:
                hits.append((len(alias), key))
    if hits:
        hits.sort(reverse=True)
        return hits[0][1]
    return None


def resolve_queue_from_text(text: str) -> Optional[str]:
    for key, meta in QUEUE_DIMENSION.items():
        for alias in meta.get("aliases", ()):
            if alias in text:
                return key
        if key in text:
            return key
    return None


def resolve_output_mode(text: str) -> str:
    from app.services.agent.data_query import extract_lookup_ids

    if extract_lookup_ids(text):
        return "lookup"
    for mode, aliases in OUTPUT_MODE_ALIASES.items():
        if any(a in text for a in aliases):
            return mode
    if re.search(r"订单", text):
        return "list"
    return "count"


def resolve_health_from_text(text: str) -> Optional[str]:
    if re.search(r"正常(的)?订单|无异常|健康的", text):
        return "normal"
    if re.search(r"需处理|要处理|有问题|异常单|卡单", text):
        return "needs_action"
    return None


def describe_interpretation(filters: dict[str, str], output_mode: str) -> str:
    parts: list[str] = []
    preset = filters.get("time_preset")
    field = filters.get("time_field")
    if preset and field:
        preset_label = TIME_PRESETS.get(preset, {}).get("label", preset)
        field_label = TIME_FIELDS.get(field, {}).get("label", field)
        parts.append(f"{preset_label}{field_label}")
    elif preset:
        parts.append(TIME_PRESETS.get(preset, {}).get("label", preset))
    if filters.get("bd_owner"):
        parts.append(f"BD {filters['bd_owner']}")
    if filters.get("user_keyword"):
        parts.append(f"客户「{filters['user_keyword']}」")
    if filters.get("queue"):
        parts.append(QUEUE_DIMENSION.get(filters["queue"], {}).get("label", filters["queue"]))
    if filters.get("health") == "needs_action":
        parts.append("需处理")
    elif filters.get("health") == "normal":
        parts.append("正常")

    scope = " · ".join(parts) if parts else "全库"
    if output_mode == "count":
        return f"统计 {scope} 订单数量"
    if output_mode == "lookup":
        return "按单号查询订单详情"
    return f"列出 {scope} 订单（含正常与异常，除非另指定）"


def describe_normalized_filters(filters: dict[str, Any], output_mode: str = "list") -> str:
    semantic = {
        "time_preset": filters.get("time_preset") or "all",
        "time_field": filters.get("time_field"),
        "bd_owner": filters.get("bd_owner"),
        "user_keyword": filters.get("user_keyword"),
        "queue": filters.get("queue"),
        "health": filters.get("health"),
    }
    return describe_interpretation(
        {k: str(v) for k, v in semantic.items() if v},
        output_mode,
    )


def interpret_user_query(text: str) -> dict[str, Any]:
    """多种表述 → 同一查询意图（供路由与 LLM 对齐）。"""
    raw = text.strip()
    filters = build_query_filters_from_text(raw)
    output_mode = resolve_output_mode(raw)
    if output_mode == "lookup":
        tool = "order_query"
        args = {"mode": "lookup"}
        ids = __import__(
            "app.services.agent.data_query", fromlist=["extract_lookup_ids"]
        ).extract_lookup_ids(raw)
        if ids:
            args["order_id"] = ids[0]
    elif output_mode == "count":
        tool = "procurement_stats"
        args = {k: v for k, v in filters.items() if k not in ("mode", "count_only")}
        args["scope"] = "orders"
    else:
        tool = "order_query"
        args = {k: v for k, v in filters.items() if k not in ("count_only",)}
        args.setdefault("mode", "list")
        args.setdefault("limit", "10")

    return {
        "output_mode": output_mode,
        "tool": tool,
        "args": args,
        "filters": filters,
        "interpretation": describe_interpretation(filters, output_mode),
    }


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
    if preset == "last_7_days":
        start = today_start - timedelta(days=7)
        return start, now + timedelta(seconds=1), TIME_PRESETS["last_7_days"]["label"]
    if preset == "last_30_days":
        start = today_start - timedelta(days=30)
        return start, now + timedelta(seconds=1), TIME_PRESETS["last_30_days"]["label"]

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
    _SKIP = frozenset(
        {"订单", "的", "多少", "有哪些", "统计", "订单统计", "情况", "概况", "列表", "清单"}
    )

    m = re.search(r"[「\"']([^」\"']+)[」\"']\s*(?:客户|用户)", text)
    if m:
        return m.group(1).strip()
    m = re.search(r"([\w.@+-]+@[\w.-]+)\s*(?:客户|用户)?", text)
    if m:
        return m.group(1).strip()
    m = re.search(
        r"(?:客户|用户)\s*[「\"']([^「」\"']+)[」\"']",
        text,
    )
    if m:
        return m.group(1).strip()
    m = re.search(
        r"(?:客户|用户)\s*[「\"']?([^「」\"'\s，,。]{2,32})[」\"']?",
        text,
    )
    if m:
        val = m.group(1).strip()
        if val in _SKIP or val.endswith("订单"):
            return None
        return val
    return None


def build_query_filters_from_text(text: str) -> dict[str, str]:
    """从自然语言提取结构化筛选参数（确定性路由用）。"""
    out: dict[str, str] = {}
    preset = resolve_time_preset(text)
    if preset:
        out["time_preset"] = preset

    field = resolve_time_field(text)
    if field:
        out["time_field"] = field
    elif preset and re.search(r"支付|付款|付过", text):
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

    health = resolve_health_from_text(text)
    if health:
        out["health"] = health

    output = resolve_output_mode(text)
    if output == "list":
        out["mode"] = "list"
    if output == "count":
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
        "time_preset": time_preset,
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

"""阶段平均停留（从订单详情 trackList 时间线采样计算）。

trackList 事件为自由文本（content），无结构化阶段码，故用关键词把事件映射到履约阶段，
再按时间线相邻事件计算各阶段停留时长。采样有上限并带 TTL 缓存，避免拖垮接口。
"""

from __future__ import annotations

import threading
import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Optional

from app.services.orders import service as order_service

# 采样与缓存（控制 Admin 详情调用量：每队列 2 条 × 3 队列 ≈ 6 次详情）
_SAMPLE_QUEUES = ("pending_procurement", "ordered", "shipped")
_SAMPLE_PER_QUEUE = 2
_CACHE_TTL_SECONDS = 600
_STALE_SERVE_SECONDS = 3600
_DETAIL_WORKERS = 4

# 阶段顺序 + 文案；关键词按“先具体后宽泛”排序，命中首个匹配阶段
_STAGE_LABELS: dict[str, str] = {
    "pending_procurement": "待下单",
    "ordered": "已订购",
    "shipped": "已发货",
    "in_warehouse": "已到仓",
    "dispatched": "已发出",
}
# 匹配顺序很重要：靠后的阶段先判，避免“国际发货”被“发货”误配到已发货
_STAGE_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("dispatched", ("出库", "出仓", "已发出", "国际", "转运", "海外仓")),
    ("in_warehouse", ("入库", "到仓", "签收", "到货", "仓库")),
    ("shipped", ("卖家发货", "商家发货", "已发货", "发货", "揽收")),
    ("ordered", ("采购下单", "已采购", "代购", "拍单", "采购成功", "采购")),
    ("pending_procurement", ("付款", "支付", "下单", "创建订单")),
]

_cache: dict[str, Any] = {"at": 0.0, "value": []}
_refresh_lock = threading.Lock()
_refreshing = False


def _fetch_timeline(ord_line_no: str) -> Optional[list[dict[str, Any]]]:
    try:
        detail = order_service.get_ord_line_detail(ord_line_no)
    except Exception:  # noqa: BLE001
        return None
    if not detail:
        return None
    timeline = detail.get("timeline")
    return timeline if isinstance(timeline, list) else None


def _recompute_stage_durations() -> list[dict[str, Any]]:
    now_ts = datetime.now(timezone.utc).timestamp()
    totals: dict[str, float] = {}
    hits: dict[str, int] = {}

    candidates: list[str] = []
    seen: set[str] = set()
    for queue in _SAMPLE_QUEUES:
        try:
            result = order_service.list_ord_lines(
                queue=queue, page=1, page_size=_SAMPLE_PER_QUEUE
            )
        except Exception:  # noqa: BLE001
            continue
        items = result.get("items") if isinstance(result.get("items"), list) else []
        for row in items[:_SAMPLE_PER_QUEUE]:
            ord_line_no = str(row.get("ord_line_no") or "")
            if not ord_line_no or ord_line_no in seen:
                continue
            seen.add(ord_line_no)
            candidates.append(ord_line_no)

    with ThreadPoolExecutor(max_workers=_DETAIL_WORKERS) as pool:
        futures = {pool.submit(_fetch_timeline, no): no for no in candidates}
        for fut in as_completed(futures):
            timeline = fut.result()
            if timeline:
                _accumulate_from_timeline(timeline, now_ts, totals, hits)

    durations = [
        {
            "stage": stage,
            "label": label,
            "hours": round(totals[stage] / hits[stage], 1),
            "samples": hits[stage],
        }
        for stage, label in _STAGE_LABELS.items()
        if hits.get(stage)
    ]

    _cache["value"] = durations
    _cache["at"] = _time.monotonic()
    return durations


def _refresh_in_background() -> None:
    global _refreshing
    with _refresh_lock:
        if _refreshing:
            return
        _refreshing = True
    try:
        _recompute_stage_durations()
    finally:
        with _refresh_lock:
            _refreshing = False


def _match_stage(content: str) -> Optional[str]:
    for stage, keywords in _STAGE_KEYWORDS:
        for kw in keywords:
            if kw in content:
                return stage
    return None


def _parse_time(raw: Any) -> Optional[float]:
    """解析时间线时间 → epoch 秒。兼容 'YYYY-MM-DD HH:MM:SS'、ISO 与毫秒时间戳。"""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        v = float(raw)
        return v / 1000.0 if v > 1e12 else v
    s = str(raw).strip()
    if not s:
        return None
    if s.isdigit():
        v = float(s)
        return v / 1000.0 if v > 1e12 else v
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _accumulate_from_timeline(
    timeline: list[dict[str, Any]],
    now_ts: float,
    totals: dict[str, float],
    hits: dict[str, int],
) -> None:
    """从单个订单的时间线累加各阶段停留（小时）。"""
    events: list[tuple[float, str]] = []
    for ev in timeline or []:
        stage = _match_stage(str(ev.get("action") or ""))
        ts = _parse_time(ev.get("time"))
        if stage is None or ts is None:
            continue
        events.append((ts, stage))
    if not events:
        return
    events.sort(key=lambda e: e[0])

    # 折叠相邻同阶段，保留最早进入时间
    collapsed: list[tuple[float, str]] = []
    for ts, stage in events:
        if collapsed and collapsed[-1][1] == stage:
            continue
        collapsed.append((ts, stage))

    for idx, (ts, stage) in enumerate(collapsed):
        end = collapsed[idx + 1][0] if idx + 1 < len(collapsed) else now_ts
        hours = (end - ts) / 3600.0
        if hours <= 0:
            continue
        totals[stage] = totals.get(stage, 0.0) + hours
        hits[stage] = hits.get(stage, 0) + 1


def compute_stage_durations(force: bool = False) -> list[dict[str, Any]]:
    """返回 [{stage, label, hours, samples}]，按阶段顺序。带 TTL + 过期兜底缓存。"""
    nowmono = _time.monotonic()
    cached = _cache["value"]
    age = nowmono - float(_cache["at"] or 0.0)

    if not force and cached and age < _CACHE_TTL_SECONDS:
        return cached  # type: ignore[return-value]

    if not force and cached and age < _STALE_SERVE_SECONDS:
        threading.Thread(target=_refresh_in_background, daemon=True).start()
        return cached  # type: ignore[return-value]

    return _recompute_stage_durations()

"""指挥中心履约简报 — 事实聚合、增量快照、LLM 输入。"""

from __future__ import annotations

import json
import time as _time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Optional
from zoneinfo import ZoneInfo

from app.core.paths import data_dir
from app.services.agent.llm import chat_completion_stream
from app.services.command_center.briefing_fallback import render_briefing_fallback
from app.services.command_center.briefing_prompt import build_briefing_messages
from app.services.command_center.signal_scan import (
    BRIEFING_MAX_PER_QUEUE,
    REASON_TO_SIGNAL,
    aggregate_signal_stats,
    scan_ord_lines_for_signals,
)
from app.services.orders.exception_rules import (
    classify_exception,
    classify_exception_reason,
)
from app.services.products.service import list_products
from app.services.tasks.store import OPERATION_TASK_TYPES, list_tasks

_SH_TZ = ZoneInfo("Asia/Shanghai")
_SNAPSHOT_PATH = data_dir() / "command-center" / "briefing-snapshot.json"
_SHIP_OVERDUE_HOURS = 48
_FACTS_CACHE_TTL = 90.0
_BRIEFING_SYNC_STALE_SEC = 600.0
_facts_cache: dict[str, Any] = {"at": 0.0, "value": None}


def _num(v: Any) -> float:
    try:
        n = float(v)
        return n if n == n else 0.0
    except (TypeError, ValueError):
        return 0.0


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
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _today_start_sh() -> datetime:
    now = datetime.now(_SH_TZ)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _is_today_sh(dt: datetime) -> bool:
    local = dt.astimezone(_SH_TZ)
    start = _today_start_sh()
    return local >= start


def _is_before_today_sh(dt: datetime) -> bool:
    return dt.astimezone(_SH_TZ) < _today_start_sh()


def _row_pay_time(row: dict[str, Any]) -> Optional[datetime]:
    return _parse_iso(row.get("pay_time")) or _parse_iso(row.get("pur_time"))


def _count_yesterday_carryover(rows: list[dict[str, Any]]) -> dict[str, int]:
    """今日 0 点前产生、当前仍为 action 档的订单，按信号类型估算。"""
    out: dict[str, int] = {}
    for row in rows:
        if classify_exception(row) != "action":
            continue
        ref = _row_pay_time(row)
        if not ref or not _is_before_today_sh(ref):
            continue
        result = classify_exception_reason(row)
        label = result[1] if result else "其他"
        signal = REASON_TO_SIGNAL.get(label, "OTHER")
        if row.get("ord_line_stat") in (22, "22"):
            ref_time = _parse_iso(row.get("pur_time")) or ref
            if ref_time and ref_time < datetime.now(timezone.utc) - timedelta(
                hours=_SHIP_OVERDUE_HOURS
            ):
                signal = "SHIP_OVERDUE"
        out[signal] = out.get(signal, 0) + 1
    return out


def _read_dispositions() -> list[dict[str, Any]]:
    path = data_dir() / "orders" / "dispositions.jsonl"
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                if isinstance(row, dict):
                    records.append(row)
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return records


def _disposition_today_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in _read_dispositions():
        at = _parse_iso(row.get("at"))
        if not at or not _is_today_sh(at):
            continue
        key = str(row.get("action_key") or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return counts


def _tasks_completed_today() -> dict[str, int]:
    counts: dict[str, int] = {t: 0 for t in OPERATION_TASK_TYPES}
    for task in list_tasks():
        if task.get("status") != "completed":
            continue
        at = _parse_iso(task.get("completed_at")) or _parse_iso(task.get("updated_at"))
        if not at or not _is_today_sh(at):
            continue
        task_type = str(task.get("type") or "")
        if task_type in counts:
            counts[task_type] += 1
    return counts


def _category_mapping_stats() -> dict[str, int]:
    products = list_products()
    today_prefix = datetime.now(_SH_TZ).strftime("%Y-%m-%d")
    pending_review = 0
    today_mapped = 0
    auto_resolved = 0
    for p in products:
        status = str(p.get("category_status") or "")
        if status in ("pending", "mapping", "failed", "needs_review"):
            pending_review += 1
        if status == "auto_passed":
            auto_resolved += 1
        mapped_at = str(p.get("category_mapped_at") or p.get("mapped_at") or "")
        if mapped_at.startswith(today_prefix):
            today_mapped += 1
    return {
        "pending_review": pending_review,
        "today_mapped": today_mapped,
        "auto_resolved": auto_resolved,
    }


def _load_snapshot() -> Optional[dict[str, Any]]:
    if not _SNAPSHOT_PATH.exists():
        return None
    try:
        data = json.loads(_SNAPSHOT_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def save_briefing_snapshot(facts: dict[str, Any]) -> None:
    _SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "facts": facts,
    }
    _SNAPSHOT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _numeric_delta(current: dict[str, Any], previous: dict[str, Any], key: str) -> int:
    return int(current.get(key) or 0) - int(previous.get(key) or 0)


def _dict_delta(
    current: dict[str, int], previous: dict[str, int]
) -> dict[str, int]:
    keys = set(current) | set(previous)
    return {k: int(current.get(k, 0)) - int(previous.get(k, 0)) for k in keys}


def compute_delta(
    current: dict[str, Any], snapshot: Optional[dict[str, Any]]
) -> dict[str, Any]:
    if not snapshot or not isinstance(snapshot.get("facts"), dict):
        return {"is_first": True, "interval_minutes": None}

    prev = snapshot["facts"]
    prev_at = _parse_iso(snapshot.get("generated_at"))
    interval_minutes: Optional[int] = None
    if prev_at:
        delta_sec = (datetime.now(timezone.utc) - prev_at).total_seconds()
        interval_minutes = max(0, int(delta_sec // 60))

    return {
        "is_first": False,
        "interval_minutes": interval_minutes,
        "queue_counts": _dict_delta(
            current.get("queue_counts") or {},
            prev.get("queue_counts") or {},
        ),
        "exception_bands": _dict_delta(
            current.get("exception_bands") or {},
            prev.get("exception_bands") or {},
        ),
        "signal_counts": _dict_delta(
            current.get("signal_counts") or {},
            prev.get("signal_counts") or {},
        ),
        "ship_overdue_estimated": _numeric_delta(
            current, prev, "ship_overdue_estimated"
        ),
        "agent_active": _numeric_delta(current, prev, "agent_active"),
        "disposition_today": _dict_delta(
            current.get("disposition_today") or {},
            prev.get("disposition_today") or {},
        ),
        "tasks_completed_today": _dict_delta(
            current.get("tasks_completed_today") or {},
            prev.get("tasks_completed_today") or {},
        ),
        "category_mapping": _dict_delta(
            current.get("category_mapping") or {},
            prev.get("category_mapping") or {},
        ),
    }


def _queue_counts_from_cache() -> dict[str, int] | None:
    from app.services.orders.line_cache import load_all_lines
    from app.services.orders.queue_filters import resolve_order_queue

    all_lines = load_all_lines()
    if not all_lines:
        return None
    keys = (
        "pending_procurement",
        "pending_payment",
        "ordered",
        "shipped",
        "in_warehouse",
        "dispatched",
        "exception",
        "reverse",
    )
    counts = {k: 0 for k in keys}
    for row in all_lines.values():
        q = resolve_order_queue(row)
        if q and q in counts:
            counts[q] += 1
    counts["all"] = sum(counts[k] for k in keys)
    return counts


def _briefing_cache_needs_sync(*, force: bool) -> bool:
    from app.services.orders.line_cache import load_all_lines, load_sync_state

    if not load_all_lines():
        return True
    if force:
        return True
    state = load_sync_state()
    at = _parse_iso(state.get("last_incremental_at"))
    if not at:
        return True
    age = (datetime.now(timezone.utc) - at).total_seconds()
    return age > _BRIEFING_SYNC_STALE_SEC


def ensure_order_cache_for_briefing(*, force: bool = False) -> dict[str, Any]:
    """简报前置：增量拉 Admin 订单写入 line_cache，与汇总解耦。"""
    from app.services.orders import line_cache, order_line_sync

    if not _briefing_cache_needs_sync(force=force):
        return _orders_sync_meta(skipped=True)

    pages = 2 if force else 1
    result = order_line_sync.sync_orders_incremental(pages=pages)
    cache_total = int(result.get("cache_total") or len(line_cache.load_all_lines()))
    if cache_total <= 0:
        errors = result.get("errors") if isinstance(result.get("errors"), list) else []
        detail = str(errors[0]) if errors else "订单同步失败"
        raise RuntimeError(detail)
    return _orders_sync_meta(skipped=False, sync_result=result)


def _orders_sync_meta(
    *,
    skipped: bool,
    sync_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from app.services.orders.line_cache import load_all_lines, load_sync_state

    state = load_sync_state()
    meta: dict[str, Any] = {
        "orders_sync": "cached" if skipped else "fresh",
        "orders_synced_at": state.get("last_incremental_at"),
        "cache_total": int(state.get("cached_total") or len(load_all_lines())),
    }
    if not skipped and sync_result:
        stats = sync_result.get("stats") if isinstance(sync_result.get("stats"), dict) else {}
        meta["sync_added"] = int(stats.get("added") or 0)
        meta["sync_updated"] = int(stats.get("updated") or 0)
    return meta


def build_briefing_facts() -> dict[str, Any]:
    cached = _queue_counts_from_cache()
    if not cached:
        raise RuntimeError("订单缓存为空，请稍后重试")

    counts = cached
    orders_source = "line_cache"

    queue_counts = {
        k: int(counts.get(k) or 0)
        for k in (
            "pending_procurement",
            "pending_payment",
            "ordered",
            "shipped",
            "in_warehouse",
            "dispatched",
            "exception",
            "reverse",
            "all",
        )
    }

    rows, per_queue_scan = scan_ord_lines_for_signals(
        queue_counts,
        max_per_queue=BRIEFING_MAX_PER_QUEUE,
    )
    scan_stats = aggregate_signal_stats(rows)

    from app.services.tasks.store import get_agent_operation_stats

    agent_ops = get_agent_operation_stats()

    facts: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "timezone": "Asia/Shanghai",
        "orders_source": orders_source,
        "queue_counts": queue_counts,
        "exception_bands": scan_stats["exception_bands"],
        "signal_counts": scan_stats["signal_counts"],
        "ship_overdue_estimated": scan_stats["ship_overdue_estimated"],
        "scanned_rows": len(rows),
        "per_queue_scan": per_queue_scan,
        "agent_active": int(agent_ops.get("active") or 0),
        "agent_ops_by_type": agent_ops.get("by_type") or [],
        "disposition_today": _disposition_today_counts(),
        "tasks_completed_today": _tasks_completed_today(),
        "category_mapping": _category_mapping_stats(),
        "yesterday_carryover": _count_yesterday_carryover(rows),
    }
    return facts


def _get_facts_cached(*, force: bool = False, assume_cache_ready: bool = False) -> dict[str, Any]:
    if not force:
        cached = _facts_cache.get("value")
        if (
            cached
            and _time.monotonic() - float(_facts_cache.get("at") or 0.0) < _FACTS_CACHE_TTL
        ):
            return cached  # type: ignore[return-value]
    if not assume_cache_ready:
        ensure_order_cache_for_briefing(force=force)
    facts = build_briefing_facts()
    _facts_cache["value"] = facts
    _facts_cache["at"] = _time.monotonic()
    return facts


def get_command_center_stats(*, force: bool = False) -> dict[str, Any]:
    facts = _get_facts_cached(force=force)
    signal_counts = dict(facts.get("signal_counts") or {})
    neg = int(signal_counts.pop("NEGATIVE_MARGIN", 0) or 0)
    if neg:
        signal_counts["PAY_AMOUNT_GAP"] = int(signal_counts.get("PAY_AMOUNT_GAP") or 0) + neg
    return {
        "generated_at": facts.get("generated_at"),
        "queue_counts": facts.get("queue_counts") or {},
        "signal_counts": signal_counts,
        "exception_bands": facts.get("exception_bands") or {},
        "ship_overdue_estimated": facts.get("ship_overdue_estimated") or 0,
        "scanned_rows": facts.get("scanned_rows") or 0,
        "per_queue_scan": facts.get("per_queue_scan") or {},
        "orders_source": facts.get("orders_source"),
    }


def get_briefing_payload(*, force: bool = False) -> dict[str, Any]:
    snapshot = _load_snapshot()
    facts = _get_facts_cached(force=force)
    delta = compute_delta(facts, snapshot)
    return {"facts": facts, "delta": delta, "snapshot_at": snapshot.get("generated_at") if snapshot else None}


def _stream_text_chunks(text: str, *, chunk_size: int = 48) -> Iterator[str]:
    for i in range(0, len(text), chunk_size):
        yield text[i : i + chunk_size]


def _iter_bg_task(
    fn,
    *,
    timeout_error: str,
) -> Iterator[tuple[str, Any] | str]:
    """后台跑 fn，期间 yield keepalive；结束时 yield ('ok', value) 或 ('err', exc)。"""
    import threading
    from queue import Empty, Queue

    result_queue: Queue[tuple[str, Any]] = Queue(maxsize=1)

    def _run() -> None:
        try:
            result_queue.put(("ok", fn()))
        except Exception as exc:  # noqa: BLE001
            result_queue.put(("err", exc))

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()

    while True:
        try:
            yield result_queue.get(timeout=5)
            return
        except Empty:
            yield ": keepalive\n\n"
            if not worker.is_alive() and result_queue.empty():
                yield ("err", RuntimeError(timeout_error))
                return


def stream_briefing(*, force: bool = False) -> Iterator[str]:
    """SSE 事件流：sync → meta → facts → llm / t / error，结束 [DONE]。"""
    from app.core.config import get_settings

    orders_meta: dict[str, Any] | None = None
    if _briefing_cache_needs_sync(force=force):
        yield f'data: {json.dumps({"phase": "sync"}, ensure_ascii=False)}\n\n'
        for item in _iter_bg_task(
            lambda: ensure_order_cache_for_briefing(force=force),
            timeout_error="订单同步超时",
        ):
            if isinstance(item, str):
                yield item
                continue
            kind, value = item
            if kind == "err":
                err = json.dumps({"error": str(value)}, ensure_ascii=False)
                yield f"data: {err}\n\n"
                yield "data: [DONE]\n\n"
                return
            orders_meta = value if isinstance(value, dict) else None
    else:
        orders_meta = ensure_order_cache_for_briefing(force=force)

    if orders_meta:
        yield f'data: {json.dumps({"meta": orders_meta}, ensure_ascii=False)}\n\n'

    yield f'data: {json.dumps({"phase": "facts"}, ensure_ascii=False)}\n\n'

    def _build_payload() -> dict[str, Any]:
        snapshot = _load_snapshot()
        facts = _get_facts_cached(force=force, assume_cache_ready=True)
        return {"facts": facts, "delta": compute_delta(facts, snapshot)}

    payload: dict[str, Any] | None = None
    for item in _iter_bg_task(_build_payload, timeout_error="简报数据汇总超时"):
        if isinstance(item, str):
            yield item
            continue
        kind, value = item
        if kind == "err":
            err = json.dumps({"error": str(value)}, ensure_ascii=False)
            yield f"data: {err}\n\n"
            yield "data: [DONE]\n\n"
            return
        payload = value

    if payload is None:
        err = json.dumps({"error": "简报数据汇总超时"}, ensure_ascii=False)
        yield f"data: {err}\n\n"
        yield "data: [DONE]\n\n"
        return

    settings = get_settings()
    use_llm = settings.llm_configured
    full_text: list[str] = []

    if use_llm:
        yield f'data: {json.dumps({"phase": "llm"}, ensure_ascii=False)}\n\n'
        messages = build_briefing_messages(facts=payload["facts"], delta=payload["delta"])
        try:
            for chunk in chat_completion_stream(
                messages, temperature=0.2, max_tokens=1200
            ):
                if not chunk:
                    continue
                full_text.append(chunk)
                event = json.dumps({"t": chunk}, ensure_ascii=False)
                yield f"data: {event}\n\n"
        except Exception:
            use_llm = False
            full_text.clear()

    if not use_llm or not full_text:
        if not full_text:
            yield f'data: {json.dumps({"phase": "llm"}, ensure_ascii=False)}\n\n'
        fallback = render_briefing_fallback(facts=payload["facts"], delta=payload["delta"])
        full_text = [fallback]
        for chunk in _stream_text_chunks(fallback):
            event = json.dumps({"t": chunk}, ensure_ascii=False)
            yield f"data: {event}\n\n"

    if full_text:
        save_briefing_snapshot(payload["facts"])

    yield "data: [DONE]\n\n"

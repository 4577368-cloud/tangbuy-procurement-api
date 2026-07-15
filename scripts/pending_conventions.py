"""在线品类共识：确认/纠错只加票，达门槛后 soft-boost / 晋级，不单次污染主惯例。

政策默认（可调常量）：
- soft：同 term→cid 窗口内 support≥3 且 conflict_rate < 20%
- promote 候选：support≥5 且 conflict_rate < 20% → 写入 live-conventions（不改 Excel 主文件）
- 冲突：conflict_rate ≥ 20% 且票数够 → 标 conflict，仅复盘不加分
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from category_heuristics import _history_title_terms, _title_blob

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "category"
PENDING_FILE = DATA / "pending-conventions.json"
LIVE_CONVENTIONS_FILE = DATA / "live-conventions.json"
GOODS_SOFT_FILE = DATA / "goods-id-soft.json"

SOFT_BOOST_MIN = 3
PROMOTE_MIN = 5
CONFLICT_RATE_MAX = 0.20
WINDOW_DAYS = 7
MAX_EVENTS_PER_TERM = 80

_lock = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_at(raw: Any) -> Optional[datetime]:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _empty_pending() -> dict[str, Any]:
    return {"version": 1, "terms": {}, "updated_at": None}


def _empty_live() -> dict[str, Any]:
    return {
        "version": 1,
        "term_to_categories": {},
        "dominant": {},
        "updated_at": None,
    }


def _empty_goods_soft() -> dict[str, Any]:
    return {"version": 1, "by_goods_id": {}, "updated_at": None}


def _read_json(path: Path, fallback: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return dict(fallback)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else dict(fallback)
    except (OSError, json.JSONDecodeError):
        return dict(fallback)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


@lru_cache(maxsize=1)
def load_pending_conventions() -> dict:
    return _read_json(PENDING_FILE, _empty_pending())


@lru_cache(maxsize=1)
def load_live_conventions() -> dict:
    return _read_json(LIVE_CONVENTIONS_FILE, _empty_live())


@lru_cache(maxsize=1)
def load_goods_id_soft() -> dict:
    return _read_json(GOODS_SOFT_FILE, _empty_goods_soft())


def clear_pending_caches() -> None:
    load_pending_conventions.cache_clear()
    load_live_conventions.cache_clear()
    load_goods_id_soft.cache_clear()


def _window_cutoff() -> datetime:
    return _now() - timedelta(days=WINDOW_DAYS)


def _cid_stats(events: list[dict[str, Any]], cid: str, cutoff: datetime) -> tuple[int, int]:
    support = 0
    conflict = 0
    for ev in events:
        at = _parse_at(ev.get("at"))
        if at is not None and at < cutoff:
            continue
        kind = str(ev.get("kind") or "")
        ev_cid = str(ev.get("cid") or "")
        if kind in ("confirm", "correct") and ev_cid == cid:
            support += 1
        elif kind in ("reject", "correct_away") and ev_cid == cid:
            conflict += 1
    return support, conflict


def _recompute_term(term_row: dict[str, Any]) -> None:
    events = list(term_row.get("events") or [])
    cutoff = _window_cutoff()
    by_cid: dict[str, dict[str, Any]] = dict(term_row.get("by_cid") or {})
    known_cids = set(by_cid.keys()) | {str(ev.get("cid") or "") for ev in events if ev.get("cid")}
    known_cids.discard("")

    conflict_flag = False
    for cid in known_cids:
        support, conflict = _cid_stats(events, cid, cutoff)
        total = support + conflict
        rate = (conflict / total) if total else 0.0
        prev = dict(by_cid.get(cid) or {})
        if total >= SOFT_BOOST_MIN and rate >= CONFLICT_RATE_MAX:
            status = "conflict"
            conflict_flag = True
        elif support >= PROMOTE_MIN and rate < CONFLICT_RATE_MAX:
            status = "promoted"
        elif support >= SOFT_BOOST_MIN and rate < CONFLICT_RATE_MAX:
            status = "soft"
        else:
            status = "pending"
        prev.update(
            {
                "category_id": int(cid) if cid.isdigit() else cid,
                "support": support,
                "conflict": conflict,
                "conflict_rate": round(rate, 3),
                "status": status,
            }
        )
        by_cid[cid] = prev
    term_row["by_cid"] = by_cid
    term_row["conflict_queue"] = conflict_flag


def record_vote(
    *,
    title: str,
    cid: str,
    kind: str,
    hs: Optional[dict[str, Any]] = None,
    original_cid: Optional[str] = None,
    goods_id: Optional[str] = None,
    at: Optional[str] = None,
    matched_keywords: Optional[list[str]] = None,
) -> dict[str, Any]:
    """加票。kind: confirm | correct | reject。纠错时对 original 记 correct_away。"""
    cid = str(cid or "").strip()
    if not cid and kind != "reject":
        return {"ok": False, "reason": "missing_cid"}
    stamp = at or _now().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    terms = list(_history_title_terms(title or ""))
    if matched_keywords:
        for kw in matched_keywords:
            t = str(kw or "").strip()
            if len(t) >= 2 and t not in terms:
                terms.append(t)
    terms = terms[:12]
    if not terms and not goods_id:
        return {"ok": False, "reason": "no_terms"}

    with _lock:
        data = _read_json(PENDING_FILE, _empty_pending())
        term_map: dict[str, Any] = dict(data.get("terms") or {})

        def _push(term: str, event: dict[str, Any], boost_cid: Optional[str], hs_snap: Optional[dict]) -> None:
            row = dict(term_map.get(term) or {"events": [], "by_cid": {}})
            events = list(row.get("events") or [])
            events.append(event)
            row["events"] = events[-MAX_EVENTS_PER_TERM:]
            if boost_cid and hs_snap:
                by_cid = dict(row.get("by_cid") or {})
                slot = dict(by_cid.get(boost_cid) or {})
                for key in ("category_cn_name", "declare_cn_name", "hs_code", "declare_en_name"):
                    val = hs_snap.get(key)
                    if val not in (None, ""):
                        slot[key] = val
                slot["category_id"] = int(boost_cid) if boost_cid.isdigit() else boost_cid
                slot["last_at"] = stamp
                by_cid[boost_cid] = slot
                row["by_cid"] = by_cid
            _recompute_term(row)
            term_map[term] = row

        raw_hs = hs if isinstance(hs, dict) else {}
        ch = raw_hs.get("corrected_hs") if isinstance(raw_hs.get("corrected_hs"), dict) else {}
        hs_snap = {
            "category_cn_name": ch.get("category_cn_name") or raw_hs.get("category_cn_name"),
            "declare_cn_name": ch.get("declare_cn_name") or raw_hs.get("declare_cn_name"),
            "hs_code": ch.get("hs_code") or raw_hs.get("hs_code"),
            "declare_en_name": raw_hs.get("declare_en_name"),
        }

        for term in terms:
            if kind in ("confirm", "correct") and cid:
                _push(
                    term,
                    {"at": stamp, "kind": kind, "cid": cid, "title": (title or "")[:80]},
                    cid,
                    hs_snap,
                )
            if kind == "correct" and original_cid and str(original_cid) != cid:
                _push(
                    term,
                    {
                        "at": stamp,
                        "kind": "correct_away",
                        "cid": str(original_cid),
                        "title": (title or "")[:80],
                    },
                    None,
                    None,
                )
            if kind == "reject" and (original_cid or cid):
                away = str(original_cid or cid)
                _push(
                    term,
                    {"at": stamp, "kind": "reject", "cid": away, "title": (title or "")[:80]},
                    None,
                    None,
                )

        data["terms"] = term_map
        data["updated_at"] = stamp
        _write_json(PENDING_FILE, data)

        soft_promoted = _sync_live_conventions(term_map, stamp)
        if goods_id and kind in ("confirm", "correct") and cid:
            _upsert_goods_soft(str(goods_id), cid, hs_snap, stamp, kind=kind)

        clear_pending_caches()
        return {
            "ok": True,
            "terms": terms,
            "live_updated": soft_promoted,
            "at": stamp,
        }


def _sync_live_conventions(term_map: dict[str, Any], stamp: str) -> int:
    """仅把 status=promoted 的 term→cid 写入 live-conventions，供 suggest soft/强 boost。"""
    live = _empty_live()
    term_to: dict[str, list[dict]] = {}
    dominant: dict[str, dict] = {}
    count = 0
    for term, row in term_map.items():
        by_cid = row.get("by_cid") or {}
        ranked = sorted(
            (
                (cid, meta)
                for cid, meta in by_cid.items()
                if str(meta.get("status") or "") in ("soft", "promoted")
            ),
            key=lambda x: (-int(x[1].get("support") or 0), x[0]),
        )
        if not ranked:
            continue
        rows_out: list[dict] = []
        total_support = sum(int(m.get("support") or 0) for _, m in ranked) or 1
        for cid, meta in ranked:
            support = int(meta.get("support") or 0)
            share = round(support / total_support, 3)
            item = {
                "category_id": meta.get("category_id") or (int(cid) if cid.isdigit() else cid),
                "category_cn_name": meta.get("category_cn_name") or "",
                "declare_cn_name": meta.get("declare_cn_name") or "",
                "hs_code": meta.get("hs_code") or "",
                "count": support,
                "share": share,
                "status": meta.get("status"),
                "conflict_rate": meta.get("conflict_rate") or 0,
                "source": "pending_consensus",
            }
            rows_out.append(item)
            if meta.get("status") == "promoted" and share >= 0.35:
                dominant[term] = {
                    **item,
                    "support": total_support,
                    "summary": (
                        f"在线共识「{term}」→「{item['category_cn_name'] or cid}」"
                        f"（{support} 票，冲突率 {int(float(item['conflict_rate']) * 100)}%）"
                    ),
                }
                count += 1
        if rows_out:
            term_to[term] = rows_out
    live["term_to_categories"] = term_to
    live["dominant"] = dominant
    live["updated_at"] = stamp
    live["term_count"] = len(term_to)
    live["dominant_count"] = len(dominant)
    _write_json(LIVE_CONVENTIONS_FILE, live)
    return count


def _upsert_goods_soft(
    goods_id: str,
    cid: str,
    hs_snap: dict[str, Any],
    stamp: str,
    *,
    kind: str,
) -> None:
    gid = "".join(ch for ch in goods_id if ch.isdigit())
    if not gid:
        return
    data = _read_json(GOODS_SOFT_FILE, _empty_goods_soft())
    by_g = dict(data.get("by_goods_id") or {})
    prev = dict(by_g.get(gid) or {})
    prev_cid = str(prev.get("category_id") or "")
    support = int(prev.get("support") or 0)
    if prev_cid == cid:
        support += 1
    else:
        support = 1
    by_g[gid] = {
        "category_id": int(cid) if cid.isdigit() else cid,
        "support": support,
        "hs_code": hs_snap.get("hs_code") or prev.get("hs_code"),
        "category_cn_name": hs_snap.get("category_cn_name") or prev.get("category_cn_name"),
        "declare_cn_name": hs_snap.get("declare_cn_name") or prev.get("declare_cn_name"),
        "last_at": stamp,
        "last_kind": kind,
        # 单票不得当 history_hit；仅 soft 提示
        "soft_only": True,
    }
    data["by_goods_id"] = by_g
    data["updated_at"] = stamp
    _write_json(GOODS_SOFT_FILE, data)


def lookup_goods_id_soft(goods_id: str) -> Optional[dict[str, Any]]:
    gid = "".join(ch for ch in (goods_id or "") if ch.isdigit())
    if not gid:
        return None
    row = (load_goods_id_soft().get("by_goods_id") or {}).get(gid)
    return dict(row) if isinstance(row, dict) else None


def lookup_pending_conventions_for_text(
    title: str,
    vision_keywords: list[str] | None = None,
) -> list[dict]:
    """命中 soft/promoted 在线共识，供 suggest soft-boost。"""
    live = load_live_conventions()
    dominant = live.get("dominant") or {}
    term_map = live.get("term_to_categories") or {}
    blob = _title_blob(title, vision_keywords)
    hits: list[dict] = []
    seen: set[str] = set()

    for term, dom in dominant.items():
        if term not in blob:
            continue
        cid = str(dom.get("category_id"))
        if not cid or cid in seen:
            continue
        seen.add(cid)
        hits.append({**dom, "term": term, "strength": "pending_promoted"})

    for term, rows in term_map.items():
        if term not in blob:
            continue
        for row in (rows or [])[:2]:
            cid = str(row.get("category_id"))
            if not cid or cid in seen:
                continue
            status = str(row.get("status") or "")
            if status not in ("soft", "promoted"):
                continue
            if int(row.get("count") or 0) < SOFT_BOOST_MIN:
                continue
            seen.add(cid)
            hits.append(
                {
                    **row,
                    "term": term,
                    "strength": "pending_soft" if status == "soft" else "pending_promoted",
                    "summary": row.get("summary")
                    or (
                        f"在线共识「{term}」常见「{row.get('category_cn_name') or cid}」"
                        f"（{row.get('count')} 票）"
                    ),
                }
            )

    hits.sort(
        key=lambda x: (
            1 if x.get("strength") == "pending_promoted" else 0,
            float(x.get("share") or 0) * int(x.get("count") or 0),
        ),
        reverse=True,
    )
    return hits[:6]


def ingest_feedback_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """由 category feedback.jsonl / API 条目驱动加票。"""
    if not isinstance(entry, dict):
        return {"ok": False, "reason": "invalid"}
    rejected = bool(entry.get("rejected"))
    confirmed = bool(entry.get("confirmed"))
    orig = str(entry.get("original_category_id") or "").strip()
    cid = str(entry.get("corrected_category_id") or "").strip()
    title = str(entry.get("source_title") or "")
    goods_id = str(entry.get("goods_id") or entry.get("source_goods_id") or "").strip() or None
    hs = {
        "category_cn_name": (entry.get("corrected_hs") or {}).get("category_cn_name")
        if isinstance(entry.get("corrected_hs"), dict)
        else entry.get("category_cn_name"),
        "declare_cn_name": (entry.get("corrected_hs") or {}).get("declare_cn_name")
        if isinstance(entry.get("corrected_hs"), dict)
        else entry.get("declare_cn_name"),
        "hs_code": (entry.get("corrected_hs") or {}).get("hs_code")
        if isinstance(entry.get("corrected_hs"), dict)
        else entry.get("hs_code"),
        "corrected_hs": entry.get("corrected_hs"),
    }
    if rejected:
        return record_vote(
            title=title,
            cid=orig or cid,
            kind="reject",
            original_cid=orig or cid,
            at=str(entry.get("created_at") or "") or None,
            matched_keywords=list(entry.get("matched_keywords") or []),
        )
    if confirmed or (cid and cid == orig):
        return record_vote(
            title=title,
            cid=cid or orig,
            kind="confirm",
            hs=hs,
            goods_id=goods_id,
            at=str(entry.get("created_at") or "") or None,
            matched_keywords=list(entry.get("matched_keywords") or []),
        )
    if cid:
        return record_vote(
            title=title,
            cid=cid,
            kind="correct",
            hs=hs,
            original_cid=orig or None,
            goods_id=goods_id,
            at=str(entry.get("created_at") or "") or None,
            matched_keywords=list(entry.get("matched_keywords") or []),
        )
    return {"ok": False, "reason": "unrecognized"}

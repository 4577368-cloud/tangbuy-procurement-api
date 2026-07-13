"""任务中心演示种子（对齐 task-store.ts ensureTaskSeeds）。"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Optional

from app.core.paths import data_dir
from app.services.products.store import load_products

INQUIRY_WAIT_MS = 20 * 60 * 1000


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _short_name(product: dict[str, Any]) -> str:
    name = re.sub(r"^[A-Za-z]{1,3}\d+\s*", "", product.get("product_name", "")).strip()
    name = name or product.get("product_name", "")
    return name[:18]


def _auto_release_status(review_status: str) -> str:
    if review_status == "confirmed":
        return "completed"
    if review_status == "flagged":
        return "needs_review"
    return "needs_review"


def _build_auto_release_seeds() -> list[dict[str, Any]]:
    path = data_dir() / "agent" / "auto-releases.json"
    if not path.exists():
        return []
    try:
        releases = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    tasks: list[dict[str, Any]] = []
    for r in releases:
        if not isinstance(r, dict):
            continue
        passed = sum(1 for c in r.get("conditions", []) if c.get("passed"))
        total = len(r.get("conditions", []))
        review = r.get("review_status", "pending")
        status = _auto_release_status(review)
        timeline = [{"at": r["released_at"], "label": "自动下单执行", "detail": r.get("summary", "")}]
        if r.get("reviewed_at"):
            timeline.append(
                {
                    "at": r["reviewed_at"],
                    "label": "人工复核",
                    "detail": r.get("reviewer_note") or "",
                }
            )
        tasks.append(
            {
                "id": f"task-{r['release_id']}",
                "type": "auto_release",
                "skill_id": "procurement-agent",
                "skill_name": r.get("agent_label", "采购准入 Agent"),
                "title": r.get("product_title", ""),
                "subtitle": r.get("external_order_no", ""),
                "status": status,
                "created_at": r["released_at"],
                "updated_at": r.get("reviewed_at") or r["released_at"],
                "completed_at": r.get("reviewed_at") if review == "confirmed" else None,
                "external_ref": r.get("release_id"),
                "order_no": r.get("external_order_no"),
                "payload": {
                    "release_id": r.get("release_id"),
                    "release_type": r.get("release_type"),
                    "agent_label": r.get("agent_label"),
                    "conditions_passed": passed,
                    "conditions_total": total,
                },
                "timeline": timeline,
                "result_summary": r.get("summary", ""),
            }
        )
    return tasks


def _mapping_mapped_at(prod: dict[str, Any], m: dict[str, Any]) -> str:
    """Agent 产出映射建议的时间（禁止用 now 兜底，避免时间线倒挂）。"""
    return (
        str(m.get("mapped_at") or "").strip()
        or str(prod.get("mapping_mapped_at") or "").strip()
        or str(m.get("reviewed_at") or "").strip()
    )


def _is_auto_mapping(prod: dict[str, Any], m: dict[str, Any]) -> bool:
    if m.get("auto_resolved") is True:
        return True
    if prod.get("category_status") == "auto_passed":
        return True
    wb = m.get("admin_writeback") if isinstance(m.get("admin_writeback"), dict) else {}
    if str(wb.get("resolution") or m.get("resolution") or "").strip() == "auto":
        return True
    method = str(m.get("match_method") or "").strip()
    return method in ("history_goods_id", "history_similar", "local_item_mapped", "admin_existing")


def _review_timeline_event(
    prod: dict[str, Any],
    m: dict[str, Any],
    *,
    review: str,
    review_note: Optional[str],
) -> Optional[dict[str, str]]:
    reviewed_at = str(m.get("reviewed_at") or "").strip()
    if not reviewed_at or review == "pending":
        return None

    auto = _is_auto_mapping(prod, m)
    if review == "corrected":
        label = "人工修正"
        detail = review_note or f"修正为「{prod.get('category', '')}」"
    elif review == "rejected":
        label = "已驳回"
        detail = review_note or ""
    elif review == "confirmed" and auto:
        label = "自动通过"
        conf = m.get("agent_confidence")
        method = str(m.get("match_method") or "").strip()
        bits = []
        if conf is not None:
            try:
                bits.append(f"置信 {int(float(conf) * 100)}%")
            except (TypeError, ValueError):
                pass
        if method:
            bits.append(method)
        detail = " · ".join(bits) if bits else (review_note or "质量门禁通过")
    elif review == "confirmed":
        label = "人工确认"
        detail = review_note or "确认无误"
    else:
        return None
    return {"at": reviewed_at, "label": label, "detail": detail}


def _writeback_timeline_event(wb: Any) -> Optional[dict[str, str]]:
    if not isinstance(wb, dict):
        return None
    status = str(wb.get("status") or "").strip()
    at = str(wb.get("at") or "").strip()
    if not status or not at:
        return None
    labels = {
        "writing": "写入 Admin",
        "ok": "已写入 Admin",
        "failed": "写入失败",
        "skipped": "无需写入",
    }
    label = labels.get(status)
    if not label:
        return None
    detail = ""
    if status == "ok":
        fr = str(wb.get("from_category") or "").strip()
        to = str(wb.get("to_category") or "").strip()
        if fr and to and fr != to:
            detail = f"{fr} → {to}"
        elif to:
            detail = to
    elif status == "failed":
        detail = str(wb.get("error") or "").strip()
    elif status == "skipped":
        detail = str(wb.get("reason") or "").strip()
    return {"at": at, "label": label, "detail": detail}


def _build_mapping_timeline(prod: dict[str, Any], m: dict[str, Any]) -> list[dict[str, str]]:
    events: list[dict[str, str]] = []
    mapped_at = _mapping_mapped_at(prod, m)
    if mapped_at:
        events.append(
            {
                "at": mapped_at,
                "label": "生成映射建议",
                "detail": m.get("suggested_category_path") or prod.get("category", ""),
            }
        )
    review = m.get("review_status", "pending")
    review_note = m.get("reviewer_note")
    rev = _review_timeline_event(prod, m, review=str(review), review_note=review_note)
    if rev:
        events.append(rev)
    wb_evt = _writeback_timeline_event(m.get("admin_writeback"))
    if wb_evt:
        events.append(wb_evt)
    events.sort(key=lambda e: e.get("at", ""))
    return events


def _build_category_mapping_seeds(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    for prod in products:
        m = prod.get("mapping_record")
        if not m:
            continue
        checks = m.get("checks") or []
        passed = sum(1 for c in checks if c.get("passed"))
        review = m.get("review_status", "pending")
        match_status = m.get("match_status", "")
        if review in ("confirmed", "corrected"):
            status = "completed"
        elif match_status in ("unmapped", "mismatch"):
            status = "in_progress"
        else:
            status = "needs_review"
        # 已完结的映射不进任务中心列表，避免淹没催单/询盘等长程任务
        if status == "completed":
            continue
        review_note = m.get("reviewer_note")
        if not review_note and review == "corrected":
            review_note = f"人工修正为「{prod.get('category', '')}」"
        mapped_at = _mapping_mapped_at(prod, m)
        timeline = _build_mapping_timeline(prod, m)
        auto = _is_auto_mapping(prod, m)
        wb = m.get("admin_writeback") if isinstance(m.get("admin_writeback"), dict) else {}
        suffix = "（人工修正）" if review == "corrected" else ""
        tasks.append(
            {
                "id": f"task-{m.get('mapping_id', prod.get('tangbuy_product_id'))}",
                "type": "category_mapping",
                "skill_id": "category-mapping-agent",
                "skill_name": "品类映射 Agent",
                "title": prod.get("product_name", ""),
                "subtitle": prod.get("category", ""),
                "status": status,
                "created_at": mapped_at or m.get("reviewed_at") or _iso(_now()),
                "updated_at": m.get("reviewed_at") or wb.get("at") or mapped_at or _iso(_now()),
                "completed_at": m.get("reviewed_at")
                if review in ("confirmed", "corrected")
                else None,
                "external_ref": m.get("mapping_id"),
                "order_no": prod.get("tangbuy_product_id"),
                "payload": {
                    "mapping_id": m.get("mapping_id"),
                    "match_status": match_status,
                    "suggested_category_path": m.get("suggested_category_path") or prod.get("category"),
                    "checks_passed": passed,
                    "checks_total": len(checks),
                    "auto_resolved": auto,
                    "writeback_status": wb.get("status"),
                },
                "timeline": timeline,
                "result_summary": f"{prod.get('category', '')}{suffix}",
            }
        )
    return tasks


def _build_demo_inquiry_seeds(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not products:
        return []
    now = _now()
    dresses = [
        p
        for p in products
        if re.search(r"连衣裙|长袍|女装", p.get("category", ""))
        or re.search(r"连衣裙|长袍|阿巴亚|abaya", p.get("product_name", ""), re.I)
    ]
    shoes = [
        p
        for p in products
        if re.search(r"鞋|拖", p.get("category", "")) or re.search(r"鞋|拖", p.get("product_name", ""))
    ]
    p_ready = dresses[0] if dresses else products[0]
    p_wait = shoes[0] if len(shoes) > 0 else (products[1] if len(products) > 1 else products[0])

    ago25m = _iso(now - timedelta(minutes=25))
    ago10m = _iso(now - timedelta(minutes=10))
    ago5m = _iso(now - timedelta(minutes=5))
    ready_at10 = _iso(now - timedelta(minutes=10) + timedelta(milliseconds=INQUIRY_WAIT_MS))

    tasks: list[dict[str, Any]] = [
        {
            "id": "task-inquiry-demo-ready",
            "type": "inquiry_1688",
            "skill_id": "inquiry-1688",
            "skill_name": "1688 商家询盘",
            "title": f"{_short_name(p_ready)} — 起订量多少？",
            "subtitle": f"offer/{p_ready.get('source_product_id')}",
            "status": "ready",
            "created_at": ago25m,
            "updated_at": ago25m,
            "external_ref": "demo-task-ready-001",
            "payload": {
                "alphashop_task_id": "demo-task-ready-001",
                "item": p_ready.get("source_url"),
                "question": "起订量多少？最低批发价？",
                "quantity": "10",
                "query_available_at": ago5m,
            },
            "timeline": [
                {"at": ago25m, "label": "提交询盘", "detail": "等待商家回复（约 20 分钟）"},
                {"at": ago5m, "label": "进入可查询", "detail": "可一键拉取商家回复"},
            ],
        },
        {
            "id": "task-inquiry-demo-wait",
            "type": "inquiry_1688",
            "skill_id": "inquiry-1688",
            "skill_name": "1688 商家询盘",
            "title": f"{_short_name(p_wait)} — 能否定制？",
            "subtitle": f"offer/{p_wait.get('source_product_id')}",
            "status": "in_progress",
            "created_at": ago10m,
            "updated_at": ago10m,
            "external_ref": "demo-task-wait-002",
            "payload": {
                "alphashop_task_id": "demo-task-wait-002",
                "item": p_wait.get("source_url"),
                "question": "能否定制？MOQ 多少？",
                "quantity": "100",
                "query_available_at": ready_at10,
            },
            "timeline": [{"at": ago10m, "label": "提交询盘", "detail": "等待商家回复"}],
        },
    ]

    consult_pool = dresses if len(dresses) >= 2 else products
    picks = consult_pool[:3]
    if picks:
        consult_created = _iso(now - timedelta(minutes=18))
        consult_done = _iso(now - timedelta(minutes=9))
        lines = []
        for p in picks:
            tiers = p.get("tier_prices") or [{}]
            moq = tiers[0].get("min_qty", 1) if tiers else 1
            lines.append(
                f"- **{_short_name(p)}** — ¥{p.get('tangbuy_unit_price')}/件，"
                f"起订 {moq} 件，月销 {p.get('sold_count', 0)}+，{p.get('shop_name', '')}"
            )
        best = picks[0]
        tasks.append(
            {
                "id": "task-newton-demo",
                "type": "newton_agent",
                "skill_id": "newton-cloud",
                "skill_name": "智能咨询",
                "title": "帮我找跨境女装长袍连衣裙，一件代发、包邮",
                "status": "completed",
                "created_at": consult_created,
                "updated_at": consult_done,
                "completed_at": consult_done,
                "external_ref": "demo-newton-001",
                "payload": {
                    "newton_task_id": "demo-newton-001",
                    "question": "帮我找跨境女装长袍连衣裙，一件代发、包邮，广州发货优先",
                    "newton_status": "END",
                    "task_type": "LONG_RUNNING",
                    "messages": [
                        {
                            "type": "text",
                            "content": (
                                f"已为你找到 {len(picks)} 款符合条件的商品（跨境 · 一件代发 · 已入大店）：\n\n"
                                + "\n".join(lines)
                                + f"\n\n**建议**：「{_short_name(best)}」单价与销量综合最优，可优先铺货；"
                                "如需定制可发起询盘确认起订量。"
                            ),
                        }
                    ],
                },
                "timeline": [
                    {"at": consult_created, "label": "已提交咨询", "detail": "牛顿云处理中，通常需要几分钟"},
                    {"at": consult_done, "label": "咨询完成", "detail": f"返回 {len(picks)} 款候选商品与选品建议"},
                ],
                "result_summary": f"返回 {len(picks)} 款候选商品与选品建议",
            }
        )

    src_created = _iso(now - timedelta(minutes=40))
    tasks.append(
        {
            "id": "task-sourcing-demo-ready",
            "type": "sourcing_inquiry",
            "skill_id": "1688-sourcing",
            "skill_name": "1688 寻源询盘",
            "title": f"{_short_name(p_ready)} × 500",
            "subtitle": "跨境女装 · 一件代发 · 广州优先",
            "status": "ready",
            "created_at": src_created,
            "updated_at": src_created,
            "external_ref": "https://s.1688.com/youyuan/index.htm?tab=purchase&demand=demo-src-001",
            "payload": {
                "offer_name": _short_name(p_ready),
                "count": "500",
                "demand": "跨境女装长袍连衣裙，一件代发，广州发货优先，需提供质检报告",
                "requirement_url": "https://s.1688.com/youyuan/index.htm?tab=purchase&demand=demo-src-001",
            },
            "timeline": [
                {"at": src_created, "label": "发起寻源询盘", "detail": "1688 平台匹配供应商中"},
                {"at": src_created, "label": "获取询盘页链接", "detail": "报价与匹配结果在 1688 平台更新"},
            ],
            "result_summary": "询盘已创建，供应商报价在 1688 询盘详情页查看。",
        }
    )

    of_created = _iso(now - timedelta(minutes=55))
    of_updated = _iso(now - timedelta(minutes=20))
    tasks.append(
        {
            "id": "task-followup-demo-wait",
            "type": "order_followup",
            "skill_id": "order-followup",
            "skill_name": "催单",
            "title": "催发货：下单 3 天仍未发货",
            "subtitle": "订单 TI26074801041",
            "status": "needs_review",
            "created_at": of_created,
            "updated_at": of_updated,
            "order_no": "TI26074801041",
            "external_ref": "demo-newton-followup-1",
            "payload": {
                "order_id": "TI26074801041",
                "question": "麻烦确认下发货时间，下单已 3 天还没有物流单号",
                "newton_task_id": "demo-newton-followup-1",
                "newton_status": "WAIT_USER",
                "messages": [
                    {
                        "type": "text",
                        "content": (
                            "您好，这款面料刚到货，预计明天安排发货。"
                            "方便确认下收货仓库是否仍是广州白云仓？确认后优先发出。"
                        ),
                    }
                ],
            },
            "timeline": [
                {"at": of_created, "label": "已向商家发起催单", "detail": "牛顿云长程任务处理中"},
                {"at": of_updated, "label": "商家已回复，待补充", "detail": "商家需确认收货仓库地址"},
            ],
            "result_summary": "商家需确认收货仓库地址后发货",
        }
    )

    of2_created = _iso(now - timedelta(minutes=90))
    of2_done = _iso(now - timedelta(minutes=30))
    tasks.append(
        {
            "id": "task-followup-demo-done",
            "type": "order_followup",
            "skill_id": "order-followup",
            "skill_name": "催单",
            "title": "催发货：确认物流单号",
            "subtitle": "订单 TI26074800883",
            "status": "completed",
            "created_at": of2_created,
            "updated_at": of2_done,
            "completed_at": of2_done,
            "order_no": "TI26074800883",
            "external_ref": "demo-newton-followup-2",
            "payload": {
                "order_id": "TI26074800883",
                "question": "请提供快递单号",
                "newton_task_id": "demo-newton-followup-2",
                "newton_status": "END",
                "messages": [
                    {
                        "type": "text",
                        "content": "已发货，中通快递，单号 78012345678990，预计 3 天到广州仓，请注意查收。",
                    }
                ],
            },
            "timeline": [
                {"at": of2_created, "label": "已向商家发起催单"},
                {"at": of2_done, "label": "商家已回复", "detail": "已提供中通单号 78012345678990"},
            ],
            "result_summary": "商家已发货，中通 78012345678990",
        }
    )

    nc_created = _iso(now - timedelta(minutes=22))
    nc_updated = _iso(now - timedelta(minutes=6))
    tasks.append(
        {
            "id": "task-newton-demo-wait",
            "type": "newton_agent",
            "skill_id": "newton-cloud",
            "skill_name": "智能咨询",
            "title": "帮我找夏季防晒渔夫帽，量大从优",
            "status": "needs_review",
            "created_at": nc_created,
            "updated_at": nc_updated,
            "external_ref": "demo-newton-consult-2",
            "payload": {
                "newton_task_id": "demo-newton-consult-2",
                "question": "帮我找夏季防晒渔夫帽，UPF50+，量大从优",
                "newton_status": "WAIT_USER",
                "content": "为更精准推荐，请补充：目标单价区间、首批采购数量、是否需要定制 LOGO？",
            },
            "timeline": [
                {"at": nc_created, "label": "已提交咨询"},
                {"at": nc_updated, "label": "待补充信息", "detail": "助手需要单价区间与采购数量"},
            ],
            "result_summary": "助手需要补充单价区间与采购数量",
        }
    )
    return tasks


@lru_cache(maxsize=1)
def get_task_seeds() -> tuple[dict[str, Any], ...]:
    products = load_products()
    seeds: list[dict[str, Any]] = []
    seeds.extend(_build_auto_release_seeds())
    seeds.extend(_build_category_mapping_seeds(products))
    seeds.extend(_build_demo_inquiry_seeds(products))
    seeds.sort(key=lambda t: t.get("created_at", ""), reverse=True)
    return tuple(seeds)


def _is_demo_seed_task(task: dict[str, Any]) -> bool:
    """演示种子（demo-newton 等）不进入正式任务列表。"""
    tid = str(task.get("id") or "")
    if tid in ("task-newton-demo",) or tid.startswith("task-demo-"):
        return True
    ref = str(task.get("external_ref") or "")
    if ref.startswith("demo-"):
        return True
    payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
    for key in ("newton_task_id", "alphashop_task_id"):
        val = str(payload.get(key) or "")
        if val.startswith("demo-"):
            return True
    return False


def merge_tasks_with_seeds(runtime: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seeds = [t for t in get_task_seeds() if not _is_demo_seed_task(t)]
    by_id = {t["id"]: t for t in seeds}
    for t in runtime:
        by_id[t["id"]] = t
    merged = list(by_id.values())
    merged.sort(key=lambda t: t.get("created_at", ""), reverse=True)
    return merged

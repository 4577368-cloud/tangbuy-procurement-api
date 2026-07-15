"""商品中心读写（品类映射确认回写）。"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from app.core.paths import data_dir
from app.db.session import db_session, is_db_enabled
from app.services.category_mapping.local_mappings import upsert_local_mapping

_CENTER_PATH = data_dir() / "products" / "center.json"
# RLock：update_product 的 updater 内可能再调用 load_products（同线程），普通 Lock 会死锁
_io_lock = threading.RLock()

RESOLVED_MAPPING_STATUSES = frozenset({"auto_passed", "confirmed", "needs_review"})
PROTECTED_PRODUCT_FIELDS = (
    "hs_mapping",
    "category",
    "category_status",
    "mapping_record",
    "mapping_confidence",
    "mapping_mapped_at",
)
GENERIC_PLATFORM_HINTS = frozenset({"其它", "其他", "待映射", "—", "-", ""})


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _protect_resolved_mapping(product: dict[str, Any], merged: dict[str, Any]) -> dict[str, Any]:
    """已确认品类不被订单同步覆盖。"""
    if product.get("category_status") not in RESOLVED_MAPPING_STATUSES:
        return merged
    out = dict(merged)
    for key in PROTECTED_PRODUCT_FIELDS:
        if key in product:
            out[key] = product[key]
    return out


def _parse_products_text(raw: str) -> list[dict[str, Any]]:
    raw = raw.strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        # 并发写残留下尾时，尽量取首个完整数组
        try:
            data, _end = json.JSONDecoder().raw_decode(raw)
            return data if isinstance(data, list) else []
        except json.JSONDecodeError:
            return []


def load_products() -> list[dict[str, Any]]:
    if is_db_enabled():
        from app.db.catalog_repos import ProductRepository

        with db_session() as session:
            items = ProductRepository(session).load_all()
            return [sanitize_product_mapping_state(p) for p in items]
    with _io_lock:
        if not _CENTER_PATH.exists():
            return []
        try:
            items = _parse_products_text(_CENTER_PATH.read_text(encoding="utf-8"))
            return [sanitize_product_mapping_state(p) for p in items]
        except OSError:
            return []


def save_products(items: list[dict[str, Any]]) -> None:
    if is_db_enabled():
        from app.db.catalog_repos import ProductRepository

        with db_session() as session:
            ProductRepository(session).save_all(items)
        return
    payload = json.dumps(items, ensure_ascii=False, indent=2) + "\n"
    with _io_lock:
        _CENTER_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix="center-",
            suffix=".json.tmp",
            dir=str(_CENTER_PATH.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, _CENTER_PATH)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise


def find_by_source_product_id(source_id: str) -> Optional[dict[str, Any]]:
    if not source_id:
        return None
    if is_db_enabled():
        from app.db.catalog_repos import ProductRepository

        with db_session() as session:
            return ProductRepository(session).find_by_source_product_id(source_id)
    return next((p for p in load_products() if p.get("source_product_id") == source_id), None)


def get_product_by_id(tangbuy_id: str) -> Optional[dict[str, Any]]:
    if not tangbuy_id:
        return None
    if is_db_enabled():
        from app.db.catalog_repos import ProductRepository

        with db_session() as session:
            return ProductRepository(session).get_by_id(tangbuy_id)
    return next((p for p in load_products() if p.get("tangbuy_product_id") == tangbuy_id), None)


def find_product_for_ord_line(row: dict[str, Any]) -> Optional[dict[str, Any]]:
    ord_line = str(row.get("ord_line_no") or "").strip()
    if is_db_enabled() and ord_line:
        from app.db.catalog_repos import ProductRepository

        with db_session() as session:
            found = ProductRepository(session).find_by_ord_line(ord_line)
            if found and not found.get("replaced_by_product_id"):
                return found
    if ord_line:
        for p in load_products():
            lines = p.get("linked_ord_lines") or []
            if ord_line in lines and not p.get("replaced_by_product_id"):
                return p
    item_id = str(row.get("item_id") or "").strip()
    if item_id:
        found = get_product_by_id(item_id)
        if found and not found.get("replaced_by_product_id"):
            return found
    splr_id = str(row.get("splr_item_id") or "").strip()
    if splr_id:
        found = find_by_source_product_id(splr_id)
        if found and not found.get("replaced_by_product_id"):
            return found
    return None


def build_ord_line_product_index() -> dict[str, dict[str, dict[str, Any]]]:
    """一次性加载商品并建立 ord_line / item_id / source_product_id 索引，供批量 enrich。

    替代逐行 find_product_for_ord_line（每行 1~3 次 DB 会话），
    列表/看板批量场景改为一次全量 + 内存查找。
    """
    by_line: dict[str, dict[str, Any]] = {}
    by_item: dict[str, dict[str, Any]] = {}
    by_source: dict[str, dict[str, Any]] = {}
    for p in load_products():
        if p.get("replaced_by_product_id"):
            continue
        for line in p.get("linked_ord_lines") or []:
            key = str(line or "").strip()
            if key and key not in by_line:
                by_line[key] = p
        tid = str(p.get("tangbuy_product_id") or "").strip()
        if tid and tid not in by_item:
            by_item[tid] = p
        sid = str(p.get("source_product_id") or "").strip()
        if sid and sid not in by_source:
            by_source[sid] = p
    return {"by_line": by_line, "by_item": by_item, "by_source": by_source}


def find_product_in_index(
    row: dict[str, Any], index: dict[str, dict[str, dict[str, Any]]]
) -> Optional[dict[str, Any]]:
    """在 build_ord_line_product_index 结果里查找，优先级同 find_product_for_ord_line。"""
    ord_line = str(row.get("ord_line_no") or "").strip()
    if ord_line:
        hit = index["by_line"].get(ord_line)
        if hit:
            return hit
    item_id = str(row.get("item_id") or "").strip()
    if item_id:
        hit = index["by_item"].get(item_id)
        if hit:
            return hit
    splr_id = str(row.get("splr_item_id") or "").strip()
    if splr_id:
        hit = index["by_source"].get(splr_id)
        if hit:
            return hit
    return None


def is_valid_hs_mapping(hs: Any) -> bool:
    if not isinstance(hs, dict):
        return False
    try:
        cid = int(hs.get("category_id") or 0)
    except (TypeError, ValueError):
        return False
    if cid <= 0:
        return False
    name = str(hs.get("category_cn_name") or "").strip()
    return bool(name and name.lower() != "nan")


def mapping_hint_for_product(product: dict[str, Any]) -> Optional[str]:
    hint = str(product.get("platform_category_hint") or "").strip()
    if hint in GENERIC_PLATFORM_HINTS:
        return None
    return hint or None


def find_reusable_hs_mapping(
    product: dict[str, Any],
    *,
    exclude_id: Optional[str] = None,
    validate_title: bool = True,
) -> Optional[tuple[dict[str, Any], str]]:
    """同 SKU 复用已有映射；标题与 HS 不一致时放弃复用，改走完整映射。"""
    from app.services.category_mapping.local_mappings import get_by_item_id, get_by_splr_item_id
    from app.services.category_mapping.mapping_quality import mapping_aligns_with_title

    title = str(product.get("product_name") or "").strip()

    def _accept(hs: dict[str, Any], detail: str) -> Optional[tuple[dict[str, Any], str]]:
        if validate_title and title:
            ok, reason, _ = mapping_aligns_with_title(title, hs)
            if not ok:
                return None
            return hs, detail if ok else reason
        return hs, detail

    pid = str(product.get("tangbuy_product_id") or "").strip()
    splr = str(product.get("source_product_id") or "").strip()

    if splr:
        local = get_by_splr_item_id(splr)
        if local and is_valid_hs_mapping(local.get("hs")):
            accepted = _accept(local["hs"], f"1688 offer {splr} 已有本地映射")
            if accepted:
                return accepted

    if pid:
        local = get_by_item_id(pid)
        if local and is_valid_hs_mapping(local.get("hs")):
            accepted = _accept(local["hs"], f"Tangbuy 商品 {pid} 已有本地映射")
            if accepted:
                return accepted

    for sibling in load_products():
        sid = str(sibling.get("tangbuy_product_id") or "")
        if exclude_id and sid == exclude_id:
            continue
        if sibling.get("category_status") not in RESOLVED_MAPPING_STATUSES:
            continue
        hs = sibling.get("hs_mapping")
        if not is_valid_hs_mapping(hs):
            continue
        s_splr = str(sibling.get("source_product_id") or "").strip()
        if splr and s_splr == splr:
            accepted = _accept(hs, f"商品中心同款 offer {splr}")
            if accepted:
                return accepted
        if pid and sid == pid:
            accepted = _accept(hs, f"商品中心同款 item {pid}")
            if accepted:
                return accepted

    return None


def update_product(
    tangbuy_id: str,
    updater: Callable[[dict[str, Any]], dict[str, Any]],
) -> Optional[dict[str, Any]]:
    if is_db_enabled():
        from app.db.catalog_repos import ProductRepository

        with db_session() as session:
            return ProductRepository(session).update_product(tangbuy_id, updater)
    with _io_lock:
        if not _CENTER_PATH.exists():
            return None
        try:
            items = _parse_products_text(_CENTER_PATH.read_text(encoding="utf-8"))
        except OSError:
            return None
        idx = next((i for i, p in enumerate(items) if p.get("tangbuy_product_id") == tangbuy_id), -1)
        if idx < 0:
            return None
        items[idx] = updater(items[idx])
        payload = json.dumps(items, ensure_ascii=False, indent=2) + "\n"
        _CENTER_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix="center-",
            suffix=".json.tmp",
            dir=str(_CENTER_PATH.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, _CENTER_PATH)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        return items[idx]


def _ensure_catalog_cid(hs: dict[str, Any]) -> dict[str, Any]:
    """确认前若为权威兜底候选（无本地 cid），新增写回主目录以获得 cid。"""
    try:
        cid = int(hs.get("category_id") or 0)
    except (TypeError, ValueError):
        cid = 0
    if cid > 0:
        return hs
    hs_code = str(hs.get("hs_code") or "").strip()
    cn_name = str(hs.get("category_cn_name") or hs.get("declare_cn_name") or "").strip()
    if not (hs_code and cn_name):
        return hs
    from app.services.category_mapping.catalog_admin import append_catalog_entry

    try:
        mapping = append_catalog_entry(
            cn_name=cn_name,
            hs_code=hs_code,
            en_name=str(hs.get("category_en_name") or ""),
            declare_cn_name=str(hs.get("declare_cn_name") or ""),
            declare_en_name=str(hs.get("declare_en_name") or ""),
            tariff=hs.get("tariff"),
        )
    except ValueError:
        return hs
    merged = dict(hs)
    merged.update(mapping)
    return merged


def repair_stuck_mapping_products() -> int:
    """持久化修复：mapping 且无 HS → pending。"""
    if is_db_enabled():
        items = load_products()
        changed = 0
        repaired: list[dict[str, Any]] = []
        for p in items:
            if p.get("category_status") == "mapping" and not is_valid_hs_mapping(p.get("hs_mapping")):
                repaired.append({**p, "category_status": "pending"})
                changed += 1
            else:
                repaired.append(p)
        if changed:
            save_products(repaired)
        return changed
    with _io_lock:
        if not _CENTER_PATH.exists():
            return 0
        try:
            items = _parse_products_text(_CENTER_PATH.read_text(encoding="utf-8"))
        except OSError:
            return 0
        changed = 0
        for i, p in enumerate(items):
            if p.get("category_status") == "mapping" and not is_valid_hs_mapping(p.get("hs_mapping")):
                items[i] = {**p, "category_status": "pending"}
                changed += 1
        if changed:
            payload = json.dumps(items, ensure_ascii=False, indent=2) + "\n"
            _CENTER_PATH.parent.mkdir(parents=True, exist_ok=True)
            _CENTER_PATH.write_text(payload, encoding="utf-8")
        return changed


def sanitize_product_mapping_state(product: dict[str, Any]) -> dict[str, Any]:
    """卡住的 mapping（无 HS）恢复为 pending，便于商品中心重新映射。"""
    if product.get("category_status") != "mapping":
        return product
    if is_valid_hs_mapping(product.get("hs_mapping")):
        return product
    return {**product, "category_status": "pending"}


def _initial_admin_writeback_meta(
    product: dict[str, Any],
    hs: dict[str, Any],
    *,
    at: str,
) -> dict[str, Any]:
    from app.services.category_mapping.admin_writeback import _writing_placeholder

    return _writing_placeholder(product, hs, at=at)


def persist_product_mapping_side_effects(
    product: dict[str, Any],
    hs: dict[str, Any],
    *,
    manual: bool = False,
    resolution: Optional[str] = None,
    skip_admin_writeback: bool = False,
) -> None:
    """商品落库提交后再写本地映射缓存与 Admin 回写，避免 SQLite 嵌套事务锁库。"""
    if not is_valid_hs_mapping(hs):
        return
    upsert_local_mapping(
        item_id=str(product.get("tangbuy_product_id") or "") or None,
        splr_item_id=str(product.get("source_product_id") or "") or None,
        hs=hs,
        source="correct" if manual else "confirm",
    )
    # 本地立即 overlay + resume，不等 Admin 回写；映射保存后自动重新接单/进入待下单
    _resume_linked_orders_after_category(product, hs)
    pid = str(product.get("tangbuy_product_id") or "").strip()
    if skip_admin_writeback or not pid:
        return
    from app.services.category_mapping.admin_writeback import (
        collect_item_nos,
        schedule_admin_writeback,
        should_skip_admin_writeback,
    )

    ids = collect_item_nos(product)
    skip, reason = should_skip_admin_writeback(product, hs, item_nos=ids)
    if skip and reason in ("already_ok", "in_flight", "admin_already"):
        return
    wb_resolution = resolution or ("manual_correct" if manual else "auto")
    schedule_admin_writeback(pid, hs, resolution=wb_resolution, item_nos=ids or None)  # type: ignore[arg-type]
    try:
        from app.services.workflow.hooks import trace_category_map

        trace_category_map(
            ids,
            product_id=pid,
            hs=hs,
            manual=manual,
            resolution=wb_resolution,
        )
    except Exception:
        pass


def _resume_linked_orders_after_category(product: dict[str, Any], hs: dict[str, Any]) -> None:
    """映射确认后：先写子单品类 overlay，再 resume 流水线（接单闸门解除 → 待下单）。"""
    try:
        from app.services.category_mapping.admin_writeback import collect_item_nos
        from app.services.category_mapping.write_port import apply_ord_line_category_writeback
        from app.services.orders.procurement_pipeline import is_pipeline_active, resume_pipeline
    except Exception:
        return

    lines = collect_item_nos(product)
    if not lines:
        lines = [
            str(x).strip()
            for x in (product.get("linked_ord_lines") or [])
            if str(x).strip()
        ]
    if not lines:
        return
    try:
        apply_ord_line_category_writeback(
            lines,
            hs,
            source="category_confirm",
            mapping_resolution=(
                "manual_correct"
                if str(product.get("mapping_record", {}).get("review_status")) == "corrected"
                else "manual_confirm"
            ),
            mapping_confidence=float(product.get("mapping_confidence") or 0) or None,
        )
    except Exception:
        pass
    # 流水线内自动映射已会继续接单，勿递归 resume
    if is_pipeline_active():
        return
    for line_no in lines:
        key = str(line_no or "").strip()
        if not key:
            continue
        try:
            resume_pipeline(key, operator="category_confirm")
        except Exception:
            pass


def finalize_product_mapping_confirm(
    product: Optional[dict[str, Any]],
    *,
    manual: bool = False,
    resolution: Optional[str] = None,
    skip_admin_writeback: bool = False,
) -> Optional[dict[str, Any]]:
    """商品落库提交后执行映射副作用（本地缓存 + Admin 回写）。"""
    if not product:
        return None
    hs = product.get("hs_mapping")
    if is_valid_hs_mapping(hs):
        persist_product_mapping_side_effects(
            product,
            hs,
            manual=manual,
            resolution=resolution,
            skip_admin_writeback=skip_admin_writeback,
        )
    return product


def _writeback_already_ok(product: dict[str, Any], hs: dict[str, Any]) -> bool:
    """本地已记录同目标类目的成功回写，或 Admin 已是目标类目，无需再次调度。"""
    from app.services.category_mapping.admin_writeback import (
        collect_item_nos,
        should_skip_admin_writeback,
    )

    skip, reason = should_skip_admin_writeback(
        product,
        hs,
        item_nos=collect_item_nos(product),
    )
    return skip and reason in ("already_ok", "in_flight", "admin_already")


def confirm_product_mapping(
    product: dict[str, Any],
    hs: dict[str, Any],
    reviewer_note: Optional[str] = None,
    *,
    manual: bool = False,
    resolution: Optional[str] = None,
    skip_admin_writeback: bool = False,
    defer_side_effects: bool = False,
) -> dict[str, Any]:
    hs = _ensure_catalog_cid(hs)
    now = _now_iso()
    category_path = f"{hs.get('category_cn_name', '')} / {hs.get('declare_cn_name', '')} · HS {hs.get('hs_code', '')}"
    mapping = product.get("mapping_record") or {}
    suggest_at = str(mapping.get("mapped_at") or product.get("mapping_mapped_at") or "").strip() or now
    mapping.update(
        {
            "suggested_category_path": category_path,
            "suggested_hs": hs,
            "review_status": "corrected" if manual else "confirmed",
            "reviewed_at": now,
            "reviewer_note": reviewer_note,
        }
    )
    if not mapping.get("mapped_at"):
        mapping["mapped_at"] = suggest_at
    product["mapping_record"] = mapping
    product["hs_mapping"] = hs
    product["category"] = hs.get("category_cn_name") or product.get("category")
    product["category_status"] = "auto_passed"
    product["mapping_confidence"] = 1.0 if manual else product.get("mapping_confidence", 0.9)
    product["mapping_mapped_at"] = suggest_at
    if not skip_admin_writeback:
        mapping = dict(product.get("mapping_record") or {})
        if not _writeback_already_ok(product, hs):
            from app.services.category_mapping.admin_writeback import resolve_admin_writeback_placeholder

            wb_resolution = resolution or ("manual_correct" if manual else "auto")
            mapping["admin_writeback"] = resolve_admin_writeback_placeholder(
                product,
                hs,
                at=now,
                resolution=wb_resolution,  # type: ignore[arg-type]
            )
            product["mapping_record"] = mapping
    if defer_side_effects:
        return product
    persist_product_mapping_side_effects(
        product,
        hs,
        manual=manual,
        resolution=resolution,
        skip_admin_writeback=skip_admin_writeback,
    )
    return product

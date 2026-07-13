"""Shadow eval 样本 enrichment（标题、毛利等）。"""

from __future__ import annotations

from typing import Any, Optional


def enrich_correction_case(case: dict[str, Any]) -> dict[str, Any]:
    row = dict(case)
    meta = dict(row.get("context_meta") or {}) if isinstance(row.get("context_meta"), dict) else {}

    for key in ("title", "product_title", "ord_line_no", "goods_id", "margin_pct", "margin_rate"):
        if row.get(key) is None and meta.get(key) is not None:
            row[key] = meta[key]

    ref = str(row.get("context_ref") or "").strip()
    skill_id = str(row.get("skill_id") or "").strip()

    if not row.get("title") and not row.get("product_title"):
        row = _enrich_title_from_product(ref, row)

    if skill_id == "auto-release" and row.get("margin_pct") is None and row.get("margin_rate") is None:
        row = _enrich_margin_from_release(ref, row, meta)

    if meta:
        row["context_meta"] = meta
    return row


def _enrich_title_from_product(ref: str, row: dict[str, Any]) -> dict[str, Any]:
    if not ref:
        return row
    try:
        from app.services.products.store import find_by_source_product_id

        product = find_by_source_product_id(ref)
        if product:
            title = str(product.get("product_name") or "").strip()
            if title:
                row["title"] = title
                row["product_title"] = title
    except Exception:
        pass
    return row


def _enrich_margin_from_release(
    ref: str,
    row: dict[str, Any],
    meta: dict[str, Any],
) -> dict[str, Any]:
    key = str(meta.get("ord_line_no") or ref or "").strip()
    if not key:
        return row
    try:
        from app.services.orders import release_store

        latest = release_store.latest_release(key)
        if not latest:
            return row
        rate = latest.get("margin_rate")
        if rate is None:
            return row
        pct = round(float(rate) * 100, 2)
        row["margin_rate"] = rate
        row["margin_pct"] = pct
        meta["margin_rate"] = rate
        meta["margin_pct"] = pct
        meta.setdefault("ord_line_no", key)
        title = str(latest.get("product_title") or "").strip()
        if title and not row.get("title"):
            row["title"] = title
            row["product_title"] = title
    except Exception:
        pass
    return row


def parse_margin_pct(case: dict[str, Any]) -> Optional[float]:
    meta = case.get("context_meta") if isinstance(case.get("context_meta"), dict) else {}
    for source in (meta, case):
        if not isinstance(source, dict):
            continue
        if source.get("margin_pct") is not None:
            try:
                return float(source["margin_pct"])
            except (TypeError, ValueError):
                pass
        if source.get("margin_rate") is not None:
            try:
                v = float(source["margin_rate"])
                return round(v * 100, 4) if abs(v) <= 1 else v
            except (TypeError, ValueError):
                pass
    return None

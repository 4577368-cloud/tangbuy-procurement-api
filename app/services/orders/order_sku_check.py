"""规格核对（Admin frontSku vs 货源规格）。"""

from __future__ import annotations

from typing import Any


def _normalize_spec(text: str) -> str:
    return (
        (text or "")
        .strip()
        .lower()
        .replace(" ", "")
        .replace("；", ";")
        .replace("：", ":")
        .rstrip(";")
    )


def _parse_pairs(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in _normalize_spec(text).split(";"):
        if ":" not in part:
            continue
        key, val = part.split(":", 1)
        key, val = key.strip(), val.strip()
        if key and val:
            out[key] = val
    return out


def check_sku_alignment(row: dict[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    evidence: list[str] = []

    front_attr = str(row.get("front_sku_attr_desc") or "").strip()
    source_attr = str(row.get("item_attr") or row.get("item_attr_cn") or "").strip()
    front_sku = str(row.get("front_sku_id") or "").strip()
    source_sku = str(row.get("sku_id") or "").strip()

    if front_attr and source_attr:
        if _normalize_spec(front_attr) != _normalize_spec(source_attr):
            diffs = []
            a, b = _parse_pairs(front_attr), _parse_pairs(source_attr)
            for key in set(a) | set(b):
                if key in a and key in b and a[key] != b[key]:
                    diffs.append(f"{key}: {a[key]} ≠ {b[key]}")
            if diffs:
                reasons.append("前台下单规格与货源规格不一致")
                evidence.extend(f"规格差异 · {d}" for d in diffs)
            else:
                reasons.append("前台下单规格与货源规格文本不一致")
                evidence.extend([f"前台：{front_attr}", f"货源：{source_attr}"])

    if front_sku and source_sku and front_sku != source_sku and not reasons:
        reasons.append("前台 SKU 与货源 SKU 不一致")
        evidence.extend([f"前台 SKU {front_sku}", f"货源 SKU {source_sku}"])

    bar = row.get("item_bar_cd")
    if bar:
        evidence.append(f"条码 {bar}")

    return {
        "sku_mismatch": bool(reasons),
        "sku_mismatch_reasons": reasons,
        "sku_mismatch_evidence": evidence,
    }


def enrich_row_sku_fields(row: dict[str, Any]) -> dict[str, Any]:
    result = check_sku_alignment(row)
    row["sku_mismatch"] = result["sku_mismatch"]
    row["sku_mismatch_reasons"] = result["sku_mismatch_reasons"]
    return row

#!/usr/bin/env python3
"""从 Excel 构建品类映射数据（HS 类目表 + 历史商品映射）。"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pandas as pd

from category_heuristics import build_heuristics_from_catalog

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "category"
HS_XLSX = ROOT / "all_hscode_category.xlsx"
HIST_XLSX = ROOT / "历史设置类目信息商品记录.xlsx"


def tokenize(text: str) -> set[str]:
    text = (text or "").lower()
    tokens: set[str] = set()
    tokens.update(re.findall(r"[a-z]{2,}", text))
    han = re.sub(r"[^\u4e00-\u9fff]", "", text)
    for i in range(max(0, len(han) - 1)):
        tokens.add(han[i : i + 2])
    tokens.update(re.findall(r"[\u4e00-\u9fff]{2,6}", text))
    return {t for t in tokens if len(t) >= 2}


def main() -> int:
    if not HS_XLSX.exists() or not HIST_XLSX.exists():
        print("缺少 Excel 文件，请放在 procurement-demo 根目录", file=sys.stderr)
        return 1

    OUT.mkdir(parents=True, exist_ok=True)

    hs = pd.read_excel(HS_XLSX)
    hist = pd.read_excel(HIST_XLSX)

    catalog_records = []
    by_cid: dict[str, dict] = {}
    for _, row in hs.iterrows():
        cid = int(row["cid"])
        item = {
            "cid": cid,
            "cn_name": str(row.get("cn_name") or "").strip(),
            "en_name": str(row.get("en_name") or "").strip(),
            "hs_code": str(row.get("hs_code") or "").strip(),
            "dec_cn_name": str(row.get("dec_cn_name") or "").strip(),
            "dec_en_name": str(row.get("dec_en_name") or "").strip(),
            "tariff": None if pd.isna(row.get("tariff")) else float(row["tariff"]),
            "parent_id": int(row["parent_id"]) if not pd.isna(row.get("parent_id")) else None,
        }
        catalog_records.append(item)
        by_cid[str(cid)] = item

    history_records = []
    goods_id_map: dict[str, int] = {}
    token_index: dict[str, list[int]] = {}

    for idx, row in hist.iterrows():
        goods_id = str(row.get("goods_id") or "").strip()
        category_id = int(row["category_id"])
        goods_name = str(row.get("goods_name") or "").strip()
        goods_img = str(row.get("goods_img") or "").strip()
        rec = {
            "goods_name": goods_name,
            "goods_id": goods_id,
            "category_id": category_id,
            "goods_img": goods_img,
        }
        history_records.append(rec)
        if goods_id:
            goods_id_map[goods_id] = category_id
        for tok in tokenize(goods_name):
            token_index.setdefault(tok, []).append(len(history_records) - 1)

    # 限制倒排索引体积：每个 token 最多保留 80 条
    for tok in list(token_index.keys()):
        if len(token_index[tok]) > 80:
            token_index[tok] = token_index[tok][:80]

    # HS 类目搜索倒排索引（供 Node 侧模糊搜索，避免每次冷启 Python）
    catalog_search_index: dict[str, list[dict]] = {}
    catalog_search_entries: list[dict] = []

    for entry in catalog_records:
        cid = entry["cid"]
        search_blob = " ".join(
            filter(
                None,
                [
                    entry.get("cn_name"),
                    entry.get("en_name"),
                    entry.get("dec_cn_name"),
                    entry.get("dec_en_name"),
                    entry.get("hs_code"),
                    str(cid),
                ],
            )
        )
        catalog_search_entries.append(
            {
                "cid": cid,
                "cn_name": entry["cn_name"],
                "en_name": entry["en_name"],
                "hs_code": entry["hs_code"],
                "dec_cn_name": entry["dec_cn_name"],
                "dec_en_name": entry["dec_en_name"],
                "search_blob": search_blob.lower(),
            }
        )
        blob_tokens = tokenize(search_blob)
        for tok in blob_tokens:
            catalog_search_index.setdefault(tok, []).append({"cid": cid, "w": 1.0})
        for seg in re.findall(r"[\u4e00-\u9fff]{2,8}", entry.get("cn_name", "")):
            catalog_search_index.setdefault(seg, []).append({"cid": cid, "w": 1.5})
        for seg in re.findall(r"[\u4e00-\u9fff]{2,8}", entry.get("dec_cn_name", "")):
            catalog_search_index.setdefault(seg, []).append({"cid": cid, "w": 1.2})

    for tok in list(catalog_search_index.keys()):
        if len(catalog_search_index[tok]) > 120:
            catalog_search_index[tok] = catalog_search_index[tok][:120]

    meta = {
        "catalog_count": len(catalog_records),
        "history_count": len(history_records),
        "built_from": [HS_XLSX.name, HIST_XLSX.name],
        "search_index_tokens": len(catalog_search_index),
    }

    (OUT / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "catalog.json").write_text(
        json.dumps({"by_cid": by_cid, "list": catalog_records}, ensure_ascii=False),
        encoding="utf-8",
    )
    (OUT / "history.json").write_text(json.dumps(history_records, ensure_ascii=False), encoding="utf-8")
    (OUT / "goods-id-index.json").write_text(
        json.dumps(goods_id_map, ensure_ascii=False), encoding="utf-8"
    )
    (OUT / "token-index.json").write_text(json.dumps(token_index, ensure_ascii=False), encoding="utf-8")
    (OUT / "catalog-search-entries.json").write_text(
        json.dumps(catalog_search_entries, ensure_ascii=False), encoding="utf-8"
    )
    (OUT / "catalog-search-index.json").write_text(
        json.dumps(catalog_search_index, ensure_ascii=False), encoding="utf-8"
    )

    heuristics = build_heuristics_from_catalog(catalog_records)
    (OUT / "mapping-heuristics.json").write_text(
        json.dumps(heuristics, ensure_ascii=False), encoding="utf-8"
    )

    print(
        f"✅ catalog {len(catalog_records)} · history {len(history_records)} "
        f"· search {len(catalog_search_entries)} · heuristics collision {len(heuristics.get('declare_collision_terms', {}))} → {OUT}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

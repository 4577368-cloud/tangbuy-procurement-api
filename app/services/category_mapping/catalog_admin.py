"""主目录 catalog.json 追加写回。

当推荐与预存都不命中、经权威兜底库确认后需**新增**一个 HS 类目时，把它写进
本地主目录，使后续 suggest / HS 搜索可复用。同步维护搜索 entries 与倒排索引，
并失效 catalog_search 的进程内缓存。

新增类目的 cid 使用合成高位区间（≥ CUSTOM_CID_BASE），与真实类目 ID 隔离。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.core.paths import data_dir

CUSTOM_CID_BASE = 9_000_000_000_000  # 9e12，远高于真实 cid 上限，标识“本系统新增”

_CAT = "catalog.json"
_ENTRIES = "catalog-search-entries.json"
_INDEX = "catalog-search-index.json"


def _p(name: str) -> Path:
    return data_dir() / "category" / name


def _read(name: str) -> Any:
    path = _p(name)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _write(name: str, data: Any) -> None:
    _p(name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _tokenize(text: str) -> set[str]:
    text = (text or "").lower()
    tokens: set[str] = set()
    tokens.update(re.findall(r"[a-z]{2,}", text))
    han = re.sub(r"[^\u4e00-\u9fff]", "", text)
    for i in range(max(0, len(han) - 1)):
        tokens.add(han[i : i + 2])
    tokens.update(re.findall(r"[\u4e00-\u9fff]{2,6}", text))
    return {t for t in tokens if len(t) >= 2}


def _normalize_hs(raw: object) -> str:
    text = str(raw or "").strip()
    if text.isdigit() and len(text) == 9:
        return text.zfill(10)
    return text


def _find_existing(catalog: dict[str, Any], hs_code: str, cn_name: str) -> dict[str, Any] | None:
    for e in catalog.get("list", []):
        if str(e.get("hs_code") or "").strip() == hs_code and str(e.get("cn_name") or "").strip() == cn_name:
            return e
    return None


def _next_cid(catalog: dict[str, Any]) -> int:
    existing = [
        int(e["cid"])
        for e in catalog.get("list", [])
        if str(e.get("cid", "")).lstrip("-").isdigit() and int(e["cid"]) >= CUSTOM_CID_BASE
    ]
    return (max(existing) + 1) if existing else (CUSTOM_CID_BASE + 1)


def append_catalog_entry(
    *,
    cn_name: str,
    hs_code: str,
    en_name: str = "",
    declare_cn_name: str = "",
    declare_en_name: str = "",
    tariff: float | None = None,
) -> dict[str, Any]:
    """追加一个新类目到主目录，返回 6 字段映射（含新 cid）。

    若同 hs_code + cn_name 已存在则直接复用，不重复写入。
    """
    cn_name = (cn_name or "").strip()
    hs_code = _normalize_hs(hs_code)
    if not cn_name or not hs_code:
        raise ValueError("cn_name 与 hs_code 必填")
    if not (hs_code.isdigit() and len(hs_code) == 10):
        raise ValueError(f"hs_code 必须为 10 位数字：{hs_code!r}")

    catalog = _read(_CAT) or {"by_cid": {}, "list": []}
    dup = _find_existing(catalog, hs_code, cn_name)
    if dup is not None:
        return _to_mapping(dup)

    cid = _next_cid(catalog)
    dec_cn = (declare_cn_name or cn_name).strip()
    dec_en = (declare_en_name or en_name).strip()
    entry = {
        "cid": cid,
        "cn_name": cn_name,
        "en_name": (en_name or "").strip(),
        "hs_code": hs_code,
        "dec_cn_name": dec_cn,
        "dec_en_name": dec_en,
        "tariff": tariff,
        "parent_id": 999999999,
        "source": "custom_append",
    }
    catalog.setdefault("list", []).append(entry)
    catalog.setdefault("by_cid", {})[str(cid)] = entry
    _write(_CAT, catalog)

    _append_search(entry)
    _invalidate_cache()
    return _to_mapping(entry)


def _append_search(entry: dict[str, Any]) -> None:
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
    ).lower()

    entries = _read(_ENTRIES)
    if isinstance(entries, list):
        entries.append(
            {
                "cid": cid,
                "cn_name": entry["cn_name"],
                "en_name": entry["en_name"],
                "hs_code": entry["hs_code"],
                "dec_cn_name": entry["dec_cn_name"],
                "dec_en_name": entry["dec_en_name"],
                "search_blob": search_blob,
            }
        )
        _write(_ENTRIES, entries)

    index = _read(_INDEX)
    if isinstance(index, dict):
        for tok in _tokenize(search_blob):
            index.setdefault(tok, []).append({"cid": cid, "w": 1.0})
        for seg in re.findall(r"[\u4e00-\u9fff]{2,8}", entry.get("cn_name", "")):
            index.setdefault(seg, []).append({"cid": cid, "w": 1.5})
        for seg in re.findall(r"[\u4e00-\u9fff]{2,8}", entry.get("dec_cn_name", "")):
            index.setdefault(seg, []).append({"cid": cid, "w": 1.2})
        _write(_INDEX, index)


def _invalidate_cache() -> None:
    try:
        from app.services.category_mapping import catalog_search

        catalog_search._load_entries.cache_clear()
        catalog_search._load_index.cache_clear()
    except Exception:
        pass


def _to_mapping(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "category_id": int(entry["cid"]),
        "category_cn_name": entry.get("cn_name", ""),
        "category_en_name": entry.get("en_name", ""),
        "hs_code": entry.get("hs_code", ""),
        "declare_cn_name": entry.get("dec_cn_name", ""),
        "declare_en_name": entry.get("dec_en_name", ""),
        "tariff": entry.get("tariff"),
    }

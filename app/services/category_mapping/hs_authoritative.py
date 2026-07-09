"""权威 HS 兜底库（中国海关 10 位商品编码）。

本地主目录 catalog.json 覆盖较窄（仅 ~1.6k 唯一 10 位码），当推荐与预存都不
命中时，用此权威库校验 10 位编码是否真实存在、并按品名反查候选编码。

数据由 scripts/build-hs-authoritative.py 从「单一窗口」公开参数构建：
  data/category/hs-authoritative.json        by_code 明细
  data/category/hs-authoritative-index.json  分词倒排（品名反查）
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.core.paths import data_dir


def _path(name: str) -> Path:
    return data_dir() / "category" / name


@lru_cache(maxsize=1)
def _load_by_code() -> dict[str, dict[str, Any]]:
    path = _path("hs-authoritative.json")
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    by_code = data.get("by_code") if isinstance(data, dict) else None
    return by_code if isinstance(by_code, dict) else {}


@lru_cache(maxsize=1)
def _load_index() -> dict[str, list[str]]:
    path = _path("hs-authoritative-index.json")
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def is_ready() -> bool:
    return _path("hs-authoritative.json").exists()


def _tokenize(text: str) -> set[str]:
    text = (text or "").lower()
    tokens: set[str] = set()
    tokens.update(re.findall(r"[a-z]{2,}", text))
    han = re.sub(r"[^\u4e00-\u9fff]", "", text)
    for i in range(max(0, len(han) - 1)):
        tokens.add(han[i : i + 2])
    tokens.update(re.findall(r"[\u4e00-\u9fff]{2,8}", text))
    return {t for t in tokens if len(t) >= 2}


def normalize_code(raw: object) -> str:
    text = str(raw or "").strip()
    if text.isdigit() and len(text) == 9:
        return text.zfill(10)
    return text


def is_valid_code(code: object) -> bool:
    """该 10 位编码是否为真实存在的中国海关税则号。"""
    c = normalize_code(code)
    return bool(c) and c in _load_by_code()


def get_detail(code: object) -> dict[str, Any] | None:
    return _load_by_code().get(normalize_code(code))


def search(query: str, limit: int = 12) -> list[dict[str, Any]]:
    """按品名/关键词反查权威 10 位编码候选。"""
    q = (query or "").strip()
    if not q:
        return []
    by_code = _load_by_code()
    index = _load_index()
    if not by_code or not index:
        return []

    # 精确编码命中优先
    code_q = normalize_code(q)
    scores: dict[str, float] = {}
    if code_q.isdigit() and code_q in by_code:
        scores[code_q] = 1e6

    tokens = _tokenize(q)
    for tok in tokens:
        for code in index.get(tok, []):
            scores[code] = scores.get(code, 0.0) + 1.0

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:limit]
    out: list[dict[str, Any]] = []
    for code, score in ranked:
        e = by_code.get(code)
        if not e:
            continue
        out.append(
            {
                "score": score,
                "hs_code": code,
                "declare_cn_name": e.get("name", ""),
                "names": e.get("names", []),
                "control_mark": e.get("control_mark", ""),
                "unit_1": e.get("unit_1", ""),
                "unit_2": e.get("unit_2", ""),
                "source": "authoritative",
            }
        )
    return out

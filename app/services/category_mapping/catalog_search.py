"""品类映射 — HS 目录搜索。"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.core.paths import data_dir
from app.integrations.skill_cli import category_mapper_path, run_python_cli


def _tokenize(text: str) -> set[str]:
    tokens: set[str] = set()
    lower = (text or "").lower()
    for m in re.finditer(r"[a-z]{2,}", lower):
        tokens.add(m.group(0))
    han = re.sub(r"[^\u4e00-\u9fff]", "", lower)
    for i in range(max(0, len(han) - 1)):
        tokens.add(han[i : i + 2])
    for m in re.finditer(r"[\u4e00-\u9fff]{2,8}", lower):
        tokens.add(m.group(0))
    return {t for t in tokens if len(t) >= 2}


@lru_cache(maxsize=1)
def _load_entries() -> list[dict[str, Any]]:
    path = data_dir() / "category" / "catalog-search-entries.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


@lru_cache(maxsize=1)
def _load_index() -> dict[str, list[dict[str, Any]]]:
    path = data_dir() / "category" / "catalog-search-index.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def is_catalog_search_ready() -> bool:
    return (data_dir() / "category" / "catalog-search-index.json").exists()


def search_hs_catalog(query: str, limit: int = 12) -> list[dict[str, Any]]:
    entries = _load_entries()
    index = _load_index()
    if not entries or not index:
        return []
    by_cid = {e["cid"]: e for e in entries if "cid" in e}
    scores: dict[int, float] = {}
    for token in _tokenize(query):
        for hit in index.get(token, []):
            cid = hit.get("cid")
            if cid is None:
                continue
            scores[int(cid)] = scores.get(int(cid), 0) + float(hit.get("w") or 1)
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:limit]
    out: list[dict[str, Any]] = []
    for cid, score in ranked:
        e = by_cid.get(cid)
        if not e:
            continue
        out.append(
            {
                "score": score,
                "category_id": cid,
                "category_cn_name": e.get("cn_name", ""),
                "category_en_name": e.get("en_name", ""),
                "hs_code": e.get("hs_code", ""),
                "declare_cn_name": e.get("dec_cn_name", ""),
                "declare_en_name": e.get("dec_en_name", ""),
            }
        )
    return out


def run_category_search(query: str, limit: int = 12) -> dict[str, Any]:
    return run_python_cli(
        category_mapper_path(),
        ["search", "--query", query, "--limit", str(limit)],
    )


def is_category_data_ready() -> bool:
    return (data_dir() / "category" / "catalog.json").exists()


def build_suggest_markdown(result: dict[str, Any]) -> str:
    if not result.get("success") or not result.get("category_id"):
        return f"❌ 品类映射失败：{result.get('error') or '未知错误'}"
    lines = [
        "✅ 品类映射建议",
        "",
        f"- **决策**: {result.get('decision', 'manual_suggested')}",
        f"- **分类中文名**: {result.get('category_cn_name', '')}",
        f"- **分类英文名**: {result.get('category_en_name', '')}",
        f"- **分类编号**: {result.get('category_id', '')}",
        f"- **海关编码**: {result.get('hs_code', '')}",
        f"- **中文描述**: {result.get('declare_cn_name', '')}",
        f"- **英文描述**: {result.get('declare_en_name', '')}",
    ]
    if result.get("match_detail"):
        lines.append(f"- **说明**: {result['match_detail']}")
    return "\n".join(lines)

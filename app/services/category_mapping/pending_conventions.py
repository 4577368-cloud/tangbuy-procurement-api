"""品类在线共识（pending → soft → promote）应用层封装。

实现在 scripts/pending_conventions.py，本模块供 feedback / products 调用。
接真实库时只换存储 Port，投票与晋级政策不变。
"""

from __future__ import annotations

import sys
from functools import lru_cache
from typing import Any

from app.core.paths import scripts_dir


@lru_cache(maxsize=1)
def _pc():
    scripts = str(scripts_dir())
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import pending_conventions as pc  # noqa: WPS433

    return pc


def ingest_feedback_entry(entry: dict[str, Any]) -> dict[str, Any]:
    return _pc().ingest_feedback_entry(entry)


def record_vote(**kwargs: Any) -> dict[str, Any]:
    return _pc().record_vote(**kwargs)


def lookup_for_text(title: str, vision_keywords: list[str] | None = None) -> list[dict]:
    return _pc().lookup_pending_conventions_for_text(title, vision_keywords)


def lookup_goods_soft(goods_id: str) -> dict[str, Any] | None:
    return _pc().lookup_goods_id_soft(goods_id)


def lookup_goods_hard_override(goods_id: str) -> dict[str, Any] | None:
    return _pc().lookup_goods_id_hard_override(goods_id)

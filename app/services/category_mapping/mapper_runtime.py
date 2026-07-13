"""品类映射 in-process 运行时（避免每请求 subprocess 冷启动）。"""

from __future__ import annotations

import sys
from functools import lru_cache
from typing import Any, Optional

from app.core.paths import PROJECT_ROOT

_SCRIPTS = PROJECT_ROOT / "scripts"


@lru_cache(maxsize=1)
def _mapper_module():
    scripts = str(_SCRIPTS)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import category_mapper as cm  # noqa: WPS433

    return cm


def suggest_inprocess(
    title: str,
    hint: str = "",
    goods_id: str = "",
    image_url: str = "",
    vision_keywords: Optional[list[str]] = None,
    *,
    skip_history: bool = False,
    hint_as_reference: bool = False,
    platform_hint: str = "",
) -> dict[str, Any]:
    cm = _mapper_module()
    return cm.suggest(
        title,
        hint,
        goods_id,
        image_url,
        vision_keywords,
        skip_history=skip_history,
        hint_as_reference=hint_as_reference,
        platform_hint=platform_hint or hint,
    )


def lookup_inprocess(category_id: int) -> Optional[dict[str, Any]]:
    cm = _mapper_module()
    return cm.lookup_cid(category_id)

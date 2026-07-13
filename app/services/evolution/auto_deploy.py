"""补丁灰度发布：按 context_key 稳定分桶。"""

from __future__ import annotations

import hashlib
from typing import Optional

DEFAULT_GRAY_STEPS: list[int] = [5, 20, 50, 100]


def gray_bucket(context_key: str) -> int:
    """0–99 稳定分桶。"""
    key = (context_key or "default").strip() or "default"
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 100


def should_apply_patch(context_key: str, gray_percent: int) -> bool:
    """gray_percent=100 全量；否则按 context_key 分桶。"""
    pct = max(0, min(100, int(gray_percent or 0)))
    if pct >= 100:
        return True
    if pct <= 0:
        return False
    return gray_bucket(context_key) < pct


def first_gray_step(steps: Optional[list[int]] = None) -> int:
    seq = steps or DEFAULT_GRAY_STEPS
    return seq[0] if seq else 100


def advance_gray_percent(current: int, steps: Optional[list[int]] = None) -> int:
    seq = sorted({max(1, min(100, int(x))) for x in (steps or DEFAULT_GRAY_STEPS)})
    cur = max(0, int(current or 0))
    for step in seq:
        if cur < step:
            return step
    return 100

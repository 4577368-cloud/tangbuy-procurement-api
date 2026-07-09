"""JSON 序列化安全化（NaN/Inf 等会导致 Starlette 500 纯文本）。"""

from __future__ import annotations

import math
from typing import Any


def sanitize_for_json(value: Any) -> Any:
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, dict):
        return {k: sanitize_for_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_for_json(v) for v in value]
    return value

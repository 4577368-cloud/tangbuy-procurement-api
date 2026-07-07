"""skills-gateway 订单询盘。"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from app.core.paths import PROJECT_ROOT

_SCRIPTS = PROJECT_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import newton_cli  # noqa: E402


def send_order_inquiry(order_id: str, question: str) -> dict[str, Any]:
    return newton_cli.send_order_inquiry([order_id.strip()], question)

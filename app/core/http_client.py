"""出站 HTTP（绕过平台 HTTP_PROXY，避免 Render 等环境误走不可达代理）。"""

from __future__ import annotations

from typing import Optional
from urllib.request import ProxyHandler, Request, build_opener

_DIRECT_OPENER = build_opener(ProxyHandler({}))


def urlopen_direct(req: Request, *, timeout: Optional[int] = None):
    return _DIRECT_OPENER.open(req, timeout=timeout)

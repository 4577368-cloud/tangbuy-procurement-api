"""Tangbuy Admin Token 存储（与 alibaba-open-token 同模式）。"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

from app.core.paths import data_dir

_TOKEN_PATH = data_dir() / "integrations" / "tangbuy-admin-token.json"


def _read_token_file() -> Optional[str]:
    if not _TOKEN_PATH.exists():
        return None
    try:
        data = json.loads(_TOKEN_PATH.read_text(encoding="utf-8"))
        token = (data.get("token") or data.get("access_token") or "").strip()
        return token or None
    except (OSError, json.JSONDecodeError):
        return None


def resolve_admin_token() -> str:
    """优先 TANGBUY_ADMIN_TOKEN，其次 data/integrations/tangbuy-admin-token.json。"""
    from_env = os.environ.get("TANGBUY_ADMIN_TOKEN", "").strip()
    if from_env and from_env != "your-admin-bearer-token":
        return from_env
    from_file = _read_token_file()
    if from_file:
        return from_file
    return from_env


def save_admin_token(token: str, *, source: str = "curl") -> Path:
    _TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"token": token.strip(), "source": source}
    _TOKEN_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return _TOKEN_PATH


_BEARER_RE = re.compile(
    r"Authorization:\s*Bearer\s+([A-Za-z0-9_\-\.]+)",
    re.IGNORECASE,
)
_ADMIN_COOKIE_RE = re.compile(r"Admin-Token=([A-Za-z0-9_\-\.]+)", re.IGNORECASE)


def extract_token_from_curl(text: str) -> Optional[str]:
    """从浏览器 Copy as cURL 文本提取 Admin JWT。"""
    m = _BEARER_RE.search(text)
    if m:
        return m.group(1).strip()
    m = _ADMIN_COOKIE_RE.search(text)
    if m:
        return m.group(1).strip()
    return None

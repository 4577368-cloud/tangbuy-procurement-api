#!/usr/bin/env python3
"""从浏览器 Copy as cURL 同步 Tangbuy Admin Token。

用法：
  # 将新 curl 保存到 docs/admin-curl.txt 后：
  python3 scripts/sync_admin_token_from_curl.py docs/admin-curl.txt

  # 或粘贴到 stdin：
  pbpaste | python3 scripts/sync_admin_token_from_curl.py

会写入：
  - data/integrations/tangbuy-admin-token.json
  - .env.local 的 TANGBUY_ADMIN_TOKEN（若存在该文件）

然后重启 API：uvicorn app.main:app --reload --port 8001
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.bootstrap_env import load_env_local  # noqa: E402
from app.integrations.tangbuy_admin.token_store import (  # noqa: E402
    extract_token_from_curl,
    save_admin_token,
)

ENV_PATH = ROOT / ".env.local"
DEFAULT_CURL = ROOT / "docs" / "long_text_2026-07-07-10-53-47.txt"


def _update_env_local(token: str) -> bool:
    if not ENV_PATH.exists():
        return False
    text = ENV_PATH.read_text(encoding="utf-8")
    if "TANGBUY_ADMIN_TOKEN=" not in text:
        return False
    new_text = re.sub(
        r"^TANGBUY_ADMIN_TOKEN=.*$",
        f"TANGBUY_ADMIN_TOKEN={token}",
        text,
        count=1,
        flags=re.MULTILINE,
    )
    ENV_PATH.write_text(new_text, encoding="utf-8")
    return True


def main() -> int:
    load_env_local()
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if src and src.exists():
        curl_text = src.read_text(encoding="utf-8")
        try:
            source_label = str(src.resolve().relative_to(ROOT.resolve()))
        except ValueError:
            source_label = src.name
    else:
        curl_text = sys.stdin.read()
        source_label = "stdin"

    token = extract_token_from_curl(curl_text)
    if not token:
        print("❌ 未找到 Admin-Token / Authorization: Bearer，请粘贴完整 cURL", file=sys.stderr)
        return 1

    path = save_admin_token(token, source=source_label)
    env_ok = _update_env_local(token)
    print(f"✅ 已写入 {path.relative_to(ROOT)}")
    if env_ok:
        print(f"✅ 已更新 {ENV_PATH.relative_to(ROOT)} → TANGBUY_ADMIN_TOKEN")
    else:
        print("⚠️ 未更新 .env.local，请手工设置 TANGBUY_ADMIN_TOKEN")
    print("请重启 API 后在前端点「拉取最新」")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

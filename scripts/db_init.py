#!/usr/bin/env python3
"""初始化数据库：迁移 + 从 JSON 导入历史数据。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.bootstrap_env import load_env_local

load_env_local()

from app.core.config import get_settings
from app.db.import_json import import_all_to_db
from app.db.session import check_db_connection, init_database, is_db_enabled


def main() -> int:
    if not is_db_enabled():
        print("❌ DATABASE_URL 未配置（.env.local）")
        return 1

    print(f"DATABASE_URL = {get_settings().database_url}")

    print("→ alembic upgrade head")
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        check=True,
    )

    init_database()
    db = check_db_connection()
    if not db.get("ok"):
        print(f"❌ 数据库连接失败: {db.get('error')}")
        return 1

    print(f"✅ 数据库连接 OK ({db.get('url_scheme')})")
    stats = import_all_to_db(force="--force" in sys.argv)
    print(f"✅ JSON 导入完成: {stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""启动时将 .env.local 注入 os.environ（alibaba_open_cli / Skill 子进程依赖）。"""

from __future__ import annotations

import os

from app.core.paths import PROJECT_ROOT


# 这些键以 .env.local 为准，避免 reload 后子进程仍用父进程注入的旧值
_ALWAYS_REFRESH_KEYS = frozenset({
    "TANGBUY_ADMIN_TOKEN",
    "ALIBABA_NEWTON_APIKEY",
    "ALI_1688_AK",
})


def load_env_local() -> None:
    for name in (".env.local", ".env"):
        path = PROJECT_ROOT / name
        if not path.exists():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            key = key.strip()
            if not key:
                continue
            if key in _ALWAYS_REFRESH_KEYS or key not in os.environ:
                os.environ[key] = value.strip()
        return

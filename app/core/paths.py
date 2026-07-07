"""项目路径（独立后端仓库根目录）。"""

from __future__ import annotations

import os
from pathlib import Path

# app/core/paths.py → parents[2] = 项目根 tangbuy-procurement-api/
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def get_work_root() -> Path:
    raw = os.environ.get("AGENT_WORK_ROOT", "").strip()
    if raw:
        return Path(raw).resolve()
    return PROJECT_ROOT


def data_dir() -> Path:
    return get_work_root() / "data"


def scripts_dir() -> Path:
    return get_work_root() / "scripts"


def workspace_dir() -> Path:
    return get_work_root() / "workspace"

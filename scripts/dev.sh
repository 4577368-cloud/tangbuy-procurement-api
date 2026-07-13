#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
  echo "未找到 .venv，正在用 Python 3.11 创建…" >&2
  /Users/panda/.local/bin/python3.11 -m venv "$ROOT/.venv"
  "$ROOT/.venv/bin/pip" install -U pip
  "$ROOT/.venv/bin/pip" install -r "$ROOT/requirements.txt"
fi

exec "$ROOT/.venv/bin/python" -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8001

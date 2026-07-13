#!/usr/bin/env bash
# 本机 API（127.0.0.1:8001），供 Next 同源代理；局域网用户只访问前端 3001 即可。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
  echo "未找到 .venv，正在用 Python 3.11 创建…" >&2
  /Users/panda/.local/bin/python3.11 -m venv "$ROOT/.venv"
  "$ROOT/.venv/bin/pip" install -U pip
  "$ROOT/.venv/bin/pip" install -r "$ROOT/requirements.txt"
fi

exec "$ROOT/.venv/bin/python" -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8001

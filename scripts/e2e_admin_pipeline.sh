#!/usr/bin/env bash
# Tangbuy 采购履约 → Admin 全链路手工测试（真实写 Admin）
# 用法: ./scripts/e2e_admin_pipeline.sh TI26070000093
# 环境: API http://127.0.0.1:8001  账号 admin / tangbuy123

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
API="${TANGBUY_API:-http://127.0.0.1:8001}"
ACCOUNT="${TANGBUY_ACCOUNT:-admin}"
PASSWORD="${TANGBUY_PASSWORD:-tangbuy123}"
TI="${1:-}"

if [[ -z "$TI" ]]; then
  echo "用法: $0 <ord_line_no>   例: $0 TI26070000093"
  exit 1
fi

COOKIE_JAR="$(mktemp)"
trap 'rm -f "$COOKIE_JAR"' EXIT

login() {
  curl -s -c "$COOKIE_JAR" -b "$COOKIE_JAR" -X POST "$API/api/auth/login" \
    -H "Content-Type: application/json" \
    -d "{\"account\":\"$ACCOUNT\",\"password\":\"$PASSWORD\"}" >/dev/null
}

api_get() { curl -s -b "$COOKIE_JAR" "$API$1"; }
api_post() {
  curl -s -b "$COOKIE_JAR" -X POST "$API$1" -H "Content-Type: application/json" -d "$2"
}

show_line() {
  python3 - "$TI" <<'PY'
import json, sys
ti = sys.argv[1]
d = json.load(sys.stdin)
r = d.get("row", d)
print(f"  {ti}  stat={r.get('ord_line_stat')} {r.get('ord_line_stat_nm')}  ord_no={r.get('ord_no')}")
print(f"  item: {(r.get('item_nm') or '')[:50]}")
PY
}

show_pipeline() {
  python3 - "$TI" <<'PY'
import json, sys
ti = sys.argv[1]
p = json.load(sys.stdin)
print(f"  pipeline_step={p.get('pipeline_step')}  stat={p.get('ord_line_stat')}")
for b in p.get("blockers") or []:
    print(f"  blocker: {b.get('key')} — {b.get('detail')}")
if p.get("last_error"):
    print(f"  last_error: {p.get('last_error')}")
PY
}

step() { echo ""; echo "=== $1 ==="; }

login
step "同步宽表"
api_post "/api/orders/sync" '{"mode":"incremental","page_size":50,"pages":2,"wait":true}' \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('  ok=',d.get('ok'),'updated=',(d.get('stats') or {}).get('updated'))"

step "当前子单"
api_get "/api/orders/$TI" | show_line
api_get "/api/orders/$TI/pipeline" | show_pipeline

STAT="$(api_get "/api/orders/$TI" | python3 -c "import sys,json;print(json.load(sys.stdin).get('row',{}).get('ord_line_stat',''))")"

case "$STAT" in
  0)
    step "接单 0→23 (pipeline/run → Admin confirmList)"
    api_post "/api/orders/pipeline/run" "{\"ord_line_nos\":[\"$TI\"]}" | python3 -m json.tool
    ;;
  23)
  54)
    step "流水线推进 (接单/预订购/下单)"
    api_post "/api/orders/pipeline/run" "{\"ord_line_nos\":[\"$TI\"]}" | python3 -m json.tool
    ;;
  *)
    step "按状态 $STAT 仅跑 pipeline/run（如需预订购/下单请确认状态）"
    api_post "/api/orders/pipeline/run" "{\"ord_line_nos\":[\"$TI\"]}" | python3 -m json.tool
    ;;
esac

# 若仍卡在 stat=54，可显式下单
STAT2="$(api_get "/api/orders/$TI" | python3 -c "import sys,json;print(json.load(sys.stdin).get('row',{}).get('ord_line_stat',''))")"
if [[ "$STAT2" == "54" ]]; then
  step "显式 1688 下单 54→55"
  api_post "/api/orders/1688/place-order" "{\"ord_line_nos\":[\"$TI\"],\"operator\":\"e2e-script\"}" \
    | python3 -m json.tool
fi

step "再次同步"
api_post "/api/orders/sync" '{"mode":"incremental","page_size":50,"pages":1,"wait":true}' \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('  ok=',d.get('ok'))"

step "结果"
api_get "/api/orders/$TI" | show_line
api_get "/api/orders/$TI/pipeline" | show_pipeline

echo ""
echo "Admin 后台核对: 子单 $TI 状态应为 1688待支付(55)"

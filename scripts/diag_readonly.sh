#!/usr/bin/env bash
# READ-ONLY live diagnostics for the crypto-bot droplet.
#
# Safety contract (enforced by tests/test_diag_readonly.py):
#   * Never restarts/starts/stops services, never pulls/pushes git, never
#     installs packages, never edits files, never places/cancels orders.
#   * Every SQLite access is opened read-only (`sqlite3 -readonly` or the
#     venv python `mode=ro` fallback) and only runs SELECT/PRAGMA.
#   * Never prints secrets: journald output is redacted; no .env reads, no
#     `printenv`/`env` dumps, no API keys/signatures.
#
# Usage (on the droplet, after an APPROVED `git pull`):
#   bash scripts/diag_readonly.sh
#
# Does NOT include the signed /account auth probe (that needs credentials and
# is run separately and manually only when an auth error is seen).
set -uo pipefail

APP="${APP:-/opt/crypto-trading-machine}"
DB="${DB:-$APP/data/cache/trading.db}"
# Redaction for any journald lines: mask key/secret/signature-looking values.
SED='s/(signature|apikey|api_key|secret|X-MBX-APIKEY)[=: "]+[A-Za-z0-9+\/=_-]{8,}/\1=REDACTED/Ig'

line(){ printf '\n===== %s =====\n' "$1"; }

# Read-only SQL helper: prefer the sqlite3 binary (-readonly), else fall back
# to the bot's venv python opened with mode=ro. Only ever used for SELECT/PRAGMA.
sq(){
  if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 -readonly "$DB" "$1"
  elif [ -x "$APP/.venv/bin/python" ]; then
    "$APP/.venv/bin/python" - "$DB" "$1" <<'PY'
import sqlite3, sys
db, q = sys.argv[1], sys.argv[2]
c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
for row in c.execute(q):
    print("|".join("" if v is None else str(v) for v in row))
PY
  else
    echo "(no sqlite3 binary and no venv python — cannot query DB)"
  fi
}

line "A HEALTH"
hostname; uptime
systemctl is-active crypto-bot crypto-bot-risk crypto-bot-watchdog.timer
systemctl show crypto-bot -p ActiveState -p SubState -p NRestarts -p ExecMainStartTimestamp
curl -s -o /dev/null -w "healthz=%{http_code}\n" http://127.0.0.1:8000/healthz
df -h /; free -m
echo "binance=$(curl -s https://api.binance.us/api/v3/time) local_ms=$(date -u +%s%3N)"

line "B ERRORS (48h, redacted)"
journalctl -u crypto-bot --since "48 hours ago" \
  | grep -iE "error|critical|traceback|halt|429|418|-1021|-2015|-1003|banned" \
  | sed -E "$SED" | tail -40
echo "--- last 30 lines after last restart ---"
journalctl -u crypto-bot --since "$(systemctl show crypto-bot -p ExecMainStartTimestamp --value)" \
  | sed -E "$SED" | head -30

line "C STATE (kv + heartbeats age)"
sq "SELECT key,substr(value,1,400) FROM kv WHERE key IN ('autopilot_state','emergency_halt','tick_protection','drawdown_halt','entry_status','health_status');"
sq "SELECT CASE WHEN COUNT(*)=0 THEN 'component_heartbeats: MISSING' ELSE 'component_heartbeats: present' END FROM sqlite_master WHERE type='table' AND name='component_heartbeats';"
sq "SELECT component,healthy,CAST((julianday('now')-julianday(ts))*86400 AS INT) age_s,substr(detail,1,100) FROM component_heartbeats ORDER BY component;"

line "D WHY NO ENTRIES (72h)"
sq "PRAGMA table_info(closed_trades);"
sq "SELECT substr(ts,1,10) d,reason,COUNT(*) n FROM tick_audit WHERE ts>=strftime('%Y-%m-%dT%H:%M:%S','now','-72 hours') GROUP BY d,reason ORDER BY d DESC,n DESC;"
sq "SELECT substr(ts,1,10) d,final_outcome,COUNT(*) n FROM trade_audit WHERE ts>=strftime('%Y-%m-%dT%H:%M:%S','now','-72 hours') GROUP BY d,final_outcome ORDER BY d DESC,n DESC;"
sq "SELECT substr(ts,1,10) d,COUNT(DISTINCT symbol) symbols,COUNT(DISTINCT CASE WHEN action='BUY' THEN symbol END) buys FROM tick_audit WHERE ts>=strftime('%Y-%m-%dT%H:%M:%S','now','-72 hours') GROUP BY d ORDER BY d DESC;"

line "E THROTTLES NOW"
sq "SELECT COALESCE(SUM(pnl),0) today_pnl,COUNT(*) closes FROM closed_trades WHERE exit_ts>=strftime('%Y-%m-%dT00:00:00','now');"
sq "SELECT symbol,pnl,exit_ts FROM closed_trades ORDER BY exit_ts DESC LIMIT 12;"
sq "SELECT symbol,qty,entry_price,mode FROM positions;"
sq "SELECT key,substr(value,1,300) FROM kv WHERE key IN ('autopilot_state','drawdown_halt','aggressive_mode','btc_regime','equity_snapshot','risk_loop_last_run');"

line "F CODE VERSION"
( cd "$APP" \
  && git rev-parse HEAD \
  && git log -1 --format='%h %ci %s' \
  && git status --short \
  && git ls-remote origin -h refs/heads/main \
  && echo "proc_started=$(systemctl show crypto-bot -p ExecMainStartTimestamp --value)" )

echo; echo ">>> diagnostics complete (read-only) <<<"

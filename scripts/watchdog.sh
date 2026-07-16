#!/usr/bin/env bash
# Watchdog for the crypto-trading-machine uvicorn/autopilot process.
#
# Run this on a schedule (cron) in any environment without systemd (e.g. this
# dev container). It detects two distinct failure modes that a plain
# `Restart=always` systemd unit would not catch on its own when systemd isn't
# available, and restarts the app if either is true:
#   1. The process/port is down entirely (curl to /health fails).
#   2. The process is up but the autopilot scheduler has stopped ticking
#      (last_tick_at older than MAX_STALE_MINUTES) — the exact failure mode
#      that went undetected for ~a month prior to this script existing.
#
# Usage: add to crontab, e.g. every 5 minutes:
#   */5 * * * * /workspaces/crypto-trading-machine/scripts/watchdog.sh >> /workspaces/crypto-trading-machine/logs/watchdog.log 2>&1
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

HOST="127.0.0.1"
PORT="8000"
MAX_STALE_MINUTES=20
LOG_FILE="$REPO_DIR/logs/uvicorn.log"
DB_FILE="$REPO_DIR/data/cache/trading.db"
# Raw stdout/stderr catch-all — anything printed before app.logging_setup
# attaches its own RotatingFileHandler on logs/uvicorn.log (startup tracebacks,
# asyncio warnings, "nohup: ignoring input", etc). Deliberately a DIFFERENT
# file than LOG_FILE: the app already writes its own structured, rotated copy
# of every log line to LOG_FILE, so redirecting shell stdout there too would
# double-write every line.
RAW_LOG_FILE="$REPO_DIR/logs/uvicorn.raw.log"
PID_FILE="$REPO_DIR/logs/uvicorn.pid"

log() {
    echo "$(date -u '+%Y-%m-%dT%H:%M:%S') [watchdog] $*"
}

start_app() {
    # On the droplet, crypto-bot.service is managed by systemd (Restart=always).
    # Always prefer `systemctl restart` there so we don't spawn a second,
    # unmanaged process alongside the one systemd tracks. Only fall back to a
    # bare nohup start where systemd isn't available (e.g. this dev container).
    if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files crypto-bot.service >/dev/null 2>&1; then
        log "restarting via systemctl..."
        systemctl restart crypto-bot
        log "systemctl restart issued"
        return
    fi
    log "starting uvicorn (no systemd unit found)..."
    # shellcheck disable=SC1091
    source "$REPO_DIR/.venv/bin/activate" 2>/dev/null || true
    nohup uvicorn app.main:app --host 0.0.0.0 --port "$PORT" >> "$RAW_LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    log "started uvicorn pid=$(cat "$PID_FILE")"
}

stop_app() {
    if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files crypto-bot.service >/dev/null 2>&1; then
        return  # systemctl restart (in start_app) handles stop+start atomically
    fi
    pkill -f "uvicorn app.main:app" || true
}

# --- 1. Is the process even up? ---
# /healthz (not /health) is the public, unauthenticated liveness route — see
# app/auth/middleware.py's _PUBLIC_EXACT. Everything else, including
# /autopilot/status, sits behind the login wall, so it can't be curled here
# without a session.
if ! curl -fsS -o /dev/null "http://$HOST:$PORT/healthz" 2>/dev/null; then
    log "health check failed (process down or not responding) — restarting"
    start_app
    exit 0
fi

# --- 2. Is the process up but the scheduler stalled? ---
# Read last_tick_at straight out of the kv-backed autopilot_state row in
# SQLite instead of the authenticated /autopilot/status endpoint — avoids
# needing a login session and matches exactly what the app itself persists.
last_tick_at="$(python3 - "$DB_FILE" <<'PY' 2>/dev/null || echo ""
import json
import sqlite3
import sys

path = sys.argv[1]
try:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    row = conn.execute("SELECT value FROM kv WHERE key='autopilot_state'").fetchone()
    if row:
        print(json.loads(row[0]).get("last_tick_at") or "")
except Exception:
    pass
PY
)"

if [[ -z "$last_tick_at" ]]; then
    log "no last_tick_at reported yet (autopilot may not be started) — no action"
    exit 0
fi

now_epoch="$(date -u +%s)"
tick_epoch="$(date -u -d "$last_tick_at" +%s 2>/dev/null || echo "$now_epoch")"
age_minutes=$(( (now_epoch - tick_epoch) / 60 ))

if (( age_minutes > MAX_STALE_MINUTES )); then
    log "last tick was ${age_minutes}m ago (> ${MAX_STALE_MINUTES}m) — scheduler appears stalled, restarting"
    stop_app
    sleep 2
    start_app
else
    log "ok — last tick ${age_minutes}m ago"
fi

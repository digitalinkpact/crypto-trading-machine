#!/usr/bin/env bash
# READ-ONLY production preflight for the droplet. Run as root:
#
#   bash scripts/preflight_readonly.sh 2>&1 | tee /root/preflight-$(date -u +%Y%m%dT%H%M%SZ).log
#
# Never writes git, .env, systemd, the database, or exchange state. The DB is
# opened with SQLite mode=ro; Binance.US calls are account/openOrders/ticker
# only. Secret-looking lines in unit files are redacted; keys are never printed.
set -uo pipefail

APP_DIR=${APP_DIR:-/opt/crypto-trading-machine}
ENV_FILE=${ENV_FILE:-/var/lib/crypto-bot/.env}
APP_USER=${APP_USER:-cryptobot}
PY=$APP_DIR/.venv/bin/python
REDACT='s/((KEY|SECRET|TOKEN|PASSWORD|PASS|SMTP)[A-Z_]*=).*/\1<redacted>/I'

section() { printf '\n===== %s =====\n' "$*"; }
cd "$APP_DIR" || { echo "FATAL: cannot cd $APP_DIR"; exit 1; }

section "host"
hostname; date -u; uptime; [ -f /var/run/reboot-required ] && echo "NOTE: reboot-required flag present"
df -h "$APP_DIR" | tail -1; free -m | sed -n 2p

section "git (no fetch)"
git -c safe.directory="$APP_DIR" rev-parse HEAD
git -c safe.directory="$APP_DIR" log -1 --format='%h %cd %s' --date=iso
git -c safe.directory="$APP_DIR" rev-parse --abbrev-ref HEAD
echo "-- status --porcelain (tracked + untracked, ignored excluded):"
git -c safe.directory="$APP_DIR" status --porcelain
echo "-- stashes:"; git -c safe.directory="$APP_DIR" stash list | head
echo "-- remote main:"; git -c safe.directory="$APP_DIR" ls-remote origin refs/heads/main 2>&1 | head -1

section "systemd units + drop-ins (secrets redacted)"
for u in crypto-bot.service crypto-bot-risk.service crypto-bot-watchdog.service crypto-bot-watchdog.timer; do
  printf -- '--- %s: enabled=%s active=%s\n' "$u" "$(systemctl is-enabled "$u" 2>&1)" "$(systemctl is-active "$u" 2>&1)"
  systemctl cat "$u" 2>&1 | sed -E "$REDACT"
done
echo "-- MainPID / restarts:"
for u in crypto-bot crypto-bot-risk; do
  systemctl show "$u" -p MainPID -p NRestarts -p ActiveEnterTimestamp -p ExecMainStartTimestamp 2>&1
done
echo "-- deployed unit vs repo unit:"
for u in crypto-bot crypto-bot-risk crypto-bot-watchdog; do
  for ext in service timer; do
    src=deploy/systemd/$u.$ext; dst=/etc/systemd/system/$u.$ext
    [ -f "$src" ] || continue
    if [ -f "$dst" ]; then diff -q "$src" "$dst" >/dev/null && echo "same  $dst" || { echo "DIFF  $dst"; diff "$src" "$dst" | sed -E "$REDACT"; }
    else echo "MISSING $dst"; fi
  done
done
ls -la /etc/systemd/system/crypto-bot*.d 2>/dev/null

section "restart automation (timers / cron)"
systemctl list-timers --all --no-pager 2>/dev/null | grep -i crypto
for u in root "$APP_USER"; do echo "-- crontab $u:"; crontab -l -u "$u" 2>&1 | grep -v '^#' | sed -E "$REDACT"; done
grep -ls crypto /etc/cron.d/* /etc/cron.*/* 2>/dev/null

section "processes"
ps -eo pid,ppid,user,etime,args --sort=pid | grep -E "uvicorn|app\.main|crypto|python" | grep -v -E "grep|preflight_readonly"

section "listening ports"
ss -ltnp 2>/dev/null

section "env file (names + mode flags only)"
ls -la "$ENV_FILE"
grep -E '^\s*(TRADING_MODE|LIVE_MODE|PAPER_TRADING|DRY_RUN|LIVE_BUYS_ENABLED|EXCHANGE_NATIVE_STOPS_ENABLED|EMERGENCY_HALT_ENABLED|DATA_CACHE_DIR)=' "$ENV_FILE"
echo "-- variable names present:"; grep -oE '^\s*[A-Z_][A-Z0-9_]*=' "$ENV_FILE" | tr -d '=' | sort | tr '\n' ' '; echo
echo "-- mode overrides in unit environment:"; systemctl show crypto-bot -p Environment | grep -oE '(TRADING_MODE|LIVE_MODE|PAPER_TRADING|DRY_RUN|LIVE_BUYS_ENABLED)=[^ ]*'

section "effective settings + DB + exchange (python, read-only, as $APP_USER)"
runuser -u "$APP_USER" -- env APP_ENV_FILE="$ENV_FILE" "$PY" - <<'PYEOF'
import asyncio, json, os, sqlite3
from decimal import Decimal
from pathlib import Path
from app.config import Settings

env = Path(os.environ["APP_ENV_FILE"])
s = Settings(_env_file=env)
names = ("trading_mode", "live_mode", "paper_trading", "dry_run", "live_buys_enabled",
         "binance_base_url", "data_cache_dir", "ledger_mode",
         "risk_per_trade_pct", "max_position_pct", "max_open_positions",
         "aggressive_max_open_positions", "rollback_max_open_positions",
         "max_long_exposure_pct", "kelly_fraction_cap", "stop_loss_pct",
         "atr_stop_min_pct", "atr_stop_max_pct", "take_profit_1_pct",
         "take_profit_1_fraction", "take_profit_2_pct", "take_profit_2_fraction",
         "trailing_stop_pct", "daily_loss_limit_pct", "drawdown_circuit_breaker_pct",
         "emergency_halt_enabled", "exchange_native_stops_enabled",
         "market_regime_gate_enabled", "breadth_gate_enabled", "ml_gate_enabled",
         "profitstream_score_threshold", "entry_strategy")
eff = {}
for n in names:
    try:
        v = getattr(s, n); eff[n] = getattr(v, "value", v)
    except Exception as exc:  # noqa: BLE001
        eff[n] = f"<n/a: {type(exc).__name__}>"
print(json.dumps(eff, indent=1, default=str))
print("api_key_present:", bool(s.binance_api_key.get_secret_value()),
      "api_secret_present:", bool(s.binance_api_secret.get_secret_value()))

db = Path(s.data_cache_dir) / ("trading.db" if s.ledger_mode == "live" else "simulation/trading.db")
if not db.is_absolute():
    db = Path("/opt/crypto-trading-machine") / db
print("\n-- database:", db.resolve(), "exists:", db.exists(),
      "size:", db.stat().st_size if db.exists() else None)
for side in ("-wal", "-shm"):
    p = Path(str(db) + side)
    print(f"   {p.name}: {p.stat().st_size if p.exists() else 'absent'}")
if not db.exists():
    raise SystemExit("FATAL: ledger database not found — STOP CONDITION")
c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
c.row_factory = sqlite3.Row
print("integrity_check:", [r[0] for r in c.execute("PRAGMA integrity_check")][:5])
print("journal_mode:", c.execute("PRAGMA journal_mode").fetchone()[0])
tables = [r[0] for r in c.execute("select name from sqlite_master where type='table'")]
for t in ("positions", "orders", "closed_trades", "order_intents", "trade_audit", "tick_runs", "kv"):
    if t in tables:
        print(f"   rows {t}: {c.execute(f'select count(*) from {t}').fetchone()[0]}")

print("\n-- positions:")
for r in c.execute("select symbol, mode, qty, entry_price, entry_ts from positions order by mode, symbol"):
    print("  ", dict(r))
if "order_intents" in tables:
    print("-- unresolved order intents:")
    for r in c.execute("select client_order_id, mode, symbol, side, qty, status, recorded, updated_at from order_intents "
                       "where status in ('SUBMITTING','UNKNOWN','NEW','PARTIALLY_FILLED') or (status='FILLED' and recorded=0)"):
        print("  ", dict(r))

def kv(key):
    row = c.execute("select value from kv where key=?", (key,)).fetchone()
    if row is None:
        return "<MISSING>"
    try:
        return json.loads(row[0])
    except ValueError:
        return f"<UNPARSEABLE {len(row[0])} bytes>"

print("\n-- safety state (kv):")
for k in ("drawdown_halt", "emergency_halt", "tick_protection", "entry_status", "baseline_unavailable",
          "order_outcome_unknown", "circuit_breaker_check_failed", "exchange_filters_unavailable",
          "ml_gate_unavailable", "accounting_status"):
    print(f"   {k}: {json.dumps(kv(k), default=str)[:400]}")
ap = kv("autopilot_state")
if isinstance(ap, dict):
    print("   autopilot_state:", json.dumps({k: ap.get(k) for k in (
        "running", "mode", "started_at", "starting_balance_usdt", "last_tick_at",
        "last_action", "last_error", "trades_executed")}, default=str))
else:
    print("   autopilot_state:", ap)
scan = kv("profitstream_scan")
if isinstance(scan, dict):
    print("   profitstream_scan:", json.dumps({k: scan.get(k) for k in (
        "timestamp", "btc_regime_label", "market_allowed", "breadth", "actions",
        "highest_score", "score_threshold", "qualified_buy_candidates")}, default=str)[:800])
if "tick_runs" in tables:
    print("-- last ticks:")
    for r in c.execute("select started_at, completed_at, mode, source, status from tick_runs order by started_at desc limit 5"):
        print("  ", dict(r))
if "orders" in tables:
    print("-- last 8 orders:")
    cols = [r[1] for r in c.execute("pragma table_info(orders)")]
    want = [x for x in ("ts", "created_at", "mode", "symbol", "side", "qty", "price", "status", "client_order_id") if x in cols]
    for r in c.execute(f"select {','.join(want)} from orders order by rowid desc limit 8"):
        print("  ", dict(r))
live_positions = {r["symbol"]: float(r["qty"]) for r in c.execute("select symbol, qty from positions where mode='live'")}
c.close()

async def exchange():
    from app.exchange.client import BinanceUSClient
    cl = BinanceUSClient()
    print("\n-- Binance.US:")
    try:
        print("   BTCUSDT price:", await cl.ticker_price("BTCUSDT"))
    except Exception as exc:  # noqa: BLE001
        print("   PUBLIC REST FAILED:", type(exc).__name__, exc)
    try:
        acct = await cl.account()
        print("   canTrade:", acct.get("canTrade"), "canWithdraw:", acct.get("canWithdraw"),
              "accountType:", acct.get("accountType"), "permissions:", acct.get("permissions"))
        bal = {b["asset"]: (Decimal(b["free"]), Decimal(b["locked"])) for b in acct.get("balances", [])
               if Decimal(b["free"]) + Decimal(b["locked"]) > 0}
        for a, (f, l) in sorted(bal.items()):
            print(f"   balance {a}: free={f} locked={l}")
        print("-- local live positions vs exchange holdings:")
        for sym, q in sorted(live_positions.items()):
            asset = sym[:-4] if sym.endswith("USDT") else sym
            f, l = bal.get(asset, (Decimal(0), Decimal(0)))
            print(f"   {sym}: local={q} exchange_total={f + l} (free={f} locked={l})")
    except Exception as exc:  # noqa: BLE001
        print("   SIGNED ACCOUNT FAILED:", type(exc).__name__, exc)
    try:
        orders = await cl.open_orders()
        print(f"-- open orders: {len(orders)}")
        for o in orders:
            print("  ", {k: o.get(k) for k in ("symbol", "side", "type", "status", "origQty",
                                               "executedQty", "price", "stopPrice", "clientOrderId", "time")})
    except Exception as exc:  # noqa: BLE001
        print("   OPEN ORDERS FAILED:", type(exc).__name__, exc)

asyncio.run(exchange())
PYEOF

section "service health endpoints"
curl -s -m 5 localhost:8000/healthz; echo
curl -s -m 10 localhost:8000/health | head -c 3000; echo

section "recent log signals"
LOG=$APP_DIR/logs/uvicorn.log
ls -la "$LOG" 2>/dev/null
grep -E "TRADING MODE|CRITICAL|EMERGENCY|HALT|safety_state_unknown|final_safety|startup|Traceback|ERROR" "$LOG" 2>/dev/null \
  | sed -E "$REDACT" | tail -n 60
echo "-- last 15 lines:"; tail -n 15 "$LOG" 2>/dev/null | sed -E "$REDACT"

section "done (read-only; nothing was changed)"

#!/bin/bash
# Read-only trade monitor (every 60s): fills, submitted-not-filled, internal
# gate reasons, tracked positions. Never calls signed exchange endpoints.
# Usage: scripts/watch_trades.sh [live|paper]

MODE="${1:-live}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${PYTHON:-$REPO_ROOT/.venv/bin/python}"
[ -x "$PYTHON" ] || PYTHON=python3

while true; do
  (cd "$REPO_ROOT" && MONITOR_MODE="$MODE" "$PYTHON" - <<'PYSCRIPT'
import os
from collections import Counter
from datetime import datetime, timezone

from app.trading import audit_labels

mode = os.environ["MONITOR_MODE"]
with audit_labels.connect_readonly() as conn:
    rows = audit_labels.fetch_trade_audit(conn, mode=mode, limit=500)
    positions = conn.execute("SELECT COUNT(*) FROM positions WHERE mode=?", (mode,)).fetchone()[0]

stages = Counter((audit_labels.execution_stage(r), r.get("signal")) for r in rows)
internal = Counter(audit_labels.rejection_reason(r) for r in rows
                   if audit_labels.execution_stage(r) == audit_labels.INTERNAL_REJECTED)
not_filled = sum(n for (stage, _), n in stages.items() if stage == audit_labels.SUBMITTED_NOT_FILLED)

print(f"\n[{datetime.now(timezone.utc):%H:%M:%S} UTC] TRADE MONITOR mode={mode} (last 500 decisions)")
print(f"  Filled on Binance.US: {stages[(audit_labels.FILLED, 'BUY')]} BUY + {stages[(audit_labels.FILLED, 'SELL')]} SELL")
print(f"  Submitted, not filled: {not_filled}")
print(f"  Tracked open positions: {positions}")
print("  Internal gate reasons (not sent to Binance.US), top 3:")
for reason, count in internal.most_common(3):
    print(f"    - {reason:28} {count}x")
PYSCRIPT
  )
  sleep 60
done

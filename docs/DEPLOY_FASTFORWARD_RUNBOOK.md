# Fast-forward deployment runbook (halt-state fix)

Operator-run, step by step, as root on the droplet. Paste each step's output
for review before running the next. **Stop at the first failed check.**

Never use `scripts/deploy-now.sh` (it runs `git reset --hard`, forces trading
flags into `.env`, and overwrites the unit) or `scripts/bootstrap_droplet.sh`
(reinstalls deps and restarts) for this deployment.

This deployment does **not** enable live BUYs. `LIVE_BUYS_ENABLED` is a
separate, explicitly approved change.

## 0. Preflight (read-only) — gate for everything below

```bash
TARGET=<full 40-char hash approved for deployment>
curl -fsSL "https://raw.githubusercontent.com/digitalinkpact/crypto-trading-machine/$TARGET/scripts/preflight_readonly.sh" -o /root/preflight_readonly.sh
cd /opt/crypto-trading-machine && TARGET=$TARGET bash /root/preflight_readonly.sh 2>&1 | tee /root/preflight-$(date -u +%Y%m%dT%H%M%SZ).log
```

Stop if any of these hold:

- tracked files modified in `git status --porcelain` (lines not starting `??`), or unexplained untracked files;
- more than one `uvicorn app.main:app` process, or port 8000 not owned by `crypto-bot`'s MainPID;
- an unidentified process holds `trading.db` open;
- `integrity_check` is not `['ok']`;
- unresolved order intents or `order_outcome_unknown` present;
- local live positions differ from exchange holdings, or open orders without the `pstop-` prefix are unexplained;
- an active `drawdown_halt` / `emergency_halt` / `tick_protection` without a known reason;
- **`LIVE_BUYS_ENABLED=true`** — deploying would activate live BUYs; requires explicit approval first;
- signed Binance.US calls fail, or `canTrade` is not `True`.

## 1. Variables and backup directory

```bash
set -euo pipefail
APP=/opt/crypto-trading-machine
TARGET=<full 40-char hash approved for deployment>
TS=$(date -u +%Y%m%dT%H%M%SZ)
BK=/root/ps-backup-$TS
REPO_OWNER=$(stat -c %U "$APP/.git")
DB=$APP/data/cache/trading.db
install -d -m 700 "$BK"
df -h / "$APP" ; du -h "$DB"* ; echo "backup dir: $BK owner of repo: $REPO_OWNER"
```

Require free space ≥ 3× the DB size.

## 2. Preserve git state (no changes)

```bash
cd "$APP"
export GIT_OPTIONAL_LOCKS=0
sudo -u "$REPO_OWNER" git rev-parse HEAD | tee "$BK/prev_commit"
sudo -u "$REPO_OWNER" git status --porcelain | tee "$BK/git_status.txt"
sudo -u "$REPO_OWNER" git diff > "$BK/worktree.diff"
sudo -u "$REPO_OWNER" git ls-files --others --exclude-standard > "$BK/untracked.txt"
```

## 3. Preserve service configuration and env file

```bash
mkdir -m 700 "$BK/systemd"
cp -a /etc/systemd/system/crypto-bot* "$BK/systemd/"
for u in crypto-bot crypto-bot-risk crypto-bot-watchdog.service crypto-bot-watchdog.timer; do
  systemctl cat "$u" > "$BK/systemd/$u.effective" 2>&1 || true
  systemctl is-enabled "$u" > "$BK/systemd/$u.enabled" 2>&1 || true
done
cp -a /var/lib/crypto-bot/.env "$BK/env.bak" && chmod 600 "$BK/env.bak"
ls -la "$BK" "$BK/systemd"
```

The env backup is never printed.

## 4. Pause restart automation

The watchdog timer restarts `crypto-bot` whenever `/healthz` fails; pause it so
it cannot race the controlled restart. Leave `crypto-bot-risk.service` (if
active) running — it keeps app-side exits alive and is serialized with the
in-process risk loop by the `risk_loop` kv lock.

```bash
systemctl stop crypto-bot-watchdog.timer
systemctl is-active crypto-bot-watchdog.timer crypto-bot-watchdog.service || true
```

## 5. Online database backup + verification

SQLite online backup from a read-only source connection; safe with the live
writer running.

```bash
runuser -u cryptobot -- "$APP/.venv/bin/python" - "$DB" "$BK/trading.db" <<'PY'
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True, timeout=30)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close(); src.close()
PY
chmod 600 "$BK/trading.db"
python3 - "$DB" "$BK/trading.db" <<'PY'
import sqlite3, sys
for p in sys.argv[1:]:
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    print(p, c.execute("pragma integrity_check").fetchone()[0],
          {t: c.execute(f"select count(*) from {t}").fetchone()[0]
           for t in ("positions", "orders", "order_intents", "closed_trades", "kv")})
PY
```

Backup integrity must be `ok`, and its counts must match the live DB (or the
live DB may only be ahead by rows written in the last seconds).

> `runuser -u cryptobot` needs write access to `$BK`; if it fails, run the
> backup as root instead (root can read the DB).

## 6. Fetch and verify the target commit

```bash
cd "$APP"
sudo -u "$REPO_OWNER" git fetch origin main
sudo -u "$REPO_OWNER" git cat-file -t "$TARGET"                     # expect: commit
sudo -u "$REPO_OWNER" git merge-base --is-ancestor HEAD "$TARGET" && echo FAST-FORWARD-OK
sudo -u "$REPO_OWNER" git merge-base --is-ancestor "$TARGET" origin/main && echo TARGET-ON-MAIN
sudo -u "$REPO_OWNER" git diff --stat HEAD "$TARGET"
sudo -u "$REPO_OWNER" git diff --quiet HEAD "$TARGET" -- requirements.txt deploy/ && echo "deps+units unchanged"
```

Stop unless both `FAST-FORWARD-OK` and `TARGET-ON-MAIN` print, and the file
list matches the reviewed change set exactly.

## 7. Fast-forward (tracked files only)

```bash
sudo -u "$REPO_OWNER" git merge --ff-only "$TARGET"
sudo -u "$REPO_OWNER" git rev-parse HEAD        # must equal $TARGET
sudo -u "$REPO_OWNER" git status --porcelain    # must equal $BK/git_status.txt
runuser -u cryptobot -- "$APP/.venv/bin/python" -m py_compile app/trading/startup.py app/storage/db.py scripts/check_live_buy_readiness.py && echo COMPILE-OK
```

`merge --ff-only` refuses rather than overwriting local changes; untracked
files are untouched. Requirements and units are unchanged, so no `pip install`
and no `daemon-reload`.

## 8. Controlled restart

Expected startup behavior (from `app/main.py` lifespan):

1. single-instance `fcntl` lock on `data/cache/.crypto-bot.lock` (a second instance fails to start);
2. Binance.US filter preload;
3. `verify_before_trading`: seeds **only missing** `drawdown_halt` / `emergency_halt` / `tick_protection` rows as inactive (logs `startup initialized missing halt state(s)`), preserves legacy drawdown halts, halts entries on pending intents, reconciliation mismatches, unexpected open orders or missing stops;
4. price stream → in-process risk loop (may immediately send **protective SELLs** for positions already past stop/TP) → scheduler → ticks.

New BUYs cannot be sent while `LIVE_BUYS_ENABLED=false`. Expected
interruption: roughly 5–60 s of app-side exit coverage (none if the separate
risk service is active, which keeps running).

```bash
systemctl restart crypto-bot
sleep 20
systemctl show crypto-bot -p ActiveState -p SubState -p MainPID -p NRestarts
grep -E "TRADING MODE|startup initialized missing halt|EMERGENCY HALT|startup|CRITICAL|Traceback" "$APP/logs/uvicorn.log" | tail -n 30
```

## 9. Post-deployment verification

```bash
pgrep -fc "uvicorn app.main:app"                               # must be 1
ss -ltnpH 'sport = :8000'                                      # pid must equal MainPID
sudo -u "$REPO_OWNER" git -C "$APP" rev-parse HEAD             # must equal $TARGET
curl -fsS localhost:8000/healthz
TARGET=$TARGET bash /root/preflight_readonly.sh 2>&1 | tee "$BK/postflight.log"
runuser -u cryptobot -- env APP_ENV_FILE=/var/lib/crypto-bot/.env "$APP/.venv/bin/python" -m scripts.check_live_buy_readiness
```

Required in the postflight:

- the three halt rows are present; previously active ones are **still active**;
- `integrity_check` `ok`; positions and open orders identical to the preflight (except protective SELLs you can account for);
- `risk_loop_last_run` age < 2 min; `autopilot_last_completed_tick` advancing after the next tick;
- no new `EMERGENCY HALT` lines unless explained.

Then re-enable restart automation:

```bash
systemctl start crypto-bot-watchdog.timer && systemctl is-active crypto-bot-watchdog.timer
```

## 10. Rollback (code only — never restore an old DB over newer activity)

```bash
cd /opt/crypto-trading-machine
PREV=$(cat "$BK/prev_commit")
systemctl stop crypto-bot-watchdog.timer
sudo -u "$REPO_OWNER" git -c advice.detachedHead=false checkout "$PREV"
systemctl restart crypto-bot
```

- `git checkout` refuses to overwrite local modifications and leaves untracked files alone. Return to the branch later with `git checkout main`.
- The seeded inactive halt rows are harmless to the previous code (every reader checks `.get("active")`); leave them.
- Restore `$BK/env.bak` or `$BK/systemd/*` only if those files were changed (this deployment changes neither).
- Restore `$BK/trading.db` **only** if the live DB fails `integrity_check` **and** `orders`, `order_intents`, `closed_trades` and `positions` show no activity after the backup time; otherwise keep the live DB and reconcile from Binance.US.

## Emergency: block new BUYs, keep protection

`LIVE_BUYS_ENABLED=false` (the state this runbook deploys in) already blocks
every BUY at the exchange wrapper and the final gate. Do not stop
`crypto-bot` or `crypto-bot-risk` merely to stop BUYs — that also stops
app-side stop-loss/take-profit/trailing exits.

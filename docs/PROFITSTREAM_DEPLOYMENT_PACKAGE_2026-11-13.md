# ProfitStream — Deployment Package (2026-11-13)

Status: **prepared in development, NOT deployed, NOT pushed.** Production
(`crypto-machine`, 64.23.171.8) runs `7819d3b`. This release is
`7819d3b..<release commit>`:

1. `4569263`: execution lifecycle hardening (already on `origin/main`, never deployed).
2. `513d696`: watchdog liveness, audit labels, test isolation.
3. The release commit: fill reconciliation, monitoring fixes, review fixes, and DB tools.

No strategy setting changed: the 80 threshold, BTC regime / strong-bull /
breadth gates, sizing, risk limits, SL/TP/trailing, and credentials are as
before. No position, order, or production record was touched.

## 1. Defects fixed

| # | Defect | Where | Effect before fix |
|---|---|---|---|
| 1 | Watchdog liveness read `autopilot_last_success`, which is written only for `EXECUTED` ticks (regression in `4569263`). | `watchdog.py::_health_loop`, `autopilot.py::Autopilot.tick` | A protection-blocked BUY looks like a stalled tick, which triggers a false emergency halt. Fix: `autopilot_last_completed_tick` is written only for completed, error-free ticks (`EXECUTED` or `BLOCKED`). |
| 2 | Internal gate rejections were written as `binance_response="REJECTED"`. | `autopilot.py::_execute._finish` | Strategy rejections looked like Binance rejections. They are now `NOT_SUBMITTED`; confirmed fills are `FILLED`. |
| 3 | pytest could load the live `.env` and the live DB. | `config.py`, `storage/db.py`, `exchange/client.py` | 75 test rows were written into production `trade_audit`. Fix: guards that work without `conftest.py`. |
| 4 | **Regression in `513d696`:** the test-row filter was `NOT (json_extract(detail,'$.agents') = '["test"]')`, which is NULL for rows without `agents`. | `storage/db.py::recent_trade_audit` | It hid every genuine row with no `agents` key (≈36k production rows, including all `live_buy_kill_switch` rows) from LIVE diagnostics. Fix: `IS`. |
| 5 | A MARKET order ending `EXPIRED`/`CANCELED` with `executedQty > 0` was logged "NOT filled — not recording". | `autopilot.py::_order_filled`, `_submit_locked` | Coins bought or sold on Binance.US were missing from the book. Executed partial quantity is now booked. |
| 6 | Mode-scoped risk keys (`hwm:/lwm:/tp1:/tp2:`) were not mirrored to legacy keys. | `risk.py::_put` | A rollback to `7819d3b` would read stale TP/trailing state. Live-mode writes now also update the legacy key. |
| 7 | `_place_buy`/`_place_sell` wrote "rejected: exchange did not fill" when `_submit` had refused internally, or when the client kill switch returned a local `REJECTED`. | `autopilot.py::_order_outcome_labels` | These were mislabelled as Binance rejections. A Binance rejection now requires an exchange order id. |
| 8 | Monitoring scripts read raw `tick_audit` and `trade_audit` with no test-row filter, labelled `REJECTED` as an exchange response, and `watch_trades.sh` called a signed `account()` endpoint. `trade_debugger` imported the global `Storage`, which **migrates the DB on open**. | `scripts/*` | Reports were contaminated or misleading, and a "monitor" could alter the schema. All scripts are now read-only (`mode=ro`), stage-labelled, and test-filtered; monitors that show several modes label each row with its mode. |
| 9 | Live fills were never verified against exchange trade records. | new `app/trading/fill_reconcile.py` | See §2. |

## 2. Exchange fill reconciliation (`app/trading/fill_reconcile.py`)

The reconciler only reads the exchange (`get_order_by_client_id` and `myTrades`). It never places, cancels, or amends an order.

**Candidates:**
- Pending intents.
- Recorded intents that are not yet verified.
- Rows with a discrepancy.
- Live `orders` rows from the last 7 days that are not yet verified.

| Stage | Meaning |
|---|---|
| `UNVERIFIED` | The lookup was inconclusive (network or API error), the order was absent within the 5-minute grace window, or it was absent beyond the 30-day order-history margin. The intent is never resolved. |
| `NOT_ON_EXCHANGE` | Binance returned "unknown order" between 5 minutes and 30 days after the intent, and nothing is booked locally. |
| `ACCEPTED` | `NEW` with no execution. |
| `PARTIALLY_FILLED` | The order is still open with some execution. |
| `FILLS_PENDING` | The order says it filled, but the `myTrades` quantity does not match yet. **An API success alone is never treated as a fill.** |
| `FILLED_VERIFIED` / `PARTIAL_TERMINAL_VERIFIED` | `executedQty` equals the sum of `myTrades` (deduplicated by trade id, filtered by orderId). |
| `CANCELED` / `EXPIRED` / `EXCHANGE_REJECTED` | Terminal, with no execution. |

**Discrepancies.** Any of these engages `new_entries_blocked`; SELL risk management keeps running:
- `exchange_fill_not_recorded`: any executed quantity, including an open partial, that is missing from the book.
- `qty_mismatch`.
- `local_fill_without_exchange_execution`.
- `local_fill_without_exchange_order`.

An intent is resolved only when it reaches a terminal stage with no discrepancy.

Results are stored in the new additive table `fill_reconciliation`. `GET /autopilot/status` (and the dashboard) shows the latest summary under `fill_reconciliation`.

The reconciler is **not scheduled automatically.** Run it manually after deployment (§6), then decide whether to schedule it.

## 3. Files changed (`git diff --stat 7819d3b..HEAD`)

Run `git diff --stat 7819d3b..HEAD` for the authoritative list. The full diff is `git diff 7819d3b..HEAD`.

- **App:**
  - `app/trading/autopilot.py`, `watchdog.py`, `risk.py`
  - `fill_reconcile.py` (new), `audit_labels.py` (new)
  - `app/storage/db.py`, `app/exchange/client.py`, `app/config.py`, `app/api/routes.py`
  - plus the `4569263` files
- **Scripts:**
  - `reconcile_fills.py` (new), `archive_tick_audit.py` (new)
  - `monitor_tick_events.py`, `monitor_trades.py`, `trade_dashboard.py`, `watch_trades.sh`, `monitor_sell_executions.py`, `trade_debugger.py`
  - `backtest_live_gate_stack.py`
- **Tests:**
  - `test_fill_reconcile.py`, `test_audit_labels.py`, `test_archive_tick_audit.py` (new)
  - `test_tick_liveness.py`, `test_live_test_separation.py`
  - `tests/conftest.py` (from `4569263`; production currently has none)

## 4. Migration (verified on a production-shaped copy)

The first start of the new code runs `Storage._init`:

- `CREATE TABLE IF NOT EXISTS` for `tick_runs`, `order_intents`, `fill_reconciliation`.
- `ALTER TABLE … ADD COLUMN` for `tick_audit.tick_id` and the `orders` columns `status`, `filled_qty`, `tick_id`.
- `PRAGMA user_version=1`.

A drill on a 30k-row `tick_audit` / 20k-row `trade_audit` extract of production (original DDL, `user_version=0`):

- `tick_audit` and `trade_audit` **root pages unchanged**, so there is no rebuild.
- The page count went from 26,627 to 26,635 (+8).
- The migration took 0.31 s.
- Row hashes are identical before and after.
- `integrity_check` is ok.

`ADD COLUMN` with a NULL default is a schema-only change in SQLite, so the 9 GB table is not rewritten.

**Rollback compatibility:** `7819d3b` code opened the migrated copy successfully, because it ignores the new tables and columns.

## 5. Backup (before deploy, with owner approval)

The DB is in WAL mode (header byte 18 = 2). **Do not `cp` it while the bot runs.** Use the online backup API.

```bash
ssh root@64.23.171.8
cd /opt/crypto-trading-machine
REL=<release-commit>; B=/var/backups/crypto-bot/$REL; mkdir -p -m 700 $B
git rev-parse HEAD > $B/predeploy-rev.txt                          # expect 7819d3b…
cp -p /var/lib/crypto-bot/.env $B/service.env                      # stays 0600
systemctl cat crypto-bot > $B/crypto-bot.service
nice -n 19 ionice -c3 sqlite3 data/cache/trading.db ".backup '$B/trading-predeploy.db'"   # ~9.2 GB; 73 GB free
nice -n 19 ionice -c3 sqlite3 "$B/trading-predeploy.db" "PRAGMA integrity_check;"         # expect: ok
sqlite3 "file:$B/trading-predeploy.db?mode=ro" "select max(id) from tick_audit; select max(id) from trade_audit; select count(*) from closed_trades;"
sha256sum $B/trading-predeploy.db > $B/trading-predeploy.db.sha256
```

The existing `/var/backups/crypto-bot/4569263/` holds:
- `trading-consistent.db` (8.85 GB, 2026-11-11 12:57 UTC; readable, WAL header, last tick 12:57:09Z).
- A **5 GB `trading-incomplete.db` partial plus a stray `-journal`.** It is unusable and only uses disk space. Removing it is an owner decision; I did not touch it.

**Restore** (only if corruption is proven, with the service stopped):

```bash
systemctl stop crypto-bot
mv data/cache/trading.db data/cache/trading.db.bad-$(date -u +%Y%m%dT%H%MZ)
rm -f data/cache/trading.db-wal data/cache/trading.db-shm
cp -p $B/trading-predeploy.db data/cache/trading.db && chown cryptobot:cryptobot data/cache/trading.db
sqlite3 data/cache/trading.db "PRAGMA integrity_check;"
```

The backup → integrity check → restore round trip was verified on the copy: every table hash matched.

## 6. Deploy (requires explicit owner approval; not executed)

```bash
# Codespaces: publish the release (approval required)
git push origin main

# Server
ssh root@64.23.171.8
cd /opt/crypto-trading-machine
git status --porcelain                         # must be empty (it is today)
git fetch origin && git checkout <release-commit>
grep -E "^(TRADING_MODE|LIVE_MODE|PAPER_TRADING|DRY_RUN|LIVE_BUYS_ENABLED)=" /var/lib/crypto-bot/.env   # values only; unchanged
.venv/bin/pip install -r requirements.txt      # no new pins in this release
sudo -u cryptobot env -i PATH=/usr/bin:/bin HOME=/tmp .venv/bin/python -m pytest -q -p no:cacheprovider
                                               # isolated: guards refuse the live DB/.env/credentials
systemctl restart crypto-bot
```

## 7. Rollback

```bash
systemctl stop crypto-bot
cd /opt/crypto-trading-machine && git checkout 7819d3b
systemctl start crypto-bot
```

- The schema additions are backward compatible (tested).
- Live risk state is mirrored to legacy keys, so trailing and TP state carries over.
- `fill_reconciliation`, `tick_runs` and `order_intents` stay in place unused.
- Restore the DB backup only for proven corruption (§5).

## 8. Post-deploy checks (read-only)

1. `systemctl show crypto-bot -p ActiveEnterTimestamp -p NRestarts` shows one fresh start.
2. `git rev-parse HEAD` equals the release commit, and `git status --porcelain` is empty.
3. Within 3 minutes, `tick_runs` has one row per minute with status `EXECUTED` or `BLOCKED` and a `completed_at`.
4. kv `autopilot_last_completed_tick` advances every minute. `health_status.trade_loop_alive` is true. There are no `emergency_halt` / `tick_protection` events while BUYs are gate-blocked.
5. kv `risk_loop_last_run` advances. SELL management is independent of BUY gates.
6. New `trade_audit` rows show `NOT_SUBMITTED` for gate rejections, and `startup` logs no pending intents.
7. Reconciliation:
   - `.venv/bin/python -m scripts.reconcile_fills --report` is read-only.
   - Then, after approval, run `.venv/bin/python -m scripts.reconcile_fills` (read-only exchange queries, no halt).
   - Expect `FILLED_VERIFIED` for the recent live orders. Investigate any discrepancy before enabling `--halt`.
8. Monitors: `.venv/bin/python -m scripts.trade_dashboard --mode live` and `scripts.monitor_tick_events` show stages, with test rows hidden.
9. `logs/uvicorn.log` has no new `Traceback`, `database is locked`, or `readonly database`.

**Expected behaviour.** APScheduler fires `autopilot_tick` on `CronTrigger(minute="*")` with `max_instances=1` and `coalesce=True` (`app/scheduler/scheduler.py`). Each tick holds the tick lock, evaluates every eligible symbol, writes `tick_runs`, and refreshes `autopilot_last_completed_tick`.

The watchdog (systemd timer every 5 minutes, plus the in-app health loop) halts **only** when the last completed tick is older than the stale threshold. In the current BEAR regime the expected steady state is all ticks `BLOCKED` or `EXECUTED` with no orders, and that is correct.

## 9. Database growth and retention (no action taken in production)

Measured read-only:

- **Size:** 9.23 GB, freelist 0, WAL.
- **`tick_audit`:** 6.50M rows, about 53–59k rows/day. That is one `HOLD` row per symbol per tick, about 2.2 KB each, of which `indicators` JSON is about 1.9 KB. Growth is about 130 MB/day, roughly 71% of the table is older than 30 days, and the oldest row is from 2026-07-26.
- **Other tables:**
  - `trade_audit`: 504,888 rows, quiet.
  - `ml_signal_events`: 650,689 rows, idle since 2026-11-09.
- **Indexes:** `(ts)` and `(symbol, ts)` serve retention well. None are missing for current queries.

**Tool:** `scripts/archive_tick_audit.py`, dry-run by default.

- **Phase 1:** copy an id-range batch to a separate archive DB, then commit.
- **Phase 2:** delete only rows whose archived copy is **column-for-column identical**, then commit. A crash can only leave harmless duplicates.
- It never runs VACUUM and enforces a minimum of 7 days of retention.

**Drill on the production extract:**
- 15,000 rows moved in 8 batches, with byte-identical content in the archive.
- A rerun moved nothing (idempotent).
- Restoring from the archive reproduced the original table exactly.
- `integrity_check` was ok on both files.
- 845 pages went to the freelist for reuse.

**Recommended plan** (each step needs owner approval):

1. Take the §5 backup first. Then, in a quiet window:
   ```bash
   python -m scripts.archive_tick_audit --db data/cache/trading.db --older-than-days 30
   ```
   This dry run is safe at any time.
2. Execute with `--archive /var/backups/crypto-bot/tick_audit_archive.db --execute`. The defaults are a 5k batch and a 0.2 s pause, about 4.6M rows. File size stays near 9.2 GB, but growth stops for about 35 days as freed pages are reused.
3. Schedule step 2 daily, which keeps the table at about 30 days (≈4 GB).
4. A later code change (needs approval): persist full `indicators` only for actionable or high-score rows. This gives an expected reduction of over 80%.
5. Optional: `VACUUM INTO` a new file in a maintenance window to return disk space. It needs about 5 GB free.

## 10. Strategy findings (simulation and read-only analysis; no settings changed)

**Live record** (`closed_trades`, mode=live, 643 rows, 2026-06-13 → 2026-10-30):

- **Overall:** −$159.52, win rate 42.3%, average win +$0.70, average loss −$0.94, PF 0.54, maximum drawdown −$172.93.
- **Expectancy:** −$0.25 per trade, bootstrap 95% CI [−$0.40, −$0.12]. The loss is statistically clear.
- **Profit came from:**
  - `trailing_stop`: +$85.73 over 113 trades.
  - `take_profit_1` / `take_profit_2`: +$71.10.
- **Losses came from:**
  - `stop_loss`: −$173.42 over 97 trades, median −4.2%.
  - `stale_dead_money`: −$56.10.
  - `signal` exits: −$31.64, 8.8% win rate.
  - `exchange_stop`: −$48.50.
- **Accounting defect in `exchange_stop`:** two of the four rows (#646 VTHO, #709 DASH) were booked at `exit_price=0.0` (−100%) by pre-`f45b839` code. That fix reached production only on 2026-11-07. The rows overstate losses by about $43; they were not altered.
- **By month:** only September was positive (PF 2.18). October lost −$165.70 (PF 0.25).

**ML gate:** `ml_signal_events` BUY with confidence ≥ 0.8 is 46.8% directional wins with a +0.03% average outcome, below a 0.4% round-trip cost. The gate has **no demonstrated edge.**

**Backtest** (`scripts/backtest_live_gate_stack.py`):
- 25 symbols, daily closed bars from 2025-05-22 to 2026-11-12, out-of-sample (OOS) from 2026-06-04.
- 0.2% cost per side.
- Not modelled: the ML gate, spread and orderbook gates.

| Variant | All trades / PF / exp [95% CI] |
|---|---|
| LIVE config | 31 / 0.17 / −4.33% [−6.07, −2.41] |
| threshold 70 | 47 / 0.18 / −3.99% |
| no breadth | 56 / 0.46 / −2.31% |
| no pullback confirmation | 396 / 0.74 / −0.81% [−1.46, −0.17] |
| SIM trail arms at +3% | 31 / 0.22 / −3.07% |
| SIM ATR stop 3× | 31 / 0.18 / −5.20% |
| raw setups (no gates) by regime | bear 39 / PF 2.09; **neutral 31 / PF 3.11, exp +3.37% [+1.06, +5.57]**; **bull 76 / PF 0.41** |

- **80 is reachable:** 216 of 13,244 symbol-days scored ≥ 80, and the maximum is 100.
- **Strong-bull gate concern:** this gate restricts entries to the regime where this mean-reversion setup does *worst*. Only one bucket (neutral, n=31) has a confidence interval excluding zero. Given the number of variants tested, it is a hypothesis for a forward paper test, not a change to deploy.

## 11. Test evidence

- Full suite: **468 passed**, run from a fresh clone of the release commit with no `.env`, no caches, and no credentials. Command: `env -i PATH=/usr/bin:/bin HOME=<empty dir> .venv/bin/python -m pytest -q -p no:cacheprovider` → `468 passed`.
- The first clean run (clone under `/tmp`) gave 467 passed / 1 failed: the pytest DB guard accepted the repo's own `data/cache` because the clone was inside `tempfile.gettempdir()`. Fixed in `Storage.__init__` (`app/storage/db.py`): under pytest, a DB inside the repository is refused even when the repo lives under the temp dir. Re-run: 468 passed (clean clone) and 468 passed (dev tree).
- Proving tests:
  - `tests/test_tick_liveness.py`: blocked tick ≠ halt, stalled tick → halt, failed or skipped tick does not refresh liveness, the scheduler fires, the risk loop runs during a BUY halt.
  - `tests/test_fill_reconcile.py` (15 tests):
    - Verified fills with duplicate trades.
    - API success without trades → `FILLS_PENDING`.
    - Network failures.
    - Partial then expired.
    - An open partial missing from the book → halt.
    - Absence within grace and beyond retention.
    - Idempotency.
    - Paper mode skipped.
    - Booking of an expired partial.
  - `tests/test_audit_labels.py`: stage classification, read-only monitors, test-row exclusion, the kill-switch `REJECTED` label, legacy risk-key mirror.
  - `tests/test_live_test_separation.py`: Storage, credential and `.env` guards; the NULL `agents` regression.
  - `tests/test_archive_tick_audit.py`: batch move, idempotency, no delete when the archive copy is missing or differs.

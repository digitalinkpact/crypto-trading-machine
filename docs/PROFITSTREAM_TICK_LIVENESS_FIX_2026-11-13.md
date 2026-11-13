# ProfitStream — Tick Liveness, Audit Labelling & Test Isolation (2026-11-13)

Status: **prepared in development, NOT deployed.** Production runs `7819d3b`.
Deploying this brings `4569263` (already on `main`, never deployed) plus the
commit that adds this file.

## Root causes

| # | Defect | Where | Effect |
|---|---|---|---|
| 1 | Watchdog measured liveness from `autopilot_last_success`, which `tick()` writes only for status `EXECUTED` (introduced in `4569263`). A tick whose qualifying BUY is rejected by a protection gate returns `BLOCKED`. | `app/trading/watchdog.py::_health_loop`, `app/trading/autopilot.py::Autopilot.tick` | After 180 s of persistent blocked signals: false stale tick → emergency halt + tick protection → all BUYs blocked while the protection-rejected signal persists. Not live in production (`7819d3b` still uses `state.last_tick_at`). |
| 2 | Every internal gate rejection was written as `binance_response="REJECTED"`; every confirmed fill as `"SUCCESS"`. | `app/trading/autopilot.py::Autopilot._execute._finish` | Internal strategy rejections looked like Binance rejections in `trade_audit` and the dashboard ("Last exchange response"). |
| 3 | Test runs were not isolated from LIVE. Production has no `tests/conftest.py`, and `/opt/crypto-trading-machine/.env` (root, 0600) holds a real API key with live flags. `pytest` run as root on the server loads LIVE settings and the LIVE database. | `app/config.py`, `app/storage/db.py::Storage.__init__`, `app/exchange/client.py::BinanceUSClient.__init__` | 75 `trade_audit` rows tagged `agents=["test"]` across **20 pytest runs** from 2026-08-21 to 2026-11-11, including 40 fake `executed_sell` / `SUCCESS` rows. No order in `orders` matches those runs (the only order in those minutes is a genuine `risk:trailing_stop` ADAUSDT SELL). |

## Fixes

- `Autopilot.tick` writes `autopilot_last_completed_tick` (`LAST_COMPLETED_TICK_KEY`) only when the strategy evaluation completed (`executed` or `blocked`) **and** the tick recorded no errors. Skipped ticks (lock held, autopilot stopped) and failed ticks do not refresh it. `_health_loop` reads this key. The dashboard JSON (`_execution_overview`) exposes `last_completed_tick_at`.
- `_finish` records `NOT_SUBMITTED` for internal rejections and `FILLED` only for confirmed fills. Existing rows are left unchanged.
- `running_under_pytest()` in `app/config.py` drives three guards that do not depend on `conftest.py`:
  - Settings never read the repository `.env` under pytest unless `APP_ENV_FILE` is set explicitly.
  - `BinanceUSClient` never holds signing credentials under pytest, so it cannot authenticate an order.
  - `Storage` refuses to open any database outside the system temp dir under pytest.
- `Storage.recent_trade_audit` hides `agents=["test"]` rows by default (`include_test=True` to show them). This is a read-side filter only; no data is changed.

Unchanged: 80 threshold, BTC regime / strong-bull / breadth gates, sizing, risk limits, SL/TP/trailing, credentials, positions.

## Tests

- `tests/test_tick_liveness.py`:
  - A blocked tick does not trigger a halt.
  - A genuinely stalled tick engages protection.
  - A failed tick does not refresh liveness.
  - A lock-skipped tick does not refresh liveness.
  - The real scheduler fires the tick.
  - The risk loop manages positions while BUYs are halted.
- `tests/test_live_test_separation.py`:
  - Storage guard.
  - Credential strip.
  - No repository `.env` under pytest.
  - Test rows are hidden from LIVE diagnostics.
- `tests/test_autopilot_positions.py::test_strong_bull_regime_mode_blocks_weak_bull`: an internal rejection is recorded as `NOT_SUBMITTED` with no execution attempt.
- Full suite: `434 passed`.

## Backup (before deploy)

```bash
ssh root@64.23.171.8
cd /opt/crypto-trading-machine
git rev-parse HEAD > /root/predeploy-rev.txt               # expect 7819d3b…
cp -p /var/lib/crypto-bot/.env /root/predeploy-env.bak      # chmod 600 is preserved
# Online, consistent copy of the WAL database (~9.2 GB; 73 GB free on /):
sqlite3 data/cache/trading.db ".backup '/root/trading-predeploy.db'"
sqlite3 /root/trading-predeploy.db "PRAGMA integrity_check;"   # expect ok
```

## Deploy (requires owner approval)

```bash
cd /opt/crypto-trading-machine
git fetch origin && git status --porcelain     # must be empty
git checkout <approved-commit>
.venv/bin/pip install -r requirements.txt      # no new pins expected
.venv/bin/python -m pytest -q -p no:cacheprovider   # runs isolated; must not touch data/cache
systemctl restart crypto-bot
```

The first start runs the `4569263` migration: it creates `tick_runs` and `order_intents`, adds columns with `ALTER TABLE … ADD COLUMN` (metadata-only), and sets `PRAGMA user_version=1`. It does not rebuild `tick_audit`.

## Rollback

```bash
systemctl stop crypto-bot
cd /opt/crypto-trading-machine && git checkout 7819d3b
systemctl start crypto-bot
```

`7819d3b` ignores the new tables and columns, so the schema additions are backward compatible. Restore `/root/trading-predeploy.db` only if data corruption is proven, and only with the service stopped.

## Post-deploy checklist (read-only)

1. `systemctl show crypto-bot -p ActiveEnterTimestamp -p NRestarts` shows a single fresh start.
2. `git rev-parse HEAD` equals the approved commit, and `git status --porcelain` is empty.
3. Within 3 minutes, `tick_runs` shows one row per minute with `status` in (`EXECUTED`, `BLOCKED`) and a `completed_at`.
4. kv `autopilot_last_completed_tick` advances every minute. kv `health_status.trade_loop_alive` is `true`.
5. No new `emergency_halt` or `tick_protection` while signals are protection-blocked.
6. kv `risk_loop_last_run` advances (SELL management is independent of BUY gates).
7. New `trade_audit` rows show `NOT_SUBMITTED` for gate rejections.
8. `logs/uvicorn.log` has no new `Traceback`, `database is locked`, or `readonly database` entries.

## Database growth and retention plan (no action taken)

Measured read-only on 2026-11-13 ~02:41 UTC:

- **Size:** `trading.db` is 9.23 GB (2,253,176 pages × 4 KiB, freelist 0, WAL mode).
- **`tick_audit` drives growth:**
  - 6,502,891 rows; 59,183 in the last 24 h (≈41/min = one row per evaluated symbol per tick).
  - ≈2.2 KB/row (indicators + filters JSON) → ≈130 MB/day ≈ 4 GB/month.
  - 4,642,183 rows (≈71 %) are older than 30 days.
  - The oldest row is from 2026-07-26.
- **Other tables:**
  - `trade_audit`: 504,888 rows, 0 in the last 24 h.
  - `ml_signal_events`: 650,689 rows, idle since 2026-11-09.
  - All other tables are negligible.
- **Indexes:** `(ts)` and `(symbol, ts)` on both audit tables. This is adequate for time-window reads and retention.

Recommended plan:

1. Archive, then prune `tick_audit` older than 30 days. This needs owner approval and must happen in a maintenance window, after the backup above:
   - Copy the rows into a separate archive DB with `ATTACH` + `INSERT … SELECT`.
   - Then `DELETE` in `ts`-bounded batches of ~50k rows.
   - Pages are reused afterwards without needing `VACUUM`.
2. Code-side (future change, needs approval): persist full indicator JSON only for symbols that pass a predicate or score ≥ 50, and store a compact summary for the rest. Expected reduction is >80 %.
3. Optionally run `VACUUM INTO` a new file during a later window to return space to the OS. This needs ~9 GB free; 73 GB is available.

## Strategy findings (simulation only — no settings changed)

`scripts/backtest_live_gate_stack.py`:

- **Setup:**
  - 25 static symbols, closed daily bars from 2025-05-22 to 2026-11-12, out-of-sample (OOS) from 2026-06-04.
  - 0.10 % fee + 0.10 % slippage per side.
  - Daily-bar risk ladder with stop-first.
- **Not modelled:** the ML gate, spread and other auxiliary gates.

| Variant | IS trades / PF / exp | OOS trades / PF / exp |
|---|---|---|
| LIVE config | 23 / 0.05 / −5.79 % | 8 / 0.95 / −0.14 % |
| no strong-bull | 27 / 0.13 / −4.91 % | 10 / 1.43 / +0.96 % |
| no breadth | 39 / 0.38 / −2.84 % | 17 / 0.70 / −1.09 % |
| threshold 70 | 38 / 0.10 / −4.90 % | 9 / 0.93 / −0.18 % |
| no pullback confirmation | 280 / 0.65 / −1.19 % | 116 / 1.04 / +0.09 % |

- **80 is mathematically reachable:**
  - dip (70) + BTC alignment (20) + filters (10) = 100.
  - Pullback is capped at 75–90 by its candidate score.
  - 156 setups scored ≥ 80 in 540 days.
- **80 is unreachable while BTC regime < 1 without a golden cross:** `btc_alignment_hard_gate` zeroes the filters. In addition, market/strong-bull gates block regime < 2 by design. Regime ≥ 2 held on 159 of 540 days.
- **No variant shows a robust positive edge in both IS and OOS.** Loosening the threshold or the gates is not supported by evidence.

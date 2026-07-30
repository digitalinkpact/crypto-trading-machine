# ProfitStream Production Reliability Report (2026-07-30)

## Scope
This report covers production hardening for live trading reliability and capital preservation, with verification from:
- Code paths in trading/scheduler/exchange/storage/watchdog.
- Runtime database evidence from `data/cache/trading.db`.
- Runtime logs (`logs/watchdog.log`, scheduler/autopilot logs).
- Focused automated tests for failure paths.

## Executive Summary
- Reliability was previously vulnerable to silent failure around stale/missing ticks, unknown live-order outcomes after network exceptions, and missing heartbeat persistence.
- Multi-hour and multi-day tick gaps were confirmed in `tick_audit` history.
- New protections now block fresh entries under stale tick/health faults, preserve independent risk exits, and persist component heartbeats for operator visibility.
- Live performance evidence still shows negative expectancy (6 wins / 35 losses in live closed trades); strategy improvements should proceed only after reliability burn-in.

## 1) Complete Failure-Point Analysis

### A. Process and Scheduler Reliability
- Failure Point: Scheduler/tick loop can stall while process remains alive.
- Evidence: `tick_audit` has 6 gaps >1h, including ~68.97h and ~49.32h gaps.
- Impact: No strategy ticks; prior architecture risked unmanaged positions if exits depended on tick.
- Mitigation Implemented:
  - Tick staleness threshold made explicit via config (`health_tick_stale_seconds`).
  - Watchdog now engages `tick_protection` and emergency halt when tick becomes stale.
  - Scheduler hardened with `coalesce`, `max_instances=1`, `misfire_grace_time`.
  - Scheduled tick wrapped in timeout to prevent hung executions from stalling cadence.

### B. Stop-Loss / Trailing / Take-Profit Continuity
- Failure Point: Exits historically tied to tick cadence.
- Current State: Independent risk loop exists and remains decoupled from strategy tick.
- Mitigation Implemented:
  - Watchdog stale-tick procedure triggers emergency risk-gate run and reconciliation.
  - Risk-loop heartbeat monitored and escalated when stale.
- Residual Risk:
  - If both main app and independent risk process are down simultaneously, exchange-side native stop orders still do not exist.

### C. Live Order Placement / Unknown Outcomes
- Failure Point: Network exception during order placement could leave outcome ambiguous.
- Impact: Potential duplicate re-submit or phantom state if guessed incorrectly.
- Mitigation Implemented:
  - Added idempotent recovery protocol:
    - Generate client order ID before placement.
    - On placement exception, query exchange by client order ID.
    - `found`: reconstruct order from exchange payload.
    - `confirmed_absent`: treat as not placed.
    - `inconclusive`: persist incident and trigger emergency halt (`order_outcome_unknown`).

### D. Entry Blocking During Faults
- Failure Point: Health/watchdog halt flags existed but were not consistently enforced in entry execution.
- Mitigation Implemented:
  - Autopilot checks `emergency_halt` and `tick_protection` and blocks BUY entries.
  - SELL/risk exits remain available to reduce risk.

### E. Database and State Observability
- Failure Point: No durable component heartbeat store for supervisor-level diagnosis across restarts.
- Mitigation Implemented:
  - Added `component_heartbeats` table with upsert heartbeat records for:
    - scheduler
    - websocket
    - watchdog
    - risk_manager
    - tick_execution
    - api_connectivity

### F. Watchdog Coverage
- Failure Point: Cron/timer lapses can leave process unsupervised.
- Evidence: Large time gap in `logs/watchdog.log` between 2026-07-28 and 2026-07-30.
- Mitigation Implemented:
  - Systemd timer set `Persistent=true`.
  - Unit dependencies moved to `network-online.target` and restart semantics hardened.

### G. Strategy Performance Risk
- Evidence from DB (`closed_trades`):
  - Live: 41 trades, 6 wins, 35 losses, total PnL -39.2885.
  - Largest single observed loss: PROMUSDT about -40.3% (historical unmanaged/stale period risk profile).
- Dominant recent losers concentrated in thin/volatile names and trend-following confluence combinations.

## 2) Code Changes Implemented

### Core reliability and risk changes
- `app/config.py`
  - Added runtime reliability settings used by watchdog/risk loops:
    - `risk_loop_in_process_enabled`
    - `risk_manager_loop_seconds`
    - `autopilot_tick_timeout_seconds`
    - `health_tick_stale_seconds`
    - `health_latency_warn_seconds`
    - `health_duplicate_order_window_seconds`
    - `health_order_failure_lookback_minutes`
    - `health_order_failure_max`
    - `health_memory_rss_warn_mb`
    - `health_cpu_warn_pct`
    - `emergency_halt_max_failures`
    - `emergency_halt_auto_clear_cycles`

- `app/exchange/client.py`
  - Added:
    - `generate_client_order_id(...)`
    - `order_from_raw(...)`
    - `get_order_by_client_id(...)` returning `found | confirmed_absent | inconclusive`

- `app/trading/autopilot.py`
  - Added `_entry_block_reason()` and BUY-entry blocking on:
    - `emergency_halt.active`
    - `tick_protection.active`
  - Hardened `_submit(...)` with order-failure protocol and exchange outcome verification.
  - Added `_resolve_order_after_exception(...)` including emergency halt escalation for unknown outcomes.

- `app/trading/watchdog.py`
  - Added tick protection state lifecycle:
    - `engage_tick_protection(...)`
    - `_maybe_clear_tick_protection()`
  - Added `_record_component_heartbeats(...)` persistence.
  - Stale tick now evaluated with config threshold and triggers:
    - emergency halt
    - tick protection
    - emergency risk-gate run
    - reconciliation attempt
  - Auto-clear now verifies safe resume before clearing halt/protection.

- `app/storage/db.py`
  - Added `component_heartbeats` table.
  - Added storage APIs:
    - `record_component_heartbeat(...)`
    - `get_component_heartbeats()`

- `app/scheduler/jobs.py`
  - Wrapped `autopilot.tick()` with timeout and critical logging on timeout.

- `app/scheduler/scheduler.py`
  - Added scheduler `job_defaults` hardening (`coalesce`, `max_instances`, `misfire_grace_time`).

### Infrastructure files
- `deploy/systemd/crypto-bot.service`
- `deploy/systemd/crypto-bot-risk.service`
- `deploy/systemd/crypto-bot-watchdog.service`
- `deploy/systemd/crypto-bot-watchdog.timer`

Hardened with network-online dependencies, env loading, restart timing, stop semantics, and persistent timer behavior.

## 3) Database Migration Changes
- Added table:
  - `component_heartbeats(component PRIMARY KEY, ts, healthy, detail)`
- Added index:
  - `ix_component_heartbeats_ts`
- Migration behavior:
  - Automatic via existing schema bootstrap (`CREATE TABLE IF NOT EXISTS`), no manual migration script required.

## 4) Infrastructure Recommendations
1. Run three supervised units in production:
   - main API/scheduler unit
   - independent risk-loop unit
   - watchdog timer/service
2. Keep `risk_loop_in_process_enabled=false` when standalone risk service is enabled.
3. Use systemd journal forwarding and log retention alerts (disk-fill prevention).
4. Add node-level monitoring (CPU, memory, process restarts, disk) and off-host alerting.
5. Add uptime probes for:
   - `/healthz`
   - staleness of `autopilot_state.last_tick_at`
   - staleness of `component_heartbeats` rows.
6. Prefer dedicated non-root service user for least privilege.

## 5) Strategy Recommendations (Post-Reliability)
Based on current local DB evidence (live 6/35 W/L, negative PnL):
1. Reduce low-liquidity tail risk:
   - tighten spread/depth requirements for small-cap symbols
   - enforce stricter universe quality floors before entry scoring
2. Require stronger multi-timeframe alignment before BUY entries.
3. De-emphasize noisy momentum-only confluence in risk-off regimes.
4. Introduce explicit no-trade regime windows when BTC regime is weak/choppy.
5. Limit concurrent correlated alt positions during market stress.
6. Prioritize trade quality over count (higher threshold, lower max concurrent entries).

## 6) 24-Hour Reliability Test Plan
1. Deploy with systemd units enabled (main + risk + watchdog timer).
2. Start synthetic fault drills (one at a time):
   - block Binance API egress temporarily
   - force websocket disconnect
   - pause scheduler thread/job execution
   - simulate DB lock contention
3. Verify for each drill:
   - BUY entries blocked quickly (emergency halt/tick protection)
   - risk loop continues running and heartbeats update
   - watchdog records actions and recovery attempts
   - reconcile job results recorded
4. Confirm no duplicate orders/positions and no unbounded retries.
5. Review `component_heartbeats`, `health_status`, `trade_audit`, `tick_audit` after each drill.

## 7) 7-Day Reliability Burn-In Plan
1. Run continuously for 7 days with live-trading safeguards active.
2. Daily checks:
   - zero stale heartbeat incidents unresolved > 10 minutes
   - zero unknown order outcomes left uninvestigated
   - no multi-hour tick gaps
   - no persistent order failure streaks
3. Mid-week controlled restart/reboot tests.
4. End-of-week postmortem:
   - compare heartbeat continuity and restart behavior
   - verify reconciliation and risk exits under stress windows.

## 8) Verification of Critical Trade Paths Under Failure
Verified with focused tests and code-path checks:
- BUY execution gating under emergency protection:
  - enforced via autopilot entry-block checks.
- SELL execution path remains available during BUY block.
- Stop-loss/trailing/take-profit path:
  - independent risk loop + watchdog emergency risk-gate execution.
- Order failure protocol:
  - exception -> exchange lookup by client order id -> deterministic handling.
- Duplicate protection:
  - cross-process lock and mode-scoped positions verified by tests.

Focused test run after changes:
- `tests/test_order_failure_protocol.py`
- `tests/test_watchdog.py`
- `tests/test_health_monitor.py`
- `tests/test_exchange.py`
- `tests/test_storage_safety.py`
- Result: 61 passed.

## 9) Remaining Capital-Loss Risks (Must Be Explicit)
1. No exchange-native stop orders are currently maintained; protection still depends on bot/risk process uptime.
2. If host/network outage isolates the bot from Binance for extended periods, exits cannot execute.
3. Strategy edge remains negative in recent live sample; reliability fixes do not solve expectancy alone.
4. Existing repo has unrelated ongoing changes; full-suite behavior should be revalidated after branch stabilization.

## 10) Operational Go/No-Go Criteria
- Go live only if all are true for the full burn-in window:
  - no unresolved stale tick protection events
  - no unresolved unknown order outcome incidents
  - continuous risk loop heartbeat
  - no duplicate order/position events
  - successful supervised restart/reboot recovery drills
- If any fail, remain in paper mode and remediate first.

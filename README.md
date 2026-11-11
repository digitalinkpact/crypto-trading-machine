# crypto-trading-machine
AI Crypto Trading Machine — Binance.US | 7 Agents | 25 Coins | 4 Timeframes

## Autonomous trading diagnostics

When `PROFITSTREAM_ENABLED=true` and `PROFITSTREAM_USE_LEGACY_AGENTS=false`,
ProfitStream's daily strategy is the only entry producer; the legacy seven-agent
ensemble is not used as a fallback. The scheduler checks execution every minute,
but technical setups use the last fully closed UTC daily candle.

Strategy, BTC regime, breadth, local trend, ATR and ML features share the
[daily snapshot loader](app/data/daily.py): up to 499 closed daily bars, identical
indicator warm-up, no forming candle, and fail-closed freshness/gap checks.
The liquid pairlist requires sufficient closed history for EMA200 plus 60 usable
rows (259 closed bars); it does not lower the indicator requirements for new pairs.
Breadth uses only active non-BTC pairs and blocks entries on incomplete coverage,
rather than counting stale files from symbols no longer in the universe.

The dashboard's **Daily strategy scan** and `/autopilot/status` `strategy_scan`
report the actual candle timestamp, predicate pass counts, maximum score and BTC
market eligibility even when there are no BUY signals. A 30-point HOLD means no
complete dip or pullback setup: it is not a request to lower the 80-point threshold.
Existing risk percentages, sizing, stop/TP rules and regime policies still apply.

New `tick_audit` rows use timeframe `1d` and `filters.audit_version=2`.
`runner_pass` means a signal cleared the strategy score bar, not that it traded.
`executed` becomes true only after a fill, with submitted/filled status and the
client order ID linked back to the decision. Historical version-1 rows retain
their original meaning; do not treat their `executed` flag as exchange fills.

## Manual Tick

`POST /autopilot/tick` runs one ordinary tick in the existing application
process. It does not start a stopped autopilot, force a BUY, schedule a timed
sale, or run the independent protective-exit loop. All strategy, sizing, halt,
order idempotency, and in-process/cross-process tick locks remain in force.
It can place real orders when the service is configured for live trading.

The dashboard's **RUN ONE TICK** button requires a valid login session, even
on an allowlisted IP. Live operation requires typing `RUN_ONE_LIVE_TICK`.
The API requires the `X-Tick-Confirmation` header; simulation retains the
compatibility value `RUN_ONE_PAPER_TICK`. Cross-origin requests are rejected.

After installing this version yourself, log in to the dashboard. To invoke
one live tick manually, run this JavaScript in that dashboard tab's browser
developer console, not Bash or Copilot Chat:

```javascript
if (window.prompt("Type RUN_ONE_LIVE_TICK to allow one live trading tick") === "RUN_ONE_LIVE_TICK") {
	fetch("/autopilot/tick", {
		method: "POST",
		credentials: "same-origin",
		headers: {
			"Accept": "application/json",
			"X-Tick-Confirmation": "RUN_ONE_LIVE_TICK"
		}
	}).then(async response => console.log(response.status, await response.json()));
}
```

The JSON `status` is `EXECUTED` (including all-HOLD scans), `SKIPPED` (stopped
or locked), `BLOCKED` (applicable entry protection), or `FAILED` (technical
error). A durable `tick_id` links start/completion time, duration, decisions,
actual order responses and sanitized errors. Orders submitted and orders
filled are separate counts, never inferred from skip counters. A filled order
does not imply profit. A tick may act on any eligible symbol, not just BTC.
Scheduled, initial, delayed-retry and manual ticks use the same timeout and
locks. A delayed book recheck requests a new full tick, never an old-signal
submission. Do not retry blindly after a timeout: inspect persisted ticks and
order intents first. No command in this documentation is executed for you.

## Execution Modes

`TRADING_MODE=live|simulation` is authoritative. Valid existing legacy flags
are normalized for compatibility; conflicts fail startup rather than silently
turning simulation off. The BUY kill switch remains a separate safety control.
Production stays explicitly live; an unspecified new installation remains safe.
Mode changes require offline review and restart, not a dashboard hot toggle or
the Stop & Liquidate endpoint.

Live data remains at `DATA_CACHE_DIR/trading.db`; simulation uses
`DATA_CACHE_DIR/simulation/trading.db`. Old mixed-ledger history is preserved,
not automatically copied into a new simulation account. Simulation receives
credential-free read-only market data and cannot mutate Binance.US orders.

Inspect configuration without importing storage or starting any loop:

```bash
APP_ENV_FILE=/var/lib/crypto-bot/.env .venv/bin/python -m scripts.inspect_trading_mode
```

Run the local tests with `.venv/bin/python -m pytest -q`. They enforce temporary
per-test storage and prohibit network connections. Do not run runtime imports,
tests or schema migrations against the production database during an audit.
See the [development repair and rollout notes](docs/PROFITSTREAM_EXECUTION_REPAIR_2026-11-11.md).

## Deployment

- DigitalOcean runbook: docs/DEPLOY_DIGITALOCEAN.md
- Bootstrap script: scripts/bootstrap_droplet.sh
- Systemd unit template: deploy/systemd/crypto-bot.service
- Security hardening guide: docs/SECURITY_HARDENING.md
- Host hardening script: scripts/harden_server.sh

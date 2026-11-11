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

## Deployment

- DigitalOcean runbook: docs/DEPLOY_DIGITALOCEAN.md
- Bootstrap script: scripts/bootstrap_droplet.sh
- Systemd unit template: deploy/systemd/crypto-bot.service
- Security hardening guide: docs/SECURITY_HARDENING.md
- Host hardening script: scripts/harden_server.sh

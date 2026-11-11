# ProfitStream Execution Repairs

Development changes approved 2026-11-11. Production deployment, service restarts,
mode/risk changes and live execution remain **not authorized**. No production
commands were executed during this repair turn.

## Implemented

- Explicit transactions protect position closure/reduction and schema migration.
  Grouped storage transactions make each paper fill atomic. Confirmed live fill
  bookkeeping is atomic and keyed by client ID to prevent duplicate recording.
- Active order locks reject same-owner re-entry; each submission has a unique
  lock owner. Unresolved same-side intents block another submission.
- Order intents persist before network I/O. Cancellation retains UNKNOWN and
  requests an operator-verification halt. Pending or unrecorded fills block new
  entries at startup. No blind automatic exchange resubmission was added.
- `TRADING_MODE` defines live/simulation. Legacy flags derive a valid compatible
  mode, but conflicts and dry-run/live-balance ambiguity fail validation.
  Hot mode changes are rejected without writing settings.
- Live ledger path remains unchanged; simulation gets its own database and
  credential-free read-only prices. Existing mixed history is not deleted or
  silently copied. Risk HWM/TP keys are scoped and old live values are preserved.
- Every admitted invocation has a durable tick ID, source/requester, revision,
  attempt/completion time, duration, decisions, orders and sanitized failures.
  Scheduled/initial/manual/retry calls share locks and bounded execution.
- Technical failures override a partially successful result; HOLD is normal
  completion. Order counts come from responses, not duplicated reason counters.
- Risk success heartbeat updates only after successful position evaluation.
  Lock/database failures are contained. Emergency evaluations use the same risk
  loop guard. The watchdog measures successful trade-loop completion.
- Delayed book retries request a fresh full tick. LLM calls stay off the order
  execution path. The alternate scheduler builder delegates to the canonical one.
- Dashboard/API share persisted execution data, mode-scoped order/portfolio
  summaries, next run, last success/error and explicit unknown historical status.
  RUN ONE TICK requires a session and explicit live confirmation, rejects foreign
  origins and suppresses duplicate clicks without automatic retry.
- Tests use temporary runtime configuration, a new runtime database per test,
  and network denial even when a caller catches the blocked connection error.

The 80-point threshold, risk percentages, sizing parameters, stop and take-profit
settings were not relaxed or changed. Test-only settings and synthetic fixtures
are not production changes.

## Verification

Automated coverage includes positive daily strategy to paper BUY, independent
stop-loss SELL, partial TP1 SELL with remainder, insufficient cash, transaction
rollback, repeated client IDs, pending/cancelled exchange outcomes, actual tick
completion, timeouts, concurrent scheduled/manual admission, restart recovery,
mode conflicts, auth/origin rejection and backend-derived dashboard rendering.

Playwright checked the mocked localhost dashboard at 1440x1000 and 390x844:
no document overflow or page errors, visible controls, returned result, disabled
in-flight button, duplicate-click suppression, authentication-error recovery,
live-confirmation cancellation and removal of the hot mode form. Screenshots
were visually inspected. The preview has no application lifespan/scheduler and
uses fake balances and a mocked tick callable, not a live executor.

Final verification: **424 tests passed in 25.94 seconds** with per-test
databases and enforced network denial. Checked Python files have no editor
errors; `bash -n scripts/watchdog.sh` and `git diff --check` passed. A green
mocked suite is not proof of future profitability or production acceptance.

## Changed Files

- Mode and persistence: [config](../app/config.py), [credentials](../app/credentials.py), [storage](../app/storage/db.py).
- Execution: [autopilot](../app/trading/autopilot.py), [context](../app/trading/execution_context.py), [paper](../app/trading/paper.py), [risk](../app/trading/risk.py), [risk loop](../app/trading/risk_loop.py), [startup](../app/trading/startup.py), [watchdog](../app/trading/watchdog.py).
- Integration: [runner](../app/agents/runner.py), [routes](../app/api/routes.py), [exchange client](../app/exchange/client.py), [filters](../app/exchange/filters.py), [main](../app/main.py), [scheduler callbacks](../app/scheduler/jobs.py).
- Operations: [risk unit template](../deploy/systemd/crypto-bot-risk.service), [external watchdog](../scripts/watchdog.sh), [read-only inspector](../scripts/inspect_trading_mode.py).
- Tests: [isolation fixtures](../tests/conftest.py), [manual controls](../tests/test_manual_tick.py), [positions](../tests/test_autopilot_positions.py), [configuration](../tests/test_config.py), [health](../tests/test_health_monitor.py), [order failure](../tests/test_order_failure_protocol.py), [paper pipeline](../tests/test_profitstream_pipeline.py), [stops](../tests/test_protective_stops.py), [storage](../tests/test_storage_safety.py).
- Documentation: [README](../README.md), [original audit](PROFITSTREAM_AUTOPILOT_EXECUTION_AUDIT_2026-11-11.md), and this repair/runbook document.

The audit and manual-control files already existed as uncommitted work when
this repair turn began. Their relevant content was preserved and extended;
no unrelated user changes were reverted. No commit, push or deployment occurred.

## Migration Contract

Do not edit the live env file as part of this development work. Under a later
approved rollout, the existing service flags `LIVE_MODE=true`,
`PAPER_TRADING=false`, `DRY_RUN=false` continue to resolve to live with unchanged
risk values. `TRADING_MODE=live` can be adopted in a separately reviewed config
change with matching legacy flags or after removing them. Contradictions fail
closed. Process environment outranks an env file and must be included in review.

Existing live database: additive tables/columns and schema version 1 initialize
on application storage startup; no old trades are rewritten. Legacy order status
and parent tick ID remain unknown rather than invented. New order intents and
tick-run records support recovery. A newer schema version refuses automatic
downgrade. Use a consistent backup and rehearse upgrade against a database copy.

Existing simulation history: the old mixed database remains intact. A new
simulation ledger is separate; its empty account is seeded explicitly by normal
simulation startup. Historical simulation import requires its own inspected,
approved copy operation; never repoint simulation at the live database.

Unknown, NEW or PARTIALLY_FILLED intents require exchange/client-ID review before
resuming affected entries. This release intentionally does not fabricate later
fills or automatically repair financial history. Review ML training provenance
before using old artifacts from a previously mixed ledger.

## Approval-Gated Rollout

Do not run the old bootstrap script as an audit or verification command: it
changes the checkout, installs packages and restarts the service. This change is
not committed or pushed automatically. Choose a reviewed release commit first.

1. Operator records the current checkout and loaded revision, snapshots settings
   through the inspector, and verifies current positions, pending intents and
   exchange-native protection. The earlier zero-position snapshot is not a
   future safety guarantee. Agree on supervision during maintenance.
2. Take a consistent SQLite online backup plus restricted config/unit backups.
   Rehearse restoring and upgrading that backup **in isolated storage with no
   credentials and network denied**. Do not raw-copy a WAL database file alone.
3. Under explicit production maintenance approval, stop new admission and resolve
   in-flight requests without invoking `/autopilot/stop` (which liquidates).
4. Operator installs the pinned commit and reviewed unit changes only. Do not
   enable an additional risk process. Preserve the existing env file, BUY switch,
   risk controls and credential permissions. Any restart requires deployment
   approval. Storage startup applies the tested additive version-1 migration.
5. Verify the loaded revision, effective mode, version, tick completion, next run,
   risk success and any outstanding intents using read-only observations. Let
   ordinary scheduled scans provide the evidence; do not force a live tick.

Read-only inspection commands, for the operator after choosing the release:

```bash
cd /opt/crypto-trading-machine
git status --short
git rev-parse HEAD
systemctl show crypto-bot -p User -p MainPID -p ActiveState -p ExecMainStartTimestamp
runuser -u cryptobot -- env APP_ENV_FILE=/var/lib/crypto-bot/.env \
  .venv/bin/python -m scripts.inspect_trading_mode
journalctl -u crypto-bot --since '15 minutes ago' --no-pager
```

The actual code-install command requires the reviewed release hash; it must not
use an unreviewed moving `main`. No hash is invented in this runbook. Do not run
pytest, app startup imports or a writable schema probe on production for parity.

## Rollback and Residual Risks

Preserve the old code and compatible database backup. Do not overwrite current
accounting with an old backup after exchange activity: reconcile actual fills
first. Stop new admission under the agreed maintenance procedure and preserve
order intents/client IDs for operator verification. No rollback may silently
reset mode, liquidate holdings or remove safety controls.

External exchange/network behavior, market gaps, optional data providers and
model quality remain operational risks. Native stops are a backstop, not a fill
guarantee. Software risk evaluation shares the API process/event loop unless a
separate supervised deployment is explicitly approved. TLS/ingress hardening,
historical data repair, archival and production-load rehearsal remain separate
operational work, not silently performed by these code repairs.
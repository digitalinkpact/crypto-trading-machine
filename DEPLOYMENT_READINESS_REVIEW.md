# ProfitStream Deployment Readiness Review

## Status

**Local repair candidate: GO FOR REVIEW.**
**No commit/revision has been explicitly authorized for deployment.**
**Exact reviewed commit: PENDING explicit commit authorization.**
**Production deployment: NO-GO until operator prerequisites are completed.**
**Live BUY activation: NO-GO.**

No production connection or modification was performed. No commit was created.

## Reviewed Changes

- Safe reconciliation with unresolved state and verified SELL evidence.
- Independent HTML closed-trade/order pagination cursors.
- Mode-aware history endpoints and bounded queries.
- Authoritative tick summary from `tick_runs`.
- Authoritative live/paper database path resolution.
- Fail-closed halt-state validation.
- Atomic, timeout-bounded SQLite backup utility.
- Obsolete shell `.backup` and duplicate raw-SQLite backup instructions retired from active deployment runbooks.

## Verification Evidence

- Focused remaining-risk suite: 114 passed.
- Backup/protection suite: 138 passed.
- Complete final suite: 505 passed in 31.56 seconds.
- Python compile: passed.
- Shell syntax: passed.
- No secret values or private keys found in the diff.
- No strategy threshold or risk-limit diff found.

## Database Migration

The application creates these objects with `IF NOT EXISTS` during normal initialization:

- `reconciliation_states`
- `ix_orders_mode_id`
- `ix_closed_trades_mode_id`
- `ix_tick_runs_mode_started`
- `ix_tick_runs_status`

No destructive migration is required. Startup must retain the existing database and existing
`closed_trades`, `orders`, `positions`, and `kv` rows.

## Backup and Recovery

Use the local utility only after operator approval:

```bash
python -m scripts.backup_trading_db \
  /path/to/trading.db /path/to/backup.db \
  --timeout 30 --max-seconds 3600
```

The utility:

- Opens SQLite read-only for source/verification.
- Builds a `.tmp` destination.
- Uses SQLite online backup support.
- Runs a quick integrity check.
- Compares key table counts.
- Writes SHA-256 only after verification.
- Atomically promotes the destination.
- Never overwrites a destination until all checks pass.

Do not run this utility against production without a separate approved maintenance window.

## Existing Production Backup Investigation Procedure

Do not stop or modify the existing process. From the DigitalOcean console:

1. Record PID, PPID, command line, start time, state, CPU, RSS, and open files.
2. Record source and destination paths plus source WAL/SHM descriptors.
3. Sample destination size and process I/O counters every 60 seconds.
4. Verify no competing backup process exists.
5. Search cron, systemd, shell history, and process parent provenance.
6. Check disk inode, space, and filesystem health.
7. If destination is not growing and I/O is absent, classify stalled and escalate.
8. If I/O is active but size growth is absent, inspect SQLite/WAL and lock state.
9. Verify a backup read-only only after it is no longer being written.
10. Terminate only under an explicit operator procedure if the process is conclusively abandoned,
    document the PID, keep the destination unpromoted, and preserve all partial data.

## Binance.US API Replacement Procedure

1. Create a new Binance.US spot trading key.
2. Enable spot trading and required signed-read operations.
3. Disable withdrawals and transfers.
4. Restrict the key to the droplet public IP.
5. Store only in `/var/lib/crypto-bot/.env` with mode `0600`.
6. Do not print or place credentials in logs, backups, database fields, or dashboards.
7. Deploy the key through the normal secret-management procedure with a controlled restart.
8. Immediately verify account and open-order reads; do not place a test order.
9. If spot or IP restrictions reject required calls, stop and diagnose before live entry enablement.

## Post-Deployment Read-only Verification

- Verify commit and clean tracked worktree.
- Verify systemd unit and working directory.
- Verify `crypto-bot` is active and has one uvicorn PID.
- Verify database path is live `data/cache/trading.db`.
- Run SQLite `quick_check(1)` read-only.
- Verify halt states are valid booleans.
- Verify risk loop heartbeat and scheduler.
- Verify latest tick and latest failed tick.
- Verify Binance account canTrade/canWithdraw from the new key.
- Verify open orders and open positions.
- Verify unresolved reconciliation states are visible.
- Verify protective stops are still monitored.
- Do not activate live BUYs after deployment.

## Position Reconciliation Verification

- Confirm no reconciliation run deletes a position without a verified fill.
- Confirm zero/missing balance records unresolved state.
- Confirm fill price and commission come from verified trade records.
- Confirm partial fills reduce position quantity and preserve remainder.
- Confirm no pre-entry SELL fill resolves a later position.
- Confirm live/paper reconciliation states remain separate.
- Confirm unresolved states block additional live exposure while protective risk loops continue.

## Rollback

- Keep the old application revision and compatible database backup available.
- Obtain explicit approval for the exact 40-character commit and the current live-BUY kill-switch state.
- Roll back application code only with an approved fast-forward-compatible procedure.
- Do not copy an older SQLite database over a live database that has newer fills.
- Reconcile exchange fills before choosing a database rollback.
- Preserve unresolved reconciliation states and historical records.

## Deployment Readiness Assessment

The repair candidate is GO for review and local testing.

Deployment remains NO-GO until:

- The existing production backup process is explicitly classified and resolved by its owner.
- A restricted Binance.US key replaces the withdrawal-enabled key.
- A backup and restore rehearsal is completed on an isolated copy. Local tests prove
  WAL backup, integrity, SHA-256, temporary-destination cleanup, and restore reopening;
  this is not production backup evidence.
- The reconciliation unresolved-state behavior is accepted by the operator.
- A separate controlled deployment approval is recorded.

Live BUY activation remains NO-GO after deployment until reviewed under live market conditions.

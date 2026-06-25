# Fixes for trade execution blocker

## Issue 1: SELL signals without positions
**Root:** SignalAggregator produces SELL action even without any open position.
**Fix:** Filter SELL signals in autopilot - only allow if position exists.

## Issue 2: Low confidence BUY signals
**Root:** Min threshold 0.72 set high; early signals below threshold.
**Fix:** Temporarily lower threshold or check signal composition.

## Issue 3: Paper account seeding
**Root:** ensure_seeded() only called on start(), not on restart.
**Fix:** Ensure paper_exchange has USDT balance at tick time.

## Implementation Plan:
1. Add position check before SELL execution
2. Add balance seeding verification at tick start
3. Log signal composition to understand confidence calculation

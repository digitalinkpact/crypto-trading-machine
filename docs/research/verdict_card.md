# Stage 5 — Weekly experiment verdict card

**Written before observing any real Stage 5 output, on purpose.** The rules
below are committed up front so the final verdict cannot be rationalized after
seeing the numbers. `app/research/experiment_verdict.py` evaluates the outputs
of Stages 1-4 against exactly these rules. **The engine never applies a change
— a human decides what to do with the recommendation.**

## Inputs

| Source | Type |
|---|---|
| Stage 1 rejected-setup summary | `list[dict]` from `app.research.rejected_setups.summarize_by_reason` (or `None`) |
| Stage 2 promotion verdict | `app.trading.experiment_stats.PromotionVerdict` (or `None`) |
| Stage 3 drift report | `app.research.drift.DriftReport` (or `None`) |
| Stage 4 spread costs | `app.research.execution_quality.SpreadCosts` (or `None`) |
| Stage 4 expected maker benefit | `float | None` from `expected_entry_benefit` |

Missing inputs are recorded in `inputs_present` and never crash the verdict.

## Decision logic (in priority order)

1. **PAUSE** — any of:
   * a material execution-drift metric is present in the drift report
     (`kind == "execution"` and `material == True`), OR
   * a material max-drawdown drift metric worse than backtest is present
     (`name == "max_drawdown_pct"` and `material == True`).
2. **REJECT** — Stage 2 verdict is `REJECT`.
3. **PROMOTE** — Stage 2 verdict is `PROMOTE` **AND** the drift report shows
   neither `strategy_drift` nor `execution_drift`.
4. **KEEP_TESTING** — anything else, including any case where required inputs
   are missing.

## Concerns

Every material metric or failing criterion is appended to `concerns` with a
short human phrase so a reviewer can see *why* the verdict is what it is
without opening each stage's report.

## Recommendations

Concrete, ordered actions. Examples:

* PAUSE from execution drift → *"Halve position size and switch entries to
  passive limits until the entry spread drops below the modeled slippage."*
* PAUSE from drawdown drift → *"Cut position size to 50% and require Stage 2
  to re-evaluate after 20 more trades."*
* REJECT → *"Stop the current live experiment. Return to paper trading with a
  parameter change or a new hypothesis."*
* PROMOTE → *"Consider graduating this variant (larger universe or higher
  sizing). Manual change only — review Stage 4 maker-vs-taker economics first
  in case a cheaper entry raises the ceiling further."*
* KEEP_TESTING → *"Collect more forward evidence. Do not change parameters
  mid-experiment."*

## What Stage 5 explicitly does NOT do

* It never writes to `orders`, `closed_trades`, `settings`, or any config
  table.
* It never toggles autopilot mode, position size, or the universe.
* It never emails, pages, or webhooks. That belongs to a future Stage 6.

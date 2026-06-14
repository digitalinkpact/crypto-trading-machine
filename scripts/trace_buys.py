"""Debug harness: run the REAL autopilot._execute gate logic over live signals
without placing any orders. Stubs _submit so nothing hits the exchange, then
prints the per-symbol skip reason breakdown.

    python -m scripts.trace_buys
"""
from __future__ import annotations

import asyncio

from app.agents import run_all_agents
from app.config import get_settings
from app.signals import SignalAction
from app.trading.autopilot import autopilot


async def main() -> None:
    s = get_settings()
    submitted: list[tuple] = []

    async def fake_submit(symbol, side, qty, agents):
        submitted.append((symbol, side.value, str(qty), agents))
        print(f"   >>> WOULD SUBMIT {side.value} {symbol} qty={qty} agents={agents}")
        return None

    # Neutralize side effects we don't want during a dry trace.
    autopilot._submit = fake_submit  # type: ignore[assignment]
    autopilot.state.mode = "paper" if s.paper_trading else "live"
    autopilot.state.running = True
    autopilot.state.cooldowns = {}  # ignore stale cooldowns for the trace
    if autopilot.state.starting_balance_usdt is None:
        autopilot.state.starting_balance_usdt = None

    print(f"mode={autopilot.state.mode} min_conf={s.min_signal_confidence} "
          f"ml_gate={s.ml_gate_enabled}@{s.ml_gate_threshold} "
          f"orderbook_gate={s.orderbook_gate_enabled} "
          f"max_open={s.max_open_positions} max_long_exp={s.max_long_exposure_pct}")

    sigs = await run_all_agents(use_llm=False)
    buys = {k: v for k, v in sigs.items() if v.action == SignalAction.BUY
            and v.confidence >= s.min_signal_confidence}
    print(f"\nQualifying BUY signals (conf>=min): {len(buys)}")
    for k, v in sorted(buys.items(), key=lambda kv: -kv[1].confidence):
        print(f"  {k:12s} conf={v.confidence:.3f}")

    print("\n--- running real _execute (orders stubbed) ---")
    await autopilot._execute(sigs, allow_buys=True)

    from app.storage import storage
    dbg = storage.kv_get("autopilot_last_tick_debug") or {}
    print("\n=== skip-reason breakdown ===")
    for k, v in sorted((dbg.get("by_reason") or {}).items(), key=lambda kv: -kv[1]):
        print(f"  {k:24s} {v}")

    print("\n=== per-symbol detail for qualifying BUYs ===")
    per = dbg.get("per_symbol") or {}
    for k in buys:
        info = per.get(k)
        print(f"  {k:12s} -> {info}")

    print(f"\nWOULD-SUBMIT count: {len(submitted)}")


if __name__ == "__main__":
    asyncio.run(main())

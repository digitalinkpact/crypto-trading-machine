"""Strategy lab — a single faithful event-driven simulator (entry -> stop_loss
/ TP1 / TP2 / trailing / mean-reversion-RSI-exit / max_hold, exactly mirroring
app/trading/risk.py's live exit ladder and app/trading/strategy.py's signal
exit) used to run every sweep this optimization pass needs with an IDENTICAL
execution model, so only the one parameter under test actually varies:

    python -m scripts.strategy_lab entry            # dip_buy vs oversold_bounce
    python -m scripts.strategy_lab rsi_exit          # old vs new RSI-exit params
    python -m scripts.strategy_lab trailing          # activation x distance grid
    python -m scripts.strategy_lab all               # everything, in the requested order

Why a custom simulator instead of vectorbt (scripts/walkforward.py): vectorbt's
`Portfolio.from_signals` takes one fixed sl_stop/tp_stop and has no notion of
running per-position PnL for a signal-based exit — it cannot express the TP1
(partial)/TP2(partial)/trailing-with-activation ladder or the mean-reversion
exit's PnL-gate at all. This module walks each symbol bar-by-bar (daily closed
candles only, no look-ahead — every decision at bar i only reads df.iloc[:i+1])
applying the exact same rule order as the live code.

Read-only: fetches public klines via the existing OHLCVRepository cache, never
places orders or touches trading.db.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np
import pandas as pd

from app.config import Timeframe, get_settings
from scripts.walkforward import SYMBOLS, _fold_bounds, _load  # noqa: F401 (reuse loader/fold splitter)

EntryFn = Callable[[pd.DataFrame, int], bool]


# ── Entry conditions (mirror app/trading/strategy.py exactly) ──────────────

def entry_dip_buy(df: pd.DataFrame, i: int) -> bool:
    row = df.iloc[i]
    return bool(row["rsi_14"] < 30 and row["close"] <= row["bb_lower"])


def make_entry_oversold_bounce(rsi_max: float, bb_mult: float, min_bounce_pct: float) -> EntryFn:
    def fn(df: pd.DataFrame, i: int) -> bool:
        if i < 5:
            return False
        row = df.iloc[i]
        low5 = float(df["low"].iloc[max(0, i - 4): i + 1].min())
        if low5 <= 0:
            return False
        bounced = row["close"] > low5 * (1 + min_bounce_pct)
        return bool(
            row["rsi_14"] < rsi_max
            and row["close"] < row["bb_lower"] * bb_mult
            and bounced
        )
    return fn


entry_oversold_bounce = make_entry_oversold_bounce(40.0, 1.02, 0.05)


# ── Trade / params containers ───────────────────────────────────────────

@dataclass
class LadderParams:
    stop_loss_pct: float
    tp1_pct: float
    tp1_frac: float
    tp2_pct: float
    tp2_frac: float
    trail_activation: float
    trail_distance: float
    max_hold_days: int
    mean_reversion_rsi: float
    mean_reversion_min_pnl: float
    mean_reversion_require_confirmation: bool
    fee_rate: float


def default_params(**overrides) -> LadderParams:
    s = get_settings()
    base = dict(
        stop_loss_pct=s.stop_loss_pct,
        tp1_pct=s.take_profit_1_pct,
        tp1_frac=s.take_profit_1_fraction,
        tp2_pct=s.take_profit_2_pct,
        tp2_frac=s.take_profit_2_fraction,
        trail_activation=s.trailing_activation_pct,
        trail_distance=s.trailing_stop_pct,
        max_hold_days=int(s.max_hold_hours / 24),
        mean_reversion_rsi=s.mean_reversion_exit_rsi,
        mean_reversion_min_pnl=s.mean_reversion_exit_min_pnl_pct,
        mean_reversion_require_confirmation=(
            s.mean_reversion_exit_require_momentum_confirmation
            or s.mean_reversion_exit_require_price_confirmation
        ),
        fee_rate=s.binance_taker_fee,
    )
    base.update(overrides)
    return LadderParams(**base)


@dataclass
class Trade:
    symbol: str
    entry_idx: int
    exit_idx: int
    entry_price: float
    exit_price: float
    qty: float
    pnl_pct: float  # relative to this leg's own entry notional
    exit_reason: str
    mfe_pct: float
    mae_pct: float


def _momentum_deteriorating(df: pd.DataFrame, i: int) -> bool:
    if i < 1 or "macd_hist" not in df.columns:
        return False
    return bool(df["macd_hist"].iloc[i] < df["macd_hist"].iloc[i - 1])


def _bearish_price_confirmation(df: pd.DataFrame, i: int) -> bool:
    if "ema_20" not in df.columns:
        return False
    row = df.iloc[i]
    return bool(row["close"] < row["ema_20"])


def simulate_symbol(
    symbol: str,
    df: pd.DataFrame,
    entry_fn: EntryFn,
    params: LadderParams,
    *,
    risk_on: Optional[pd.Series] = None,
    mean_reversion_priority: bool = False,
) -> list[Trade]:
    """Walk one symbol's daily candles bar-by-bar, applying the full live
    exit ladder (stop_loss -> TP1 -> TP2 -> trailing -> mean_reversion_rsi ->
    max_hold, same priority risk.py/strategy.py use) plus the BTC-regime entry
    gate. No look-ahead: entry_fn(df, i) and every exit check at bar i only
    reads df.iloc[:i+1].

    `mean_reversion_priority=True` checks the mean-reversion RSI exit BEFORE
    the price-based ladder instead of after. In production these are two
    independent, uncoordinated processes (the strategy tick vs. the 15s risk
    loop) racing on real intraday price, not one deterministic daily-bar
    order — this flag approximates "what if the RSI-exit signal won that
    race" as a bracketing scenario, since a single daily-close simulation
    can't reproduce the real race condition at all.
    """
    trades: list[Trade] = []
    entry_price: Optional[float] = None
    entry_idx: Optional[int] = None
    qty = 0.0
    original_qty = 0.0
    hwm = 0.0
    lwm = 0.0
    tp1_taken = False
    tp2_taken = False

    n = len(df)
    for i in range(n):
        row = df.iloc[i]
        price = float(row["close"])

        if entry_price is None:
            if risk_on is not None and not bool(risk_on.iloc[i]):
                continue
            if entry_fn(df, i):
                entry_price = price
                entry_idx = i
                qty = 1.0
                original_qty = 1.0
                hwm = price
                lwm = price
                tp1_taken = False
                tp2_taken = False
            continue

        hwm = max(hwm, price)
        lwm = min(lwm, price)
        change = (price - entry_price) / entry_price
        exit_reason: Optional[str] = None
        exit_qty = 0.0

        def _mean_reversion_hit() -> bool:
            return bool(
                row.get("rsi_14", 0) > params.mean_reversion_rsi
                and change >= params.mean_reversion_min_pnl
                and change < params.trail_activation  # defer to the ladder above this
                and (
                    not params.mean_reversion_require_confirmation
                    or _momentum_deteriorating(df, i)
                    or _bearish_price_confirmation(df, i)
                )
            )

        if mean_reversion_priority and _mean_reversion_hit():
            exit_reason, exit_qty = "mean_reversion_rsi", qty
        elif change <= -params.stop_loss_pct:
            exit_reason, exit_qty = "stop_loss", qty
        elif not tp1_taken and change >= params.tp1_pct:
            exit_reason, exit_qty = "take_profit_1", original_qty * params.tp1_frac
            tp1_taken = True
        elif tp1_taken and not tp2_taken and change >= params.tp2_pct:
            exit_reason, exit_qty = "take_profit_2", min(qty, original_qty * params.tp2_frac)
            tp2_taken = True
        elif hwm > entry_price * (1 + params.trail_activation) and price <= hwm * (1 - params.trail_distance):
            exit_reason, exit_qty = "trailing_stop", qty
        elif (not mean_reversion_priority) and _mean_reversion_hit():
            exit_reason, exit_qty = "mean_reversion_rsi", qty
        elif (i - entry_idx) >= params.max_hold_days:
            exit_reason, exit_qty = "max_hold", qty

        if exit_reason:
            exit_qty = min(exit_qty, qty)
            if exit_qty <= 0:
                continue
            gross = exit_qty * (price - entry_price)
            fees = exit_qty * entry_price * params.fee_rate + exit_qty * price * params.fee_rate
            pnl = gross - fees
            entry_notional = exit_qty * entry_price
            pnl_pct = (pnl / entry_notional) if entry_notional else 0.0
            mfe_pct = (hwm - entry_price) / entry_price
            mae_pct = (entry_price - lwm) / entry_price
            trades.append(Trade(
                symbol=symbol, entry_idx=entry_idx, exit_idx=i,
                entry_price=entry_price, exit_price=price, qty=exit_qty,
                pnl_pct=pnl_pct, exit_reason=exit_reason, mfe_pct=mfe_pct, mae_pct=mae_pct,
            ))
            qty -= exit_qty
            if qty <= 1e-9 or exit_reason in ("stop_loss", "trailing_stop", "max_hold", "mean_reversion_rsi"):
                entry_price = None
                entry_idx = None
                qty = 0.0

    return trades


# ── Aggregation ─────────────────────────────────────────────────────────

@dataclass
class Metrics:
    n: int = 0
    win_rate: float = 0.0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    profit_factor: float = 0.0
    expectancy_pct: float = 0.0
    total_pnl_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    max_losing_streak: int = 0
    mfe_captured_pct: float = 0.0  # avg(realized pnl% / mfe%) for winners, 0-1


def aggregate(trades: list[Trade], *, risk_per_trade_pct: float = 0.01) -> Metrics:
    if not trades:
        return Metrics()
    ordered = sorted(trades, key=lambda t: t.exit_idx)
    pcts = [t.pnl_pct for t in ordered]
    wins = [p for p in pcts if p > 0]
    losses = [p for p in pcts if p <= 0]
    n = len(pcts)
    win_rate = len(wins) / n
    avg_win = (sum(wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(losses) / len(losses)) if losses else 0.0
    profit_factor = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else float("inf") if wins else 0.0
    expectancy = sum(pcts) / n

    # Pooled equity curve: each trade risks a fixed `risk_per_trade_pct` of
    # equity (matches the live fixed-risk sizing model) — NOT full notional.
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    streak = 0
    max_streak = 0
    for t in ordered:
        equity *= (1 + t.pnl_pct * risk_per_trade_pct)
        peak = max(peak, equity)
        dd = (peak - equity) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)
        if t.pnl_pct <= 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0

    mfe_ratios = [
        (max(t.pnl_pct, 0) / t.mfe_pct) for t in ordered
        if t.mfe_pct > 0 and t.exit_reason not in ("stop_loss",)
    ]
    mfe_captured = float(np.mean(mfe_ratios)) if mfe_ratios else 0.0

    return Metrics(
        n=n, win_rate=win_rate, avg_win_pct=avg_win, avg_loss_pct=avg_loss,
        profit_factor=profit_factor, expectancy_pct=expectancy,
        total_pnl_pct=sum(pcts), max_drawdown_pct=max_dd,
        max_losing_streak=max_streak, mfe_captured_pct=mfe_captured,
    )


def print_metrics(label: str, m: Metrics) -> None:
    pf = f"{m.profit_factor:.2f}" if m.profit_factor != float("inf") else "inf"
    print(
        f"{label:<28} n={m.n:<5} win%={m.win_rate:>6.1%} pf={pf:>5} "
        f"expectancy={m.expectancy_pct:>+7.2%} avg_win={m.avg_win_pct:>+7.2%} "
        f"avg_loss={m.avg_loss_pct:>+7.2%} max_dd={m.max_drawdown_pct:>6.1%} "
        f"max_lose_streak={m.max_losing_streak:<3} mfe_captured={m.mfe_captured_pct:>5.1%}"
    )


# ── Fold-aware sweep runner ─────────────────────────────────────────────

async def _load_frames(tf: Timeframe, bars: int) -> dict[str, pd.DataFrame]:
    from app.data import OHLCVRepository
    return await _load(OHLCVRepository(), tf, bars)


def _risk_on_series(frames: dict[str, pd.DataFrame], sym: str) -> Optional[pd.Series]:
    btc = frames.get("BTCUSDT")
    if btc is None:
        return None
    mask = (btc["ema_50"] > btc["ema_200"])
    return mask.reindex(frames[sym].index, method="ffill").fillna(False)


def run_sweep(
    frames: dict[str, pd.DataFrame],
    entry_fn: EntryFn,
    params: LadderParams,
    *,
    folds: int,
    market_filter: bool,
    mean_reversion_priority: bool = False,
) -> tuple[Metrics, list[Metrics]]:
    """Runs the simulator per-symbol per-fold; returns (pooled, per-fold)."""
    all_trades: list[Trade] = []
    fold_trades: list[list[Trade]] = [[] for _ in range(folds)]
    for sym, df in frames.items():
        risk_on = _risk_on_series(frames, sym) if market_filter else None
        bounds = _fold_bounds(len(df), folds)
        for f, (lo, hi) in enumerate(bounds):
            sub = df.iloc[lo:hi]
            if len(sub) < 40:
                continue
            sub_risk_on = risk_on.iloc[lo:hi] if risk_on is not None else None
            trades = simulate_symbol(
                sym, sub, entry_fn, params, risk_on=sub_risk_on,
                mean_reversion_priority=mean_reversion_priority,
            )
            all_trades.extend(trades)
            fold_trades[f].extend(trades)
    pooled = aggregate(all_trades)
    per_fold = [aggregate(t) for t in fold_trades]
    return pooled, per_fold


# ── Sweep entrypoints ────────────────────────────────────────────────────

async def sweep_entry(frames: dict[str, pd.DataFrame], folds: int, market_filter: bool) -> None:
    print("\n### CHANGE 1 — ENTRY STRATEGY (dip_buy vs oversold_bounce), identical risk/exits ###")
    params = default_params()
    for name, fn in (("dip_buy", entry_dip_buy), ("oversold_bounce", entry_oversold_bounce)):
        pooled, per_fold = run_sweep(frames, fn, params, folds=folds, market_filter=market_filter)
        print_metrics(name, pooled)
        for i, m in enumerate(per_fold):
            if m.n:
                print(f"    fold {i}: " + " " * 0, end="")
                print_metrics(f"  fold {i}", m)


async def sweep_rsi_exit(frames: dict[str, pd.DataFrame], folds: int, market_filter: bool) -> None:
    print("\n### CHANGE 2 — RSI/MEAN-REVERSION EXIT (old vs new params), dip_buy entry ###")
    print(
        "NOTE: on daily bars evaluated in a single deterministic order, the price-based "
        "ladder (stop_loss/TP1/TP2/trailing) almost always resolves before RSI can recover "
        "enough to matter -- in production this is a real race between two independent "
        "processes (the risk loop vs. the strategy tick) on live intraday prices, which a "
        "single daily close can't reproduce. Reporting BOTH orderings as a bracket: "
        "'ladder_first' (my default ordering) and 'signal_first' (RSI-exit wins every race "
        "it's eligible for) -- the real live behavior sits somewhere between the two."
    )
    variants = {
        "old(rsi=55,min_pnl=0.0%,no_confirm)": default_params(
            mean_reversion_rsi=55, mean_reversion_min_pnl=0.0,
            mean_reversion_require_confirmation=False,
        ),
        "current(rsi=55,min_pnl=0.0%,confirm)": default_params(
            mean_reversion_rsi=55, mean_reversion_min_pnl=0.0,
            mean_reversion_require_confirmation=True,
        ),
        "proposed(rsi=60,min_pnl=0.25%,confirm)": default_params(
            mean_reversion_rsi=60, mean_reversion_min_pnl=0.0025,
            mean_reversion_require_confirmation=True,
        ),
    }
    for priority_label, priority in (("ladder_first", False), ("signal_first", True)):
        print(f"\n-- {priority_label} --")
        for name, params in variants.items():
            pooled, _ = run_sweep(
                frames, entry_dip_buy, params, folds=folds, market_filter=market_filter,
                mean_reversion_priority=priority,
            )
            print_metrics(name, pooled)


async def sweep_trailing(frames: dict[str, pd.DataFrame], folds: int, market_filter: bool) -> None:
    print("\n### CHANGE 3 — TRAILING ACTIVATION x DISTANCE grid, dip_buy entry ###")
    activations = [0.020, 0.025, 0.030]
    distances = [0.0125, 0.015, 0.020]
    print(f"(current live: activation=2.0% distance=1.0% — included for reference)")
    pooled, _ = run_sweep(
        frames, entry_dip_buy, default_params(trail_activation=0.02, trail_distance=0.01),
        folds=folds, market_filter=market_filter,
    )
    print_metrics("current(act=2.0%,dist=1.0%)", pooled)
    best = None
    for act in activations:
        for dist in distances:
            params = default_params(trail_activation=act, trail_distance=dist)
            pooled, _ = run_sweep(frames, entry_dip_buy, params, folds=folds, market_filter=market_filter)
            label = f"act={act:.1%},dist={dist:.2%}"
            print_metrics(label, pooled)
            if pooled.n >= 10 and (best is None or pooled.expectancy_pct > best[1].expectancy_pct):
                best = (label, pooled)
    if best:
        print(f"\nBest expectancy (n>=10): {best[0]} -> expectancy={best[1].expectancy_pct:+.2%} "
              f"max_dd={best[1].max_drawdown_pct:.1%} n={best[1].n}")


async def main_async(which: str, tf: Timeframe, folds: int, bars: int, market_filter: bool) -> None:
    print(f"Loading up to {bars} {tf.value} bars per symbol...")
    frames = await _load_frames(tf, bars)
    if not frames:
        print("No usable symbols.")
        return
    print(f"loaded {len(frames)} symbols, {folds} folds, market_filter={market_filter}")

    if which in ("entry", "all"):
        await sweep_entry(frames, folds, market_filter)
    if which in ("rsi_exit", "all"):
        await sweep_rsi_exit(frames, folds, market_filter)
    if which in ("trailing", "all"):
        await sweep_trailing(frames, folds, market_filter)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("which", choices=["entry", "rsi_exit", "trailing", "all"], default="all", nargs="?")
    p.add_argument("--timeframe", default="1d", choices=[t.value for t in Timeframe])
    p.add_argument("--folds", type=int, default=3)
    p.add_argument("--bars", type=int, default=1000)
    p.add_argument("--market-filter", action=argparse.BooleanOptionalAction, default=True)
    args = p.parse_args()
    asyncio.run(main_async(args.which, Timeframe(args.timeframe), args.folds, args.bars, args.market_filter))


if __name__ == "__main__":
    main()

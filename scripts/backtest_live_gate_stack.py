"""Closed-candle backtest of the LIVE daily entry path and its gate stack.

Reuses the production strategy's setup/candidate functions, the BTC regime
scorer and the score formula from `ProfitStreamStrategy.analyze_symbol`, then
applies the market (regime >= 0), strong-bull (regime >= 2) and breadth gates
and a daily-bar approximation of the risk ladder (ATR stop, TP1/TP2, trailing,
max hold). Variants change ONE parameter at a time, in simulation only.

Read-only: public Binance.US klines through app/exchange, no credentials, no
database writes. Not modelled: ML gate, spread, news blackout, orderbook and
funding gates, correlation gate, intrabar order of stop vs. target.

Usage:
    python -m scripts.backtest_live_gate_stack [--fee 0.001] [--slippage 0.001]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np
import pandas as pd

from app.config import STATIC_SYMBOLS, Timeframe, get_settings
from app.exchange.client import BinanceUSClient
from app.regime.btc_regime import compute_btc_regime_score
from app.ta import add_indicators
import app.trading.strategy as strategy_module
from app.trading.strategy import ProfitStreamStrategy

log = logging.getLogger("backtest_live_gate_stack")


@dataclass
class Variant:
    name: str
    threshold: int = 80
    market_gate: bool = True
    strong_bull: bool = True
    breadth_gate: bool = True
    settings: dict = field(default_factory=dict)


VARIANTS = (
    Variant("LIVE config"),
    Variant("no strong-bull (regime>=0 only)", strong_bull=False),
    Variant("no breadth gate", breadth_gate=False),
    Variant("no regime gates", market_gate=False, strong_bull=False),
    Variant("threshold 70", threshold=70),
    Variant("bounce_min_pct 0.03", settings={"oversold_bounce_min_bounce_pct": 0.03}),
    Variant("bb_multiplier 1.05", settings={"oversold_bounce_bb_multiplier": 1.05}),
    Variant("rsi_max 45", settings={"oversold_bounce_rsi_max": 45.0}),
    Variant("no pullback confirmation", settings={"pullback_require_candidate_confirmation": False}),
    Variant("no gates, threshold 0 (raw setups)", threshold=0, market_gate=False,
            strong_bull=False, breadth_gate=False),
    # Simulation-only exit-ladder experiments (entry gates unchanged).
    Variant("SIM exit: trail arms +3%", settings={"trailing_activation_pct": 0.03}),
    Variant("SIM exit: ATR stop 3x (4-12%)",
            settings={"atr_stop_multiple": 3.0, "atr_stop_max_pct": 0.12}),
    Variant("SIM exit: max hold 8d", settings={"max_hold_hours": 192}),
    Variant("SIM raw setups + trail +3%", threshold=0, market_gate=False, strong_bull=False,
            breadth_gate=False, settings={"trailing_activation_pct": 0.03}),
)


async def _load(symbols: list[str], limit: int) -> dict[str, pd.DataFrame]:
    client = BinanceUSClient()
    frames: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        try:
            raw = await client.klines(symbol, Timeframe.D1, limit=limit)
        except Exception as exc:  # noqa: BLE001
            log.warning("skip %s: %s", symbol, exc)
            continue
        # Drop the still-forming candle: only closed daily bars are used.
        raw = raw[raw.index <= pd.Timestamp.now(tz="UTC")]
        frames[symbol] = add_indicators(raw).replace([np.inf, -np.inf], np.nan).dropna()
    return frames


def _score(strategy: ProfitStreamStrategy, df: pd.DataFrame, btc_df: pd.DataFrame,
           regime_score: int) -> tuple[int, bool, bool]:
    s = strategy_module.get_settings()
    if s.entry_strategy == "oversold_bounce":
        dip_ready, _ = strategy._oversold_bounce_setup(df)
    else:
        dip_ready, _ = strategy._dip_buy_setup(df)
    pullback_ready, _, _ = strategy._pullback_setup(df)
    candidates = strategy._score_entry_candidates(df)
    if pullback_ready and s.pullback_require_candidate_confirmation:
        pullback_ready = candidates["pullback_to_ema"].ready
    btc_risk_on, _, _ = strategy._btc_risk_on(btc_df)
    extended, _ = strategy._dip_too_extended(df)
    filt_ok = (strategy._latest_quote_volume(df) >= s.profitstream_low_volume_quote_min
               and not extended)
    btc_aligned = btc_risk_on or regime_score >= 1
    penalty = 0
    if not btc_aligned:
        penalty = 10
        if s.btc_alignment_hard_gate:
            filt_ok = False
    score = (70 if dip_ready else 0) + (60 if pullback_ready else 0)
    score += (20 if btc_aligned else 0) + (10 if filt_ok else 0) - penalty
    score = max(0, min(100, score))
    if pullback_ready and not dip_ready and s.pullback_require_candidate_confirmation:
        score = min(score, candidates["pullback_to_ema"].score)
    return score, (dip_ready or pullback_ready) and filt_ok, dip_ready


def _simulate_exit(df: pd.DataFrame, entry_i: int, entry_px: float, cost: float,
                   s=None) -> tuple[float, int]:
    """Daily-bar risk ladder. Returns (net return fraction, exit bar index)."""
    s = s or get_settings()
    atr_pct = float(df["atr_14"].iloc[entry_i - 1] / df["close"].iloc[entry_i - 1]) \
        if "atr_14" in df.columns else s.stop_loss_pct
    stop_pct = min(max(s.atr_stop_multiple * atr_pct, s.atr_stop_min_pct), s.atr_stop_max_pct)
    stop = entry_px * (1 - stop_pct)
    remaining, realized, hwm = 1.0, 0.0, entry_px
    tp1_done = tp2_done = False
    max_bars = max(1, s.max_hold_hours // 24)
    last_i = min(len(df) - 1, entry_i + max_bars - 1)
    for i in range(entry_i, last_i + 1):
        low, high = float(df["low"].iloc[i]), float(df["high"].iloc[i])
        trail_armed = hwm >= entry_px * (1 + s.trailing_activation_pct)
        trail = hwm * (1 - s.trailing_stop_pct) if trail_armed else 0.0
        floor = max(stop, trail)
        # Conservative: assume the adverse level is hit before any target.
        if low <= floor:
            realized += remaining * (floor / entry_px - 1)
            return realized - cost, i
        if not tp1_done and high >= entry_px * (1 + s.take_profit_1_pct):
            realized += s.take_profit_1_fraction * s.take_profit_1_pct
            remaining -= s.take_profit_1_fraction
            tp1_done = True
        if not tp2_done and high >= entry_px * (1 + s.take_profit_2_pct):
            frac = min(remaining, s.take_profit_2_fraction)
            realized += frac * s.take_profit_2_pct
            remaining -= frac
            tp2_done = True
        hwm = max(hwm, high)
    realized += remaining * (float(df["close"].iloc[last_i]) / entry_px - 1)
    return realized - cost, last_i


def _metrics(returns: list[float], position_pct: float) -> dict:
    if not returns:
        return {"trades": 0}
    r = np.array(returns)
    wins, losses = r[r > 0], r[r <= 0]
    equity = np.cumprod(1 + r * position_pct)
    dd = float(np.max(1 - equity / np.maximum.accumulate(equity)))
    rng = np.random.default_rng(7)
    boots = rng.choice(r, size=(2000, len(r)), replace=True).mean(axis=1) * 100
    return {
        "trades": len(r),
        "avg_win_pct": float(wins.mean() * 100) if len(wins) else 0.0,
        "avg_loss_pct": float(losses.mean() * 100) if len(losses) else 0.0,
        "exp_ci95": (float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))),
        "win_rate": float(len(wins) / len(r)),
        "profit_factor": float(wins.sum() / -losses.sum()) if losses.sum() < 0 else float("inf"),
        "expectancy_pct": float(r.mean() * 100),
        "max_dd_pct": dd * 100,
        "return_pct": float((equity[-1] - 1) * 100),
    }


def run_variant(variant: Variant, frames: dict[str, pd.DataFrame], cost: float,
                split: pd.Timestamp) -> dict:
    base = get_settings()
    settings = base.model_copy(update=variant.settings)
    original: Callable = strategy_module.get_settings
    strategy_module.get_settings = lambda: settings
    try:
        strategy = ProfitStreamStrategy(client=None)  # type: ignore[arg-type]
        btc = frames["BTCUSDT"]
        dates = btc.index[260:]
        held_until: dict[str, pd.Timestamp] = {}
        trades: list[tuple[pd.Timestamp, float, int]] = []
        best_scores: list[int] = []
        stats = {"days": 0, "score_ge_threshold": 0, "blocked_market": 0,
                 "blocked_strong_bull": 0, "blocked_breadth": 0, "regime_ge2_days": 0}
        for d in dates:
            btc_slice = btc.loc[:d]
            regime = compute_btc_regime_score(btc_slice).score
            stats["days"] += 1
            stats["regime_ge2_days"] += regime >= 2
            above = [bool(f.loc[d, "close"] > f.loc[d, f"ema_{settings.breadth_ema_span}"])
                     for sym, f in frames.items() if sym != "BTCUSDT" and d in f.index]
            breadth_ok = (len(above) >= settings.breadth_min_symbols
                          and sum(above) / len(above) >= settings.breadth_min_fraction)
            for sym, f in frames.items():
                if d not in f.index or held_until.get(sym, d - pd.Timedelta(days=1)) >= d:
                    continue
                i = f.index.get_loc(d)
                if i < 60 or i + 1 >= len(f):
                    continue
                score, ready, _ = _score(strategy, f.iloc[: i + 1], btc_slice, regime)
                best_scores.append(score)
                if not ready or score < variant.threshold:
                    continue
                stats["score_ge_threshold"] += 1
                if variant.market_gate and regime < 0:
                    stats["blocked_market"] += 1
                    continue
                if variant.strong_bull and regime < 2:
                    stats["blocked_strong_bull"] += 1
                    continue
                if variant.breadth_gate and not breadth_ok:
                    stats["blocked_breadth"] += 1
                    continue
                entry_px = float(f["open"].iloc[i + 1]) * (1 + cost / 2)
                ret, exit_i = _simulate_exit(f, i + 1, entry_px, cost, settings)
                held_until[sym] = f.index[exit_i]
                trades.append((d, ret, regime))
        ins = [r for d, r, _ in trades if d < split]
        oos = [r for d, r, _ in trades if d >= split]
        pct = float(settings.max_position_pct)
        by_regime = {}
        for bucket, test in (("bear(<0)", lambda g: g < 0), ("neutral(0-1)", lambda g: 0 <= g < 2),
                             ("bull(>=2)", lambda g: g >= 2)):
            by_regime[bucket] = _metrics([r for _, r, g in trades if test(g)], pct)
        sc = np.array(best_scores) if best_scores else np.zeros(1)
        stats["score_max"] = int(sc.max())
        stats["score_ge70"] = int((sc >= 70).sum())
        stats["score_ge80"] = int((sc >= 80).sum())
        stats["score_evals"] = int(len(best_scores))
        return {"variant": variant.name, **stats, "by_regime": by_regime,
                "all": _metrics([r for _, r, _ in trades], pct),
                "in_sample": _metrics(ins, pct), "out_of_sample": _metrics(oos, pct)}
    finally:
        strategy_module.get_settings = original


def _fmt(m: dict) -> str:
    if not m.get("trades"):
        return "trades=0"
    lo, hi = m["exp_ci95"]
    return (f"trades={m['trades']:3d} win={m['win_rate']:.0%} PF={m['profit_factor']:.2f} "
            f"exp={m['expectancy_pct']:+.2f}% [95%CI {lo:+.2f},{hi:+.2f}] "
            f"avgW={m['avg_win_pct']:+.2f}% avgL={m['avg_loss_pct']:+.2f}% "
            f"DD={m['max_dd_pct']:.2f}% ret={m['return_pct']:+.2f}%")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fee", type=float, default=0.001, help="per side")
    parser.add_argument("--slippage", type=float, default=0.001, help="per side")
    parser.add_argument("--limit", type=int, default=1000, help="daily bars per symbol")
    parser.add_argument("--oos-fraction", type=float, default=0.3)
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)

    symbols = sorted(set(STATIC_SYMBOLS) | {"BTCUSDT"})
    frames = asyncio.run(_load(symbols, args.limit))
    btc = frames["BTCUSDT"]
    dates = btc.index[260:]
    split = dates[int(len(dates) * (1 - args.oos_fraction))]
    cost = 2 * (args.fee + args.slippage)
    print(f"symbols={len(frames)} days={len(dates)} {dates[0].date()}..{dates[-1].date()} "
          f"OOS from {split.date()} round-trip cost={cost:.2%}")
    for variant in VARIANTS:
        res = run_variant(variant, frames, cost, split)
        print(f"\n[{res['variant']}] regime>=2 days={res['regime_ge2_days']}/{res['days']} "
              f"setups>=thr={res['score_ge_threshold']} blocked market={res['blocked_market']} "
              f"strong_bull={res['blocked_strong_bull']} breadth={res['blocked_breadth']}")
        print(f"   scores: max={res['score_max']} >=70:{res['score_ge70']} >=80:{res['score_ge80']} "
              f"of {res['score_evals']} symbol-days")
        print(f"   IS : {_fmt(res['in_sample'])}")
        print(f"   OOS: {_fmt(res['out_of_sample'])}")
        print(f"   ALL: {_fmt(res['all'])}")
        for bucket, m in res["by_regime"].items():
            print(f"     {bucket:13s} {_fmt(m)}")


if __name__ == "__main__":
    main()

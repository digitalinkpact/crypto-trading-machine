"""Trade forensics from persisted closed-trade history.

Links each closed trade back to entry-time market context using cached OHLCV and
reports the patterns that most often appear in losers.

Usage:
    python -m scripts.trade_forensics
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

import pandas as pd

from app.config import get_settings
from app.storage import storage
from app.ta import add_indicators


@dataclass
class TradeContext:
    symbol: str
    mode: str
    entry_ts: datetime
    exit_ts: datetime
    pnl: float
    pnl_pct: float
    won: bool
    agents: list[str]
    symbol_regime: str
    btc_regime: str
    above_ema200: bool
    quote_volume: float | None
    atr_pct: float | None
    three_green: bool | None
    low_volume: bool = False
    high_volatility: bool = False
    vetoes: tuple[str, ...] = ()


def _parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _load_frame(cache_dir: Path, symbol: str, timeframe: str) -> pd.DataFrame | None:
    path = cache_dir / f"{symbol}_{timeframe}.pkl"
    if not path.exists():
        return None
    df = pd.read_pickle(path)
    if df is None or df.empty:
        return None
    if "ema_20" not in df.columns:
        df = add_indicators(df)
    return df.dropna().sort_index()


def _row_at_or_before(df: pd.DataFrame | None, ts: datetime) -> pd.Series | None:
    if df is None or df.empty:
        return None
    subset = df.loc[df.index <= ts]
    if subset.empty:
        return None
    return subset.iloc[-1]


def _symbol_regime(row: pd.Series | None) -> tuple[str, bool]:
    if row is None:
        return "unknown", False
    close = float(row.get("close", 0.0))
    ema20 = float(row.get("ema_20", 0.0))
    ema50 = float(row.get("ema_50", 0.0))
    ema200 = float(row.get("ema_200", 0.0))
    above = close >= ema200 if ema200 > 0 else False
    if close > ema200 and ema20 > ema50 and ema50 > ema200:
        return "uptrend", True
    if close < ema200 and ema20 < ema50 and ema50 < ema200:
        return "downtrend", False
    return "sideways", above


def _three_green(df: pd.DataFrame | None, ts: datetime) -> bool | None:
    if df is None or df.empty:
        return None
    subset = df.loc[df.index <= ts].tail(3)
    if len(subset) < 3:
        return None
    return bool(((subset["close"] > subset["open"]).astype(int).sum()) == 3)


def _quote_volume(row: pd.Series | None) -> float | None:
    if row is None:
        return None
    if "quote_volume" in row and pd.notna(row["quote_volume"]):
        return float(row["quote_volume"])
    close = row.get("close")
    volume = row.get("volume")
    if pd.isna(close) or pd.isna(volume):
        return None
    return float(close) * float(volume)


def _atr_pct(row: pd.Series | None) -> float | None:
    if row is None:
        return None
    close = float(row.get("close", 0.0))
    atr = float(row.get("atr_14", 0.0))
    if close <= 0 or atr <= 0:
        return None
    return atr / close


def _candidate_vetoes(ctx: TradeContext) -> tuple[str, ...]:
    vetoes: list[str] = []
    if ctx.symbol_regime != "uptrend":
        vetoes.append("symbol_trend_filter")
    if ctx.btc_regime != "uptrend":
        vetoes.append("btc_market_regime_filter")
    if not ctx.above_ema200:
        vetoes.append("ema200_filter")
    if ctx.low_volume:
        vetoes.append("liquidity_filter")
    if ctx.high_volatility:
        vetoes.append("atr_volatility_filter")
    if ctx.three_green:
        vetoes.append("three_green_exhaustion_filter")
    return tuple(vetoes)


def _pct(count: int, total: int) -> float:
    return (count / total * 100.0) if total else 0.0


def build_trade_contexts() -> list[TradeContext]:
    settings = get_settings()
    cache_dir = settings.data_cache_dir
    closed = storage.closed_trades(limit=5000)
    btc_1d = _load_frame(cache_dir, "BTCUSDT", "1d")
    frame_cache: dict[tuple[str, str], pd.DataFrame | None] = {("BTCUSDT", "1d"): btc_1d}
    contexts: list[TradeContext] = []

    for trade in sorted(closed, key=lambda row: row.get("entry_ts") or ""):
        entry_ts = _parse_dt(str(trade.get("entry_ts")))
        exit_ts = _parse_dt(str(trade.get("exit_ts")))
        symbol = str(trade.get("symbol") or "")
        if not symbol:
            continue
        for key in ((symbol, "1d"), (symbol, "4h")):
            if key not in frame_cache:
                frame_cache[key] = _load_frame(cache_dir, key[0], key[1])
        sym_1d = frame_cache[(symbol, "1d")]
        sym_4h = frame_cache[(symbol, "4h")]
        row_1d = _row_at_or_before(sym_1d, entry_ts)
        row_4h = _row_at_or_before(sym_4h, entry_ts)
        btc_row = _row_at_or_before(btc_1d, entry_ts)
        feature_row = row_4h if row_4h is not None else row_1d
        symbol_regime, above_ema200 = _symbol_regime(row_1d)
        btc_regime, _ = _symbol_regime(btc_row)
        try:
            agents = json.loads(str(trade.get("agents") or "[]"))
        except json.JSONDecodeError:
            agents = []
        contexts.append(
            TradeContext(
                symbol=symbol,
                mode=str(trade.get("mode") or ""),
                entry_ts=entry_ts,
                exit_ts=exit_ts,
                pnl=float(trade.get("pnl") or 0.0),
                pnl_pct=float(trade.get("pnl_pct") or 0.0),
                won=float(trade.get("pnl") or 0.0) > 0.0,
                agents=[str(agent) for agent in agents],
                symbol_regime=symbol_regime,
                btc_regime=btc_regime,
                above_ema200=above_ema200,
                quote_volume=_quote_volume(feature_row),
                atr_pct=_atr_pct(feature_row),
                three_green=_three_green(sym_4h, entry_ts),
            )
        )

    volumes = sorted(ctx.quote_volume for ctx in contexts if ctx.quote_volume is not None)
    atrs = sorted(ctx.atr_pct for ctx in contexts if ctx.atr_pct is not None)
    volume_q25 = volumes[max(0, int(len(volumes) * 0.25) - 1)] if volumes else None
    atr_q75 = atrs[min(len(atrs) - 1, int(len(atrs) * 0.75))] if atrs else None

    for ctx in contexts:
        ctx.low_volume = bool(volume_q25 is not None and ctx.quote_volume is not None and ctx.quote_volume <= volume_q25)
        ctx.high_volatility = bool(atr_q75 is not None and ctx.atr_pct is not None and ctx.atr_pct >= atr_q75)
        ctx.vetoes = _candidate_vetoes(ctx)
    return contexts


def print_report(contexts: list[TradeContext], *, limit: int) -> None:
    losses = [ctx for ctx in contexts if not ctx.won]
    wins = [ctx for ctx in contexts if ctx.won]

    print(f"trades={len(contexts)} wins={len(wins)} losses={len(losses)}")
    if not losses:
        return

    loss_symbol_regimes = Counter(ctx.symbol_regime for ctx in losses)
    loss_btc_regimes = Counter(ctx.btc_regime for ctx in losses)
    veto_counts = Counter(veto for ctx in losses for veto in ctx.vetoes)
    loss_agents = Counter(agent for ctx in losses for agent in ctx.agents)
    win_agents = Counter(agent for ctx in wins for agent in ctx.agents)
    loss_symbols = Counter(ctx.symbol for ctx in losses)

    print("\nLoss patterns")
    print(f"sideways_losses={sum(1 for ctx in losses if ctx.symbol_regime == 'sideways')} ({_pct(sum(1 for ctx in losses if ctx.symbol_regime == 'sideways'), len(losses)):.1f}%)")
    print(f"downtrend_losses={sum(1 for ctx in losses if ctx.symbol_regime == 'downtrend')} ({_pct(sum(1 for ctx in losses if ctx.symbol_regime == 'downtrend'), len(losses)):.1f}%)")
    print(f"uptrend_losses={sum(1 for ctx in losses if ctx.symbol_regime == 'uptrend')} ({_pct(sum(1 for ctx in losses if ctx.symbol_regime == 'uptrend'), len(losses)):.1f}%)")
    print(f"btc_risk_off_losses={sum(1 for ctx in losses if ctx.btc_regime != 'uptrend')} ({_pct(sum(1 for ctx in losses if ctx.btc_regime != 'uptrend'), len(losses)):.1f}%)")
    print(f"low_volume_losses={sum(1 for ctx in losses if ctx.low_volume)} ({_pct(sum(1 for ctx in losses if ctx.low_volume), len(losses)):.1f}%)")
    print(f"high_volatility_losses={sum(1 for ctx in losses if ctx.high_volatility)} ({_pct(sum(1 for ctx in losses if ctx.high_volatility), len(losses)):.1f}%)")
    print(f"three_green_losses={sum(1 for ctx in losses if ctx.three_green)} ({_pct(sum(1 for ctx in losses if ctx.three_green), len(losses)):.1f}%)")
    print(f"winners_above_ema200={sum(1 for ctx in wins if ctx.above_ema200)} / {len(wins)} ({_pct(sum(1 for ctx in wins if ctx.above_ema200), len(wins)):.1f}%)")
    print(f"losses_above_ema200={sum(1 for ctx in losses if ctx.above_ema200)} / {len(losses)} ({_pct(sum(1 for ctx in losses if ctx.above_ema200), len(losses)):.1f}%)")

    print("\nLoss regimes by symbol")
    for regime, count in loss_symbol_regimes.most_common():
        print(f"{regime}: {count} ({_pct(count, len(losses)):.1f}%)")

    print("\nLoss regimes by BTC")
    for regime, count in loss_btc_regimes.most_common():
        print(f"{regime}: {count} ({_pct(count, len(losses)):.1f}%)")

    print("\nTop loss symbols")
    for symbol, count in loss_symbols.most_common(10):
        print(f"{symbol}: {count} losses")

    print("\nContributing agents in losses")
    for agent, count in loss_agents.most_common():
        print(f"{agent}: {count} losses ({_pct(count, len(losses)):.1f}% of losses)")

    print("\nContributing agents in wins")
    for agent, count in win_agents.most_common():
        print(f"{agent}: {count} wins ({_pct(count, len(wins)):.1f}% of wins)")

    print("\nCandidate veto filters for losses")
    for veto, count in veto_counts.most_common():
        print(f"{veto}: {count} ({_pct(count, len(losses)):.1f}% of losses)")

    print("\nLosing trades")
    for ctx in sorted(losses, key=lambda item: item.entry_ts, reverse=True)[:limit]:
        print(
            f"{ctx.entry_ts.isoformat()} {ctx.symbol} pnl_pct={ctx.pnl_pct:+.2f}% "
            f"symbol_regime={ctx.symbol_regime} btc_regime={ctx.btc_regime} "
            f"low_volume={ctx.low_volume} high_volatility={ctx.high_volatility} "
            f"three_green={ctx.three_green} agents={','.join(ctx.agents) or 'none'} "
            f"vetoes={','.join(ctx.vetoes) or 'none'}"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=25, help="how many recent losing trades to print")
    args = parser.parse_args()

    contexts = build_trade_contexts()
    print_report(contexts, limit=args.limit)


if __name__ == "__main__":
    main()
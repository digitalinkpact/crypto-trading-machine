"""ProfitStream multi-timeframe strategy.

Focus: quality entries, strict filters, and explicit rejection reasons.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from statistics import pstdev
from typing import Any, Optional

import pandas as pd
from ta.trend import EMAIndicator, MACD

from app.config import get_settings
from app.exchange import BinanceUSClient
from app.exchange.orderbook import analyze_order_book
from app.logging_setup import get_logger
from app.signals import SignalAction
from app.storage import storage
from app.ta import add_indicators

log = get_logger(__name__)


@dataclass
class StrategyDecision:
    symbol: str
    action: SignalAction
    score: int
    reasons: list[str]
    indicators: dict[str, Any]


class ProfitStreamStrategy:
    """Rule-based strategy with AI-like scoring and explainable rejects."""

    def __init__(self, client: Optional[BinanceUSClient] = None) -> None:
        self._client = client or BinanceUSClient()

    async def analyze_symbol(self, symbol: str, *, mode: str) -> StrategyDecision:
        s = get_settings()
        reasons: list[str] = []
        indicators: dict[str, Any] = {"symbol": symbol}

        try:
            df_1m = await self._candles(symbol, "1m", 240)
            df_5m = await self._candles(symbol, "5m", 240)
            df_15m = await self._candles(symbol, "15m", 240)
            df_1h = await self._candles(symbol, "1h", 240)
            btc_1h = await self._candles("BTCUSDT", "1h", 240)
        except Exception as exc:  # noqa: BLE001
            reasons.append(f"data_unavailable:{exc}")
            return StrategyDecision(symbol, SignalAction.HOLD, 0, reasons, indicators)

        if min(len(df_1m), len(df_5m), len(df_15m), len(df_1h), len(btc_1h)) < 60:
            reasons.append("insufficient_history")
            return StrategyDecision(symbol, SignalAction.HOLD, 0, reasons, indicators)

        # Exit-first logic: if held and 5m MACD reverses bearish, sell immediately.
        held = self._is_held(symbol, mode)
        macd_reversal = self._macd_bear_reversal(df_5m)
        indicators["macd_reversal_5m"] = macd_reversal
        if held and macd_reversal:
            indicators["decision"] = "sell_macd_reversal_5m"
            return StrategyDecision(symbol, SignalAction.SELL, 95, reasons, indicators)

        # Entry score components.
        ema_cross = self._ema_bull_cross(df_1m, short=9, long=21)
        rsi_ok, rsi_val = self._rsi_between(df_5m, s.profitstream_rsi_min, s.profitstream_rsi_max)
        vol_spike, vol_ratio, quote_1m = self._volume_spike(df_1m, s.profitstream_volume_spike_multiple)
        macd_bull = self._macd_bull(df_15m)
        btc_aligned = self._btc_trend_aligned(btc_1h)

        indicators.update(
            {
                "ema_cross_1m": ema_cross,
                "rsi_5m": rsi_val,
                "rsi_ok": rsi_ok,
                "volume_spike_1m": vol_spike,
                "volume_ratio_1m": vol_ratio,
                "quote_volume_1m": quote_1m,
                "macd_bull_15m": macd_bull,
                "btc_aligned_1h": btc_aligned,
            }
        )

        # Market filters.
        filt_ok = True

        btc_vol = self._btc_volatility(btc_1h)
        indicators["btc_volatility_1h"] = btc_vol
        if btc_vol > s.profitstream_btc_volatility_threshold:
            filt_ok = False
            reasons.append(
                f"btc_volatility_high:{btc_vol:.4f}>{s.profitstream_btc_volatility_threshold:.4f}"
            )

        if quote_1m < s.profitstream_low_volume_quote_min:
            filt_ok = False
            reasons.append(
                f"low_volume:{quote_1m:.2f}<{s.profitstream_low_volume_quote_min:.2f}"
            )

        near_news, next_news = self._near_news_event()
        indicators["near_news"] = near_news
        if near_news:
            filt_ok = False
            reasons.append(f"news_blackout:{next_news}")

        spread_pct = await self._spread_pct(symbol)
        indicators["spread_pct"] = spread_pct
        if spread_pct is not None and spread_pct > 0.0025:
            filt_ok = False
            reasons.append(f"spread_wide:{spread_pct:.4%}>0.2500%")

        # Weighted score 0-100.
        score = 0
        score += 25 if ema_cross else 0
        score += 15 if rsi_ok else 0
        score += 15 if vol_spike else 0
        score += 20 if macd_bull else 0
        score += 15 if btc_aligned else 0
        score += 10 if filt_ok else 0

        if not ema_cross:
            reasons.append("ema9_not_crossing_ema21")
        if not rsi_ok:
            reasons.append("rsi_outside_40_65")
        if not vol_spike:
            reasons.append("volume_spike_missing")
        if not macd_bull:
            reasons.append("macd_bull_confirmation_missing")
        if not btc_aligned:
            reasons.append("btc_trend_not_aligned")

        # Overtrading guard: if already held, suppress duplicate BUY signals.
        if held:
            reasons.append("position_already_open")
            return StrategyDecision(symbol, SignalAction.HOLD, score, reasons, indicators)

        if score >= s.profitstream_score_threshold and filt_ok:
            indicators["decision"] = "buy"
            return StrategyDecision(symbol, SignalAction.BUY, score, reasons, indicators)

        indicators["decision"] = "hold"
        return StrategyDecision(symbol, SignalAction.HOLD, score, reasons, indicators)

    async def _candles(self, symbol: str, interval: str, limit: int) -> pd.DataFrame:
        df = await self._client.klines(symbol, interval, limit=limit)
        if "ema_20" not in df.columns:
            df = add_indicators(df)
        return df.dropna()

    def _ema_bull_cross(self, df: pd.DataFrame, *, short: int, long: int) -> bool:
        out = df.copy()
        out["ema_s"] = EMAIndicator(close=out["close"], window=short).ema_indicator()
        out["ema_l"] = EMAIndicator(close=out["close"], window=long).ema_indicator()
        out = out.dropna()
        if len(out) < 3:
            return False
        prev = out.iloc[-2]
        cur = out.iloc[-1]
        return bool(prev["ema_s"] <= prev["ema_l"] and cur["ema_s"] > cur["ema_l"])

    def _rsi_between(self, df: pd.DataFrame, lo: int, hi: int) -> tuple[bool, float]:
        if "rsi_14" not in df.columns or df.empty:
            return False, 0.0
        rsi = float(df.iloc[-1]["rsi_14"])
        return lo <= rsi <= hi, rsi

    def _volume_spike(self, df: pd.DataFrame, multiple: float) -> tuple[bool, float, float]:
        out = df.copy()
        out["vol_ma"] = out["volume"].rolling(20).mean()
        out["quote"] = out["close"] * out["volume"]
        out = out.dropna()
        if out.empty:
            return False, 0.0, 0.0
        last = out.iloc[-1]
        ratio = float(last["volume"] / last["vol_ma"]) if float(last["vol_ma"]) > 0 else 0.0
        quote = float(last["quote"])
        return ratio >= multiple, ratio, quote

    def _macd_bull(self, df: pd.DataFrame) -> bool:
        if df.empty:
            return False
        if "macd" in df.columns and "macd_signal" in df.columns:
            last = df.iloc[-1]
            return bool(float(last["macd"]) > float(last["macd_signal"]))
        macd = MACD(close=df["close"])
        line = macd.macd().dropna()
        sig = macd.macd_signal().dropna()
        if line.empty or sig.empty:
            return False
        return bool(float(line.iloc[-1]) > float(sig.iloc[-1]))

    def _macd_bear_reversal(self, df: pd.DataFrame) -> bool:
        out = df.dropna().copy()
        if "macd" not in out.columns or "macd_signal" not in out.columns or len(out) < 3:
            macd = MACD(close=out["close"])
            out["macd"] = macd.macd()
            out["macd_signal"] = macd.macd_signal()
            out = out.dropna()
            if len(out) < 3:
                return False
        prev = out.iloc[-2]
        cur = out.iloc[-1]
        return bool(float(prev["macd"]) >= float(prev["macd_signal"]) and float(cur["macd"]) < float(cur["macd_signal"]))

    def _btc_trend_aligned(self, btc_1h: pd.DataFrame) -> bool:
        out = btc_1h.dropna()
        if len(out) < 60:
            return False
        ema20 = float(out.iloc[-1]["ema_20"])
        ema50 = float(out.iloc[-1]["ema_50"])
        close = float(out.iloc[-1]["close"])
        return close > ema50 and ema20 > ema50

    def _btc_volatility(self, btc_1h: pd.DataFrame) -> float:
        out = btc_1h.dropna()
        if len(out) < 30:
            return 0.0
        rets = out["close"].pct_change().dropna().tail(24)
        if rets.empty:
            return 0.0
        return float(pstdev(float(x) for x in rets.tolist()))

    def _near_news_event(self) -> tuple[bool, str]:
        s = get_settings()
        raw = (s.profitstream_news_events_utc or "").strip()
        if not raw:
            return False, ""
        now = datetime.now(timezone.utc)
        buf = timedelta(minutes=s.profitstream_news_buffer_minutes)
        for token in [p.strip() for p in raw.split(",") if p.strip()]:
            try:
                event_dt = datetime.fromisoformat(token)
                if event_dt.tzinfo is None:
                    event_dt = event_dt.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if abs(now - event_dt) <= buf:
                return True, token
        return False, ""

    async def _spread_pct(self, symbol: str) -> Optional[float]:
        try:
            raw = await self._client.order_book(symbol, limit=5)
            bids = [(Decimal(str(x[0])), Decimal(str(x[1]))) for x in raw.get("bids", []) if x]
            asks = [(Decimal(str(x[0])), Decimal(str(x[1]))) for x in raw.get("asks", []) if x]
            metrics = analyze_order_book(bids, asks)
            if metrics is None:
                return None
            return float(metrics.spread_pct)
        except Exception:  # noqa: BLE001
            return None

    def _is_held(self, symbol: str, mode: str) -> bool:
        return any(p["symbol"] == symbol and p["mode"] == mode for p in storage.all_positions())

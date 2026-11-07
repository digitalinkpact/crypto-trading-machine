"""Tests for the alt-market breadth gate (Autopilot._breadth_gate).

The gate reads cached 1d OHLCV pickles; here the repo is patched so no disk or
network is touched. Covers: pass, block, fail-closed on too few symbols,
fail-closed on exception, disabled flag, and 5-minute cache reuse.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import app.trading.autopilot as autopilot_module
from app.trading.autopilot import Autopilot


def _frame(close: float, ema20: float) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=60, freq="1D", tz="UTC")
    n = len(idx)
    return pd.DataFrame(
        {
            "open": [close] * n, "high": [close] * n, "low": [close] * n,
            "close": [close] * n, "volume": [1.0] * n,
            "ema_20": [ema20] * n, "ema_50": [ema20] * n, "ema_200": [ema20] * n,
            "rsi_14": [50.0] * n, "atr_14": [1.0] * n,
            "macd": [0.0] * n, "macd_signal": [0.0] * n, "macd_hist": [0.0] * n,
            "bb_lower": [close] * n, "bb_mid": [close] * n, "bb_upper": [close] * n,
        },
        index=idx,
    )


class _Settings:
    breadth_gate_enabled = True
    breadth_ema_span = 20
    breadth_min_fraction = 0.50
    breadth_min_symbols = 3
    data_cache_dir = Path("/nonexistent")


def _wire(monkeypatch, frames: dict[str, pd.DataFrame], settings=_Settings) -> Autopilot:
    """Build an Autopilot (needs real settings), THEN patch settings + repo so
    the gate sees exactly `frames` as the universe. Returns the instance."""
    ap = Autopilot()
    monkeypatch.setattr(autopilot_module, "get_settings", lambda: settings())

    fake_paths = [Path(f"/x/{s}_1d.pkl") for s in frames]
    monkeypatch.setattr(Path, "glob", lambda self, pat: iter(fake_paths), raising=True)

    class _Repo:
        async def get(self, symbol, tf, refresh=False):
            return frames[symbol]

    import app.data as data_mod
    monkeypatch.setattr(data_mod, "OHLCVRepository", _Repo, raising=True)
    import app.ta as ta_mod
    monkeypatch.setattr(ta_mod, "add_indicators", lambda df: df, raising=True)
    return ap


@pytest.mark.asyncio
async def test_breadth_allows_when_majority_above_ema(monkeypatch):
    frames = {
        "AAAUSDT": _frame(101, 100), "BBBUSDT": _frame(102, 100),
        "CCCUSDT": _frame(103, 100), "DDDUSDT": _frame(99, 100),
    }
    ap = _wire(monkeypatch, frames)
    ok, why = await ap._breadth_gate()
    assert ok is True
    assert "3/4=75%" in why


@pytest.mark.asyncio
async def test_breadth_blocks_when_minority_above_ema(monkeypatch):
    frames = {
        "AAAUSDT": _frame(99, 100), "BBBUSDT": _frame(98, 100),
        "CCCUSDT": _frame(97, 100), "DDDUSDT": _frame(101, 100),
    }
    ap = _wire(monkeypatch, frames)
    ok, why = await ap._breadth_gate()
    assert ok is False
    assert "1/4=25%" in why


@pytest.mark.asyncio
async def test_breadth_excludes_btc_from_universe(monkeypatch):
    # BTC strongly above its EMA must NOT rescue a weak alt market.
    frames = {"BTCUSDT": _frame(200, 100), "AAAUSDT": _frame(99, 100),
              "BBBUSDT": _frame(98, 100), "CCCUSDT": _frame(97, 100)}
    ap = _wire(monkeypatch, frames)
    ok, why = await ap._breadth_gate()
    assert ok is False
    assert "0/3" in why


@pytest.mark.asyncio
async def test_breadth_fails_closed_on_too_few_symbols(monkeypatch):
    frames = {"AAAUSDT": _frame(101, 100), "BBBUSDT": _frame(102, 100)}  # < min 3
    ap = _wire(monkeypatch, frames)
    ok, why = await ap._breadth_gate()
    assert ok is False
    assert why.startswith("breadth_insufficient_symbols")


@pytest.mark.asyncio
async def test_breadth_fails_closed_on_exception(monkeypatch):
    class _S(_Settings):
        data_cache_dir = None  # Path(None) raises -> fail-closed branch
    ap = _wire(monkeypatch, {}, settings=_S)
    ok, why = await ap._breadth_gate()
    assert ok is False
    assert why.startswith("breadth_unavailable")


@pytest.mark.asyncio
async def test_breadth_disabled_flag_allows(monkeypatch):
    class _S(_Settings):
        breadth_gate_enabled = False
    ap = Autopilot()
    monkeypatch.setattr(autopilot_module, "get_settings", lambda: _S())
    ok, why = await ap._breadth_gate()
    assert ok is True and why == "breadth_gate_disabled"


@pytest.mark.asyncio
async def test_breadth_result_is_cached_for_five_minutes(monkeypatch):
    calls = {"n": 0}
    frames = {"AAAUSDT": _frame(101, 100), "BBBUSDT": _frame(102, 100), "CCCUSDT": _frame(103, 100)}
    ap = _wire(monkeypatch, frames)
    import app.data as data_mod
    class _Counting:
        async def get(self, symbol, tf, refresh=False):
            calls["n"] += 1
            return frames[symbol]
    monkeypatch.setattr(data_mod, "OHLCVRepository", _Counting, raising=True)
    await ap._breadth_gate(); first = calls["n"]
    await ap._breadth_gate()
    assert calls["n"] == first  # second call served from cache

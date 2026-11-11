from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.config import DAILY_FETCH_BARS, DAILY_HISTORY_BARS, DAILY_MIN_CLOSED_BARS
from app.data.daily import latest_daily_close, load_daily_snapshot
from app.data.ohlcv import OHLCVRepository


def _raw(end=None, count=DAILY_FETCH_BARS):
    end = end if end is not None else latest_daily_close() + pd.Timedelta(days=1)
    index = pd.date_range(end=end, periods=count, freq="D")
    close = np.linspace(100, 200, count) + np.sin(np.arange(count))
    return pd.DataFrame(
        {
            "open": close, "high": close + 2, "low": close - 2,
            "close": close, "volume": np.full(count, 1000),
            "quote_volume": close * 1000,
        },
        index=index,
    )


class _Repo:
    def __init__(self, cached, refreshed=None):
        self.cached = cached
        self.refreshed = refreshed if refreshed is not None else cached
        self.calls = []

    async def get(self, symbol, timeframe, *, limit, refresh):
        assert limit == DAILY_FETCH_BARS
        self.calls.append(refresh)
        return self.refreshed if refresh else self.cached


async def test_daily_snapshot_excludes_forming_bar_before_indicator_calculation():
    raw = _raw()
    original = await load_daily_snapshot("BTCUSDT", repository=_Repo(raw))
    raw.iloc[-1, raw.columns.get_loc("close")] = 1e9
    changed = await load_daily_snapshot("BTCUSDT", repository=_Repo(raw))
    pd.testing.assert_frame_equal(original, changed)
    assert changed.index[-1] == latest_daily_close()
    assert changed.attrs["history_bars"] == DAILY_HISTORY_BARS
    assert len(changed) == DAILY_HISTORY_BARS - 199


async def test_daily_snapshot_refreshes_stale_cache():
    repo = _Repo(_raw(end=latest_daily_close() - pd.Timedelta(days=1)), _raw())
    snapshot = await load_daily_snapshot("BTCUSDT", repository=repo)
    assert repo.calls == [False, True]
    assert snapshot.index[-1] == latest_daily_close()


async def test_daily_snapshot_fails_closed_if_exchange_still_stale():
    repo = _Repo(_raw(end=latest_daily_close() - pd.Timedelta(days=1)))
    with pytest.raises(ValueError, match="stale daily candles"):
        await load_daily_snapshot("BTCUSDT", repository=repo)


async def test_daily_snapshot_accepts_exact_warmup_and_rejects_one_bar_less():
    enough = _raw(count=DAILY_MIN_CLOSED_BARS + 1)
    snapshot = await load_daily_snapshot("BTCUSDT", repository=_Repo(enough))
    assert len(snapshot) == 60
    with pytest.raises(ValueError, match="insufficient_history"):
        await load_daily_snapshot("BTCUSDT", repository=_Repo(enough.iloc[1:]))


async def test_daily_snapshot_rejects_missing_or_duplicate_days():
    raw = _raw()
    with pytest.raises(ValueError, match="history contains gaps"):
        await load_daily_snapshot("BTCUSDT", repository=_Repo(raw.drop(raw.index[-4])))
    with pytest.raises(ValueError, match="increasing and unique"):
        await load_daily_snapshot("BTCUSDT", repository=_Repo(pd.concat([raw, raw.tail(1)])))


async def test_daily_snapshot_rejects_invalid_latest_indicator_input():
    raw = _raw()
    raw.iloc[-2, raw.columns.get_loc("close")] = np.nan
    with pytest.raises(ValueError, match="missing daily OHLCV"):
        await load_daily_snapshot("BTCUSDT", repository=_Repo(raw))


def test_cache_write_is_atomic_and_cleans_failed_temporary_file(tmp_path, monkeypatch):
    path = tmp_path / "BTCUSDT_1d.pkl"
    original = _raw()
    OHLCVRepository._write_cache(path, original)
    def _fail(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(pd.DataFrame, "to_pickle", _fail)
    with pytest.raises(OSError, match="disk full"):
        OHLCVRepository._write_cache(path, original * 2)
    pd.testing.assert_frame_equal(pd.read_pickle(path), original)
    assert list(tmp_path.iterdir()) == [path]


async def test_strategy_and_market_gate_use_identical_daily_snapshot(monkeypatch):
    from app.data import daily
    import app.data as data_module
    from app.config import get_settings
    from app.regime.btc_regime import compute_btc_regime_score
    from app.trading.strategy import ProfitStreamStrategy
    import app.trading.strategy as strategy_module
    from app.trading.autopilot import Autopilot
    import app.trading.autopilot as autopilot_module

    repo = _Repo(_raw())
    monkeypatch.setattr(data_module, "OHLCVRepository", lambda **kwargs: repo)
    monkeypatch.setattr(strategy_module, "OHLCVRepository", lambda **kwargs: repo)
    settings = get_settings()
    monkeypatch.setattr(autopilot_module, "get_settings", lambda: settings)
    strategy = ProfitStreamStrategy()
    snapshot = await strategy._candles("BTCUSDT", "1d", 320)
    gate = Autopilot()
    allowed, reason = await gate._market_gate()
    expected = compute_btc_regime_score(snapshot)
    assert gate._last_regime_score == expected.score
    assert allowed == (expected.score >= 0)
    assert str(expected.detail["ema200"]) in reason
    pd.testing.assert_frame_equal(
        snapshot, await daily.load_daily_snapshot("BTCUSDT", repository=repo)
    )


def test_tick_execution_only_becomes_true_after_actual_fill(tmp_path):
    from app.storage.db import Storage
    store = Storage(tmp_path / "audit.db")
    decision_id = store.record_tick_audit(
        mode="paper", symbol="BTCUSDT", timeframe="1d",
        action="BUY", score=100, executed=False, reason="score_pass",
        filters={"audit_version": 2, "runner_pass": True},
    )
    assert store.recent_tick_audit(1)[0]["executed"] == 0
    store.update_tick_execution(
        decision_id, submitted=True, filled=True,
        client_order_id="test-fill", outcome="FILLED",
    )
    import json
    row = store.recent_tick_audit(1)[0]
    assert row["executed"] == 1
    detail = json.loads(row["filters"])
    assert detail["runner_pass"] and detail["filled"]
    assert detail["client_order_id"] == "test-fill"

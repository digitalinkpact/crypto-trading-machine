import numpy as np
import pandas as pd

from app.ta import add_indicators


def _ohlcv(n: int = 250) -> pd.DataFrame:
    rng = np.random.default_rng(0)
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    high = close + rng.uniform(0.1, 1.0, n)
    low = close - rng.uniform(0.1, 1.0, n)
    open_ = close + rng.normal(0, 0.2, n)
    vol = rng.uniform(100, 1000, n)
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": vol},
        index=idx,
    )


def test_indicators_added():
    out = add_indicators(_ohlcv())
    for col in ("ema_20", "ema_50", "ema_200", "rsi_14", "macd", "atr_14"):
        assert col in out.columns
    # last row should be fully populated
    assert out[["ema_200", "rsi_14", "atr_14"]].dropna().shape[0] > 0

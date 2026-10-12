from __future__ import annotations

from app.trading.performance_analytics import build_performance_snapshot


def test_build_performance_snapshot_reports_execution_not_wins(monkeypatch):
    monkeypatch.setattr(
        "app.trading.performance_analytics.storage.closed_trades",
        lambda limit=5000: [
            {
                "mode": "paper",
                "pnl": 10.0,
                "pnl_pct": 5.0,
                "symbol": "BTCUSDT",
                "agents": "[\"mean_reversion\"]",
                "exit_ts": "2026-08-01T00:00:00+00:00",
            },
            {
                "mode": "paper",
                "pnl": -5.0,
                "pnl_pct": -2.5,
                "symbol": "ETHUSDT",
                "agents": "[\"momentum\"]",
                "exit_ts": "2026-08-02T00:00:00+00:00",
            },
        ],
    )
    monkeypatch.setattr(
        "app.trading.performance_analytics.storage.equity_curve",
        lambda limit=3000: [
            {"mode": "paper", "ts": "2026-08-01T00:00:00+00:00", "total_usdt": 100.0},
            {"mode": "paper", "ts": "2026-08-02T00:00:00+00:00", "total_usdt": 105.0},
        ],
    )
    monkeypatch.setattr(
        "app.trading.performance_analytics.storage.recent_tick_audit",
        lambda limit=5000: [
            {"mode": "paper", "score": 95, "timeframe": "1d", "executed": 1},
            {"mode": "paper", "score": 95, "timeframe": "1d", "executed": 0},
            {"mode": "paper", "score": 82, "timeframe": "4h", "executed": 1},
        ],
    )
    kv_writes = {}
    monkeypatch.setattr(
        "app.trading.performance_analytics.storage.kv_set",
        lambda key, value: kv_writes.__setitem__(key, value),
    )

    snapshot = build_performance_snapshot(mode="paper", lookback_days=180)

    assert snapshot["trade_count"] == 2
    assert snapshot["win_rate"] == 0.5
    assert snapshot["best_score_ranges"]["90-100"]["count"] == 2
    assert snapshot["best_score_ranges"]["90-100"]["executed_count"] == 1
    assert snapshot["best_score_ranges"]["90-100"]["execution_rate"] == 0.5
    assert "performance_analytics:paper" in kv_writes


def test_performance_snapshot_win_rate_is_position_level(monkeypatch):
    # ONE winning position scaled out across 3 TP/trailing rows + ONE losing
    # position = 50% win rate, NOT 75% (3 winning rows of 4 total rows). This
    # is the row-vs-position inflation the fix removes.
    monkeypatch.setattr(
        "app.trading.performance_analytics.storage.closed_trades",
        lambda limit=5000: [
            {"mode": "paper", "pnl": 5.0, "pnl_pct": 5.0, "symbol": "BTCUSDT",
             "entry_ts": "2026-08-01T00:00:00+00:00", "exit_ts": "2026-08-01T05:00:00+00:00", "agents": "[]"},
            {"mode": "paper", "pnl": 3.0, "pnl_pct": 3.0, "symbol": "BTCUSDT",
             "entry_ts": "2026-08-01T00:00:00+00:00", "exit_ts": "2026-08-01T06:00:00+00:00", "agents": "[]"},
            {"mode": "paper", "pnl": 2.0, "pnl_pct": 2.0, "symbol": "BTCUSDT",
             "entry_ts": "2026-08-01T00:00:00+00:00", "exit_ts": "2026-08-01T07:00:00+00:00", "agents": "[]"},
            {"mode": "paper", "pnl": -4.0, "pnl_pct": -4.0, "symbol": "ETHUSDT",
             "entry_ts": "2026-08-02T00:00:00+00:00", "exit_ts": "2026-08-02T03:00:00+00:00", "agents": "[]"},
        ],
    )
    monkeypatch.setattr("app.trading.performance_analytics.storage.equity_curve", lambda limit=3000: [])
    monkeypatch.setattr("app.trading.performance_analytics.storage.recent_tick_audit", lambda limit=5000: [])
    monkeypatch.setattr("app.trading.performance_analytics.storage.kv_set", lambda key, value: None)

    snapshot = build_performance_snapshot(mode="paper", lookback_days=180)

    assert snapshot["trade_count"] == 2       # 2 positions, not 4 rows
    assert snapshot["win_rate"] == 0.5        # 1 winning position / 2 positions
    assert snapshot["closed_row_count"] == 4  # transparency: underlying rows

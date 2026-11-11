"""Offline candle-to-fill integration with real strategy, risk, and persistence."""
from __future__ import annotations

from decimal import Decimal
import importlib
import json

import numpy as np
import pandas as pd
import pytest


async def test_daily_buy_flows_through_risk_to_paper_fill_and_linked_audit(monkeypatch, tmp_path):
    from app.config import Settings, DAILY_FETCH_BARS
    from app.data.daily import latest_daily_close
    from app.data.ohlcv import OHLCVRepository
    from app.exchange.filters import SymbolFilters
    from app.storage.db import Storage
    from app.trading.paper import PaperExchange
    from app.trading.autopilot import Autopilot
    from app.agents import runner

    settings = Settings(_env_file=None).model_copy(update={
        "data_cache_dir": tmp_path,
        "paper_trading": True, "dry_run": True, "live_mode": False,
        "ml_gate_enabled": False, "ml_learning_enabled": False,
        "derivatives_data_enabled": False, "live_price_enabled": False,
        "dynamic_threshold_enabled": False, "aggressive_mode_enabled": False,
    })
    store = Storage(tmp_path / "integration.db")
    modules = [
        "app.agents.runner", "app.trading.strategy", "app.trading.autopilot",
        "app.trading.paper", "app.trading.risk", "app.trading.risk_manager",
        "app.trading.audit", "app.exchange.orderbook",
    ]
    for name in modules:
        module = importlib.import_module(name)
        if hasattr(module, "storage"):
            monkeypatch.setattr(module, "storage", store)
        if hasattr(module, "get_settings"):
            monkeypatch.setattr(module, "get_settings", lambda: settings)

    index = pd.date_range(
        end=latest_daily_close() + pd.Timedelta(days=1),
        periods=DAILY_FETCH_BARS, freq="D",
    )
    universe = ["AAAUSDT", *[f"ALT{i}USDT" for i in range(15)]]

    class _PublicDataOnly:
        async def klines(self, symbol, timeframe, limit=500):
            close = np.linspace(80, 150, DAILY_FETCH_BARS)
            if symbol == "AAAUSDT":
                close[-6:-1] = [150, 148, 145, 140, 138]
            return pd.DataFrame(
                {
                    "open": close, "high": close + 1, "low": close * 0.9,
                    "close": close, "volume": np.full(len(close), 1000),
                    "quote_volume": close * 1000,
                }, index=index,
            )

        async def ticker_price(self, symbol):
            return Decimal("138") if symbol == "AAAUSDT" else Decimal("150")

        async def order_book(self, symbol, limit=5):
            price = await self.ticker_price(symbol)
            return {
                "bids": [[str(price - Decimal(".01")), "1000"]],
                "asks": [[str(price + Decimal(".01")), "1000"]],
            }

        async def place_order(self, **kwargs):
            raise AssertionError("Offline integration must never submit a real order")

    client = _PublicDataOnly()
    repo = OHLCVRepository(client=client, cache_dir=tmp_path)
    import app.data as data_module
    import app.exchange.symbol_source as source_module
    import app.trading.strategy as strategy_module
    import app.trading.autopilot as autopilot_module
    import app.trading.paper as paper_module
    import app.exchange.orderbook as orderbook_module
    import app.trading.portfolio as portfolio_module

    async def _symbols():
        return universe
    monkeypatch.setattr(runner, "get_symbols", _symbols)
    monkeypatch.setattr(source_module, "get_symbols", _symbols)
    monkeypatch.setattr(data_module, "OHLCVRepository", lambda **kwargs: repo)
    monkeypatch.setattr(strategy_module, "OHLCVRepository", lambda **kwargs: repo)
    monkeypatch.setattr(strategy_module, "BinanceUSClient", lambda: client)
    monkeypatch.setattr(orderbook_module, "BinanceUSClient", lambda: client)
    paper = PaperExchange(live_client=client)
    monkeypatch.setattr(autopilot_module, "paper_exchange", paper)
    monkeypatch.setattr(paper_module, "paper_exchange", paper)
    async def _portfolio(*, mode):
        assert mode == "paper"
        return await paper.snapshot()
    monkeypatch.setattr(autopilot_module, "portfolio_snapshot", _portfolio)
    monkeypatch.setattr(portfolio_module, "portfolio_snapshot", _portfolio)

    filters = SymbolFilters()
    filters._loaded = True
    filters._info = {
        symbol: {
            "status": "TRADING", "step_size": Decimal(".0001"),
            "min_qty": Decimal(".0001"), "min_notional": Decimal("1"),
        } for symbol in universe
    }
    monkeypatch.setattr(autopilot_module, "filters", filters)
    paper.ensure_seeded(Decimal("1000"))
    signals = await runner.run_all_agents()
    assert set(signals) == {"AAAUSDT"}
    signal = signals["AAAUSDT"]
    assert signal.quality_score == 100
    assert signal.timeframe.value == "1d"
    assert signal.decision_id is not None
    before = next(row for row in store.recent_tick_audit(20) if row["id"] == signal.decision_id)
    assert before["executed"] == 0
    ap = Autopilot()
    ap.state.mode = "paper"
    ap.state.running = True
    ap.state.starting_balance_usdt = Decimal("1000")
    await ap._execute(signals)
    position = store.get_position("AAAUSDT")
    assert position is not None and position["qty"] > 0
    assert store.paper_balance_get("AAA") == position["qty"]
    assert 0 < store.paper_balance_get("USDT") < 1000
    after = next(row for row in store.recent_tick_audit(20) if row["id"] == signal.decision_id)
    assert after["executed"] == 1
    detail = json.loads(after["filters"])
    assert detail["runner_pass"] and detail["submitted"] and detail["filled"]
    assert detail["client_order_id"].startswith("paper-")


@pytest.mark.parametrize("price,reason,remaining", [
    ("100", "stop_loss", False),
    ("160", "take_profit_1", True),
])
async def test_daily_buy_then_independent_paper_exit(monkeypatch, tmp_path, price, reason, remaining):
    from unittest.mock import AsyncMock
    import app.trading.autopilot as module
    from app.trading.autopilot import Autopilot

    await test_daily_buy_flows_through_risk_to_paper_fill_and_linked_audit(monkeypatch, tmp_path)
    monkeypatch.setattr(module.paper_exchange._live, "ticker_price", AsyncMock(return_value=Decimal(price)))
    autopilot = Autopilot()
    outcome = await autopilot._run_risk_gates()
    assert outcome["success"]
    assert module.storage.closed_trades()[0]["exit_reason"] == reason
    assert bool(module.storage.all_positions()) is remaining
    orders = module.storage.recent_orders()
    assert {order["side"] for order in orders} == {"BUY", "SELL"}
    assert all(order["mode"] == "paper" and order["client_order_id"].startswith("paper-") for order in orders)


async def test_paper_fill_rolls_back_all_writes_on_order_failure(monkeypatch, tmp_path):
    import sqlite3
    import app.trading.autopilot as module
    from app.exchange import OrderSide

    await test_daily_buy_flows_through_risk_to_paper_fill_and_linked_audit(monkeypatch, tmp_path)
    balances = module.storage.paper_balances()
    positions = module.storage.all_positions()
    with module.storage._conn() as connection:
        connection.execute("CREATE TRIGGER reject_order BEFORE INSERT ON orders BEGIN SELECT RAISE(ABORT, 'write failed'); END")
    with pytest.raises(sqlite3.IntegrityError, match="write failed"):
        await module.paper_exchange.place_order(symbol="ALT0USDT", side=OrderSide.BUY, quantity=Decimal("1"))
    assert module.storage.paper_balances() == balances
    assert module.storage.all_positions() == positions


async def test_paper_buy_rejects_insufficient_balance_without_writes(monkeypatch, tmp_path):
    import app.trading.autopilot as module
    from app.exchange import OrderSide

    await test_daily_buy_flows_through_risk_to_paper_fill_and_linked_audit(monkeypatch, tmp_path)
    balances = module.storage.paper_balances()
    with pytest.raises(RuntimeError, match="insufficient"):
        await module.paper_exchange.place_order(symbol="ALT0USDT", side=OrderSide.BUY, quantity=Decimal("10000"))
    assert module.storage.paper_balances() == balances


async def test_repeated_simulation_client_id_does_not_double_fill(monkeypatch, tmp_path):
    import app.trading.autopilot as module
    from app.exchange import OrderSide

    await test_daily_buy_flows_through_risk_to_paper_fill_and_linked_audit(monkeypatch, tmp_path)
    arguments = dict(symbol="ALT0USDT", side=OrderSide.BUY, quantity=Decimal("1"), client_order_id="paper-idempotent")
    await module.paper_exchange.place_order(**arguments)
    balances = module.storage.paper_balances()
    await module.paper_exchange.place_order(**arguments)
    assert module.storage.paper_balances() == balances
    assert len([order for order in module.storage.recent_orders() if order["client_order_id"] == "paper-idempotent"]) == 1

from __future__ import annotations

from decimal import Decimal

import app.trading.autopilot as autopilot_module
from app.trading.autopilot import Autopilot


def _snapshot(total: str):
    async def result(*_args, **_kwargs):
        return {"total_usdt": Decimal(total), "usdt_cash": Decimal(total)}
    return result


async def test_legacy_drawdown_halt_is_persisted_and_blocks_entries(monkeypatch):
    autopilot = Autopilot()
    autopilot.state.mode = "live"
    autopilot.state.starting_balance_usdt = Decimal("100")
    autopilot.state.last_error = "DRAWDOWN BREAKER TRIPPED at -20.0% — new BUYs halted"
    kv_state: dict = {}

    monkeypatch.setattr(autopilot_module.storage, "kv_get", lambda key: kv_state.get(key))
    monkeypatch.setattr(autopilot_module.storage, "kv_set", lambda key, value: kv_state.__setitem__(key, value))
    monkeypatch.setattr(autopilot_module, "portfolio_snapshot", _snapshot("95"))

    assert await autopilot._check_circuit_breaker() is True
    assert kv_state["drawdown_halt"]["active"] is True
    assert kv_state["drawdown_halt"]["legacy"] is True


async def test_drawdown_recovery_requires_reason_and_rebaselines(monkeypatch):
    autopilot = Autopilot()
    autopilot.state.mode = "live"
    autopilot.state.starting_balance_usdt = Decimal("100")
    kv_state = {
        "accounting_status": {"verified": True, "source": "authoritative_history"},
        "drawdown_halt": {
            "active": True,
            "drawdown_pct": -0.2,
            "drawdown_limit_pct": 0.15,
        },
    }
    audits: list[dict] = []

    monkeypatch.setattr(autopilot_module.storage, "kv_get", lambda key: kv_state.get(key))
    monkeypatch.setattr(autopilot_module.storage, "kv_set", lambda key, value: kv_state.__setitem__(key, value))
    monkeypatch.setattr(autopilot_module, "portfolio_snapshot", _snapshot("90"))
    monkeypatch.setattr(autopilot_module.trade_audit_logger, "log_event", lambda **kwargs: audits.append(kwargs))
    monkeypatch.setattr(autopilot, "_save", lambda: None)

    await autopilot.resume_after_drawdown_halt(reason="Reviewed corrected USD-inclusive equity")

    assert autopilot.state.starting_balance_usdt == Decimal("90")
    assert kv_state["drawdown_halt"]["active"] is False
    assert kv_state["drawdown_halt"]["operator_action"] == "explicit_rebaseline"
    assert audits[0]["final_outcome"] == "operator_recovery"


async def test_drawdown_recovery_requires_verified_accounting(monkeypatch):
    autopilot = Autopilot()
    kv_state = {"drawdown_halt": {"active": True}}

    monkeypatch.setattr(autopilot_module.storage, "kv_get", lambda key: kv_state.get(key))

    try:
        await autopilot.resume_after_drawdown_halt(reason="operator review")
    except RuntimeError as exc:
        assert "verified authoritative accounting" in str(exc)
    else:
        raise AssertionError("unverified accounting must block drawdown recovery")


async def test_circuit_breaker_exception_persistently_blocks_new_entries(monkeypatch):
    autopilot = Autopilot()
    autopilot.state.running = True
    autopilot.state.mode = "live"
    autopilot.state.starting_balance_usdt = Decimal("100")
    kv_state: dict = {}

    class _Settings:
        paper_trading = False
        llm_in_trading_loop = False
        emergency_halt_enabled = True

    async def _broken_breaker(self):
        raise RuntimeError("portfolio unavailable")

    async def _execute(self, _signals, *, allow_buys):
        assert allow_buys is False
        assert "circuit_breaker_check_failed" in self._entry_block_reason()

    monkeypatch.setattr(autopilot_module, "get_settings", lambda: _Settings())
    monkeypatch.setattr(autopilot_module.storage, "kv_get", lambda key: kv_state.get(key))
    monkeypatch.setattr(autopilot_module.storage, "kv_set", lambda key, value: kv_state.__setitem__(key, value))
    monkeypatch.setattr(autopilot_module.storage, "try_acquire_lock", lambda *_a, **_k: True)
    monkeypatch.setattr(autopilot_module.storage, "release_lock", lambda *_a, **_k: None)
    monkeypatch.setattr(autopilot_module.filters, "load", lambda: __import__("asyncio").sleep(0))
    monkeypatch.setattr(autopilot_module.filters, "_loaded", True)
    monkeypatch.setattr(Autopilot, "_check_circuit_breaker", _broken_breaker)
    monkeypatch.setattr(autopilot_module, "run_all_agents", lambda **_k: __import__("asyncio").sleep(0, result={}))
    monkeypatch.setattr(Autopilot, "_execute", _execute)

    await autopilot.tick()

    assert kv_state["circuit_breaker_check_failed"]["active"] is True
    assert "portfolio unavailable" in autopilot.state.last_error

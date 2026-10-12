"""Regression tests: a working API key must not be reported as "exchange down".

Two defects fixed on branch fix/buy-path-diagnostics:
  1. BinanceUSClient built the connector with timeout=None (its default), so a
     single hung socket blocked account() forever and starved the health loop's
     retry/streak logic — manifesting as `binance_alive unhealthy for N
     consecutive checks` even with a valid key.
  2. The watchdog measured exchange reachability with the SIGNED /account call.
     -1021 clock skew, -1003 rate limits, or a key/IP issue all flipped
     binance_alive=False and tripped the trading halt although Binance.US was
     up. Reachability now uses the unsigned /ping; the signed check is tracked
     separately as binance_account_ok.

No network is used; the connector is patched at the module boundary.
"""
from __future__ import annotations

import pytest

import app.exchange.client as client_module
from app.config import Settings


# ───────────────────────── 1. bounded HTTP timeout ──────────────────────────

def test_spot_client_is_constructed_with_bounded_timeout(monkeypatch):
    captured: dict = {}

    class _FakeSpot:
        def __init__(self, api_key=None, api_secret=None, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(client_module, "Spot", _FakeSpot)
    client_module.BinanceUSClient(settings=Settings(_env_file=None))

    assert "timeout" in captured, "connector must not run with its default timeout=None"
    assert captured["timeout"] is not None
    assert 1.0 <= float(captured["timeout"]) <= 120.0


def test_spot_client_timeout_is_configurable(monkeypatch):
    captured: dict = {}

    class _FakeSpot:
        def __init__(self, api_key=None, api_secret=None, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(client_module, "Spot", _FakeSpot)
    client_module.BinanceUSClient(settings=Settings(_env_file=None, binance_http_timeout_seconds=7.5))
    assert captured["timeout"] == 7.5


def test_config_rejects_unbounded_timeout():
    with pytest.raises(Exception):
        Settings(_env_file=None, binance_http_timeout_seconds=0.0)


# ───────────────────────── 2. unsigned ping for reachability ────────────────

@pytest.mark.asyncio
async def test_ping_uses_unsigned_endpoint(monkeypatch):
    calls: list[str] = []

    class _FakeSpot:
        def __init__(self, *a, **k): pass
        def ping(self):
            calls.append("ping"); return {}
        def account(self):
            calls.append("account"); raise AssertionError("ping() must not call the signed /account")

    monkeypatch.setattr(client_module, "Spot", _FakeSpot)
    c = client_module.BinanceUSClient(settings=Settings(_env_file=None))
    assert await c.ping() is True
    assert calls == ["ping"]


@pytest.mark.asyncio
async def test_watchdog_reachability_uses_ping_not_account(monkeypatch):
    """Patch BinanceUSClient inside the watchdog and assert the reachability
    decision (binance_alive) is driven by ping(), while account() failures
    only affect binance_account_ok."""
    import app.trading.watchdog as watchdog
    from app.trading import health

    class _ReachableBadKey:
        async def ping(self): return True
        async def account(self): raise RuntimeError("APIError(code=-2015): Invalid API-key, IP, or permissions")

    monkeypatch.setattr(watchdog, "BinanceUSClient", lambda: _ReachableBadKey())

    # Drive the two _retry_async calls exactly as the loop does, with retries
    # collapsed so the test is instant.
    async def _no_sleep(*_a, **_k): return None
    monkeypatch.setattr(health.asyncio, "sleep", _no_sleep)

    ok, _ = await health._retry_async(lambda: watchdog.BinanceUSClient().ping(), attempts=3, base_delay=0, label="t")
    acct_ok, _ = await health._retry_async(lambda: watchdog.BinanceUSClient().account(), attempts=2, base_delay=0, label="t")

    assert ok is True          # reachable → must NOT count toward the halt streak
    assert acct_ok is False    # bad key is still surfaced, separately


@pytest.mark.asyncio
async def test_watchdog_network_outage_still_flags_unreachable(monkeypatch):
    import app.trading.watchdog as watchdog
    from app.trading import health

    class _Down:
        async def ping(self): raise TimeoutError("connect timeout")
        async def account(self): raise TimeoutError("connect timeout")

    monkeypatch.setattr(watchdog, "BinanceUSClient", lambda: _Down())

    async def _no_sleep(*_a, **_k): return None
    monkeypatch.setattr(health.asyncio, "sleep", _no_sleep)

    ok, _ = await health._retry_async(lambda: watchdog.BinanceUSClient().ping(), attempts=3, base_delay=0, label="t")
    assert ok is False         # a real outage must still trip binance_alive


# ───────────────────────── 3. status surfaces the distinction ───────────────

@pytest.mark.asyncio
async def test_status_exposes_reachable_vs_account_ok(monkeypatch):
    import app.api.routes as routes

    kv = {
        "health_status": {
            "timestamp": "2026-10-12T22:00:00+00:00",
            "binance_alive": True,
            "binance_account_ok": False,
            "binance_latency_s": 0.12,
            "risk_loop_alive": True,
            "risk_loop_age_s": 4.2,
            "trade_loop_alive": True,
        },
        "emergency_halt": {"active": True, "level": "new_entries_blocked",
                           "reason": "binance_alive unhealthy for 3 consecutive checks"},
    }
    monkeypatch.setattr(routes.storage, "kv_get", lambda key, default=None: kv.get(key, default), raising=True)
    monkeypatch.setattr(routes.storage, "equity_curve", lambda limit=1: [], raising=True)
    monkeypatch.setattr(routes, "credentials_present", lambda: True, raising=True)

    out = await routes.autopilot_status()
    eh = out["exchange_health"]
    assert eh["binance_reachable"] is True
    assert eh["binance_account_ok"] is False
    assert eh["risk_loop_alive"] is True
    assert out["emergency_halt"]["active"] is True

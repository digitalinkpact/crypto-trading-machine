"""Tests for the universe-level hard blocklist (app/exchange/symbol_source.py).

`Settings.blocked_symbols` was silently dropped from config.py in an earlier
refactor while `_apply_blocklist` kept reading it via `getattr(s,
'blocked_symbols', ())` — a missing attribute always fell back to an empty
tuple, defanging the blocklist without raising or logging anything. These
tests guard the field's existence and the filtering behavior itself.
"""
from __future__ import annotations

import pytest

from app.config import Settings
from app.exchange import symbol_source
from app.exchange.symbol_source import _apply_blocklist


def test_blocked_symbols_field_exists_and_is_populated_from_evidence():
    # The field must exist (it was silently dropped once before) AND now carry
    # the evidence-based defaults: proven live losers + structurally thin books.
    s = Settings(_env_file=None)
    assert isinstance(s.blocked_symbols, tuple)
    for sym in ("ZECUSDT", "PUMPUSDT", "HYPEUSDT"):
        assert sym in s.blocked_symbols


def test_apply_blocklist_filters_blocked_symbols_case_insensitively():
    symbols = ["BTCUSDT", "PROMUSDT", "ETHUSDT", "hypeusdt"]
    out = _apply_blocklist(symbols, ("PROMUSDT", "HYPEUSDT"))
    assert out == ["BTCUSDT", "ETHUSDT"]


def test_apply_blocklist_noop_when_empty():
    symbols = ["BTCUSDT", "ETHUSDT"]
    assert _apply_blocklist(symbols, ()) == symbols


@pytest.mark.asyncio
async def test_get_symbols_excludes_blocked_even_when_top_ranked(monkeypatch):
    # A blocked coin ranked #1 by 24h volume must still never reach the output
    # of get_symbols(), which is what the autopilot iterates over.
    s = Settings(_env_file=None)
    assert "ZECUSDT" in s.blocked_symbols  # precondition: real evidence-based list
    monkeypatch.setattr(symbol_source, "get_settings", lambda: s)

    async def _fake_liquid():
        # ZEC/HYPE deliberately placed first (highest volume rank).
        return ["ZECUSDT", "BTCUSDT", "HYPEUSDT", "ETHUSDT", "SOLUSDT"]

    monkeypatch.setattr(symbol_source, "fetch_liquid_universe", _fake_liquid)

    out = await symbol_source.get_symbols()
    assert "ZECUSDT" not in out
    assert "HYPEUSDT" not in out
    assert out == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

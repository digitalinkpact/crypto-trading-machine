"""Tests for the universe-level hard blocklist (app/exchange/symbol_source.py).

`Settings.blocked_symbols` was silently dropped from config.py in an earlier
refactor while `_apply_blocklist` kept reading it via `getattr(s,
'blocked_symbols', ())` — a missing attribute always fell back to an empty
tuple, defanging the blocklist without raising or logging anything. These
tests guard the field's existence and the filtering behavior itself.
"""
from __future__ import annotations

from app.config import Settings
from app.exchange.symbol_source import _apply_blocklist


def test_blocked_symbols_field_exists_and_defaults_empty():
    s = Settings(_env_file=None)
    assert s.blocked_symbols == ()


def test_apply_blocklist_filters_blocked_symbols_case_insensitively():
    symbols = ["BTCUSDT", "PROMUSDT", "ETHUSDT", "hypeusdt"]
    out = _apply_blocklist(symbols, ("PROMUSDT", "HYPEUSDT"))
    assert out == ["BTCUSDT", "ETHUSDT"]


def test_apply_blocklist_noop_when_empty():
    symbols = ["BTCUSDT", "ETHUSDT"]
    assert _apply_blocklist(symbols, ()) == symbols

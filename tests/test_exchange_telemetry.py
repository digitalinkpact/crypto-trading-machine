from __future__ import annotations

import asyncio

import pytest

from app.exchange.telemetry import ExchangeTelemetry


def test_telemetry_records_percentiles_and_operation_counts():
    metrics = ExchangeTelemetry(max_samples=10)
    metrics.record("ticker_price", 0.01)
    metrics.record("ticker_price", 0.03)
    metrics.record("order_book", 0.02)
    metrics.record_retry()

    snapshot = metrics.snapshot()
    assert snapshot["requests"] == 3
    assert snapshot["successful"] == 3
    assert snapshot["retries"] == 1
    assert snapshot["latency_seconds"]["p50"] == 0.02
    assert snapshot["latency_seconds"]["p95"] == 0.02
    assert snapshot["by_operation"]["ticker_price"] == {
        "requests": 2, "successful": 2, "failed": 0,
    }


def test_telemetry_classifies_timeout_and_rate_limit():
    metrics = ExchangeTelemetry()
    metrics.record("account", 0.1, asyncio.TimeoutError())

    class RateLimitError(Exception):
        response = type("Response", (), {"status_code": 429})()

    metrics.record("order_book", 0.2, RateLimitError("rate limited"))
    snapshot = metrics.snapshot()
    assert snapshot["failed"] == 2
    assert snapshot["timeouts"] == 1
    assert snapshot["rate_limits"] == 1
    assert snapshot["exchange_errors"] == 0


@pytest.mark.asyncio
async def test_api_call_records_success_and_failure(monkeypatch):
    from app.exchange.client import BinanceUSClient

    client = BinanceUSClient.__new__(BinanceUSClient)
    observed = ExchangeTelemetry()
    monkeypatch.setattr("app.exchange.client.exchange_telemetry", observed)

    assert await client._api_call("test", lambda: 7) == 7
    with pytest.raises(RuntimeError):
        await client._api_call("test", lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    snapshot = observed.snapshot()
    assert snapshot["requests"] == 2
    assert snapshot["successful"] == 1
    assert snapshot["failed"] == 1
    assert snapshot["by_operation"]["test"]["failed"] == 1

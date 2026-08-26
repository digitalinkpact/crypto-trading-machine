"""In-process, secret-free telemetry for exchange API calls."""
from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from statistics import mean
from typing import Any


class ExchangeTelemetry:
    """Bounded request metrics that never influence trading decisions."""

    def __init__(self, max_samples: int = 2000) -> None:
        self._lock = threading.Lock()
        self._max_samples = max_samples
        self._latencies: deque[float] = deque(maxlen=max_samples)
        self._counts: dict[str, int] = {
            "requests": 0,
            "successful": 0,
            "failed": 0,
            "timeouts": 0,
            "retries": 0,
            "rate_limits": 0,
            "exchange_errors": 0,
        }
        self._by_operation: dict[str, dict[str, int]] = {}
        self._stage_latencies: dict[str, deque[float]] = {}

    def record(self, operation: str, elapsed: float, error: BaseException | None = None) -> None:
        with self._lock:
            self._counts["requests"] += 1
            self._latencies.append(max(0.0, elapsed))
            bucket = self._by_operation.setdefault(operation, {"requests": 0, "successful": 0, "failed": 0})
            bucket["requests"] += 1
            if error is None:
                self._counts["successful"] += 1
                bucket["successful"] += 1
                return
            self._counts["failed"] += 1
            bucket["failed"] += 1
            if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
                self._counts["timeouts"] += 1
            status = getattr(getattr(error, "response", None), "status_code", None)
            message = str(error)
            if status == 429 or "429" in message or "rate limit" in message.lower():
                self._counts["rate_limits"] += 1
            if status is not None and status != 429:
                self._counts["exchange_errors"] += 1

    def record_retry(self) -> None:
        with self._lock:
            self._counts["retries"] += 1

    def record_stage(self, stage: str, elapsed: float) -> None:
        with self._lock:
            self._stage_latencies.setdefault(stage, deque(maxlen=self._max_samples)).append(max(0.0, elapsed))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            values = sorted(self._latencies)
            def percentile(fraction: float) -> float | None:
                if not values:
                    return None
                index = min(len(values) - 1, int((len(values) - 1) * fraction))
                return round(values[index], 6)
            return {
                **self._counts,
                "latency_seconds": {
                    "p50": percentile(0.50),
                    "p95": percentile(0.95),
                    "p99": percentile(0.99),
                    "mean": round(mean(values), 6) if values else None,
                },
                "by_operation": {key: dict(value) for key, value in self._by_operation.items()},
                "stages": {
                    stage: {
                        "p50": percentile_from(values),
                        "p95": percentile_from(values, 0.95),
                        "p99": percentile_from(values, 0.99),
                        "samples": len(values),
                    }
                    for stage, samples in self._stage_latencies.items()
                    for values in [sorted(samples)]
                },
            }


def percentile_from(values: list[float], fraction: float = 0.50) -> float | None:
    if not values:
        return None
    index = min(len(values) - 1, int((len(values) - 1) * fraction))
    return round(values[index], 6)

exchange_telemetry = ExchangeTelemetry()


def timed() -> float:
    return time.perf_counter()

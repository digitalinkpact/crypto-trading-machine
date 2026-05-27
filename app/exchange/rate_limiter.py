import asyncio
import time
from collections import deque
from typing import Callable, Any, Awaitable
from app.config import get_settings
from app.logging_setup import get_logger

log = get_logger(__name__)

class RateLimiter:
    def __init__(self, rate: int, per: float, weight_limit: int = 1200):
        self.rate = rate  # max requests
        self.per = per    # per seconds
        self.tokens = rate
        self.updated = time.monotonic()
        self.lock = asyncio.Lock()
        self.weight_limit = weight_limit
        self.weight_used = 0
        self.weight_reset = time.monotonic()
        self.queue = deque()

    async def acquire(self, weight: int = 1):
        async with self.lock:
            now = time.monotonic()
            elapsed = now - self.updated
            refill = int(elapsed * (self.rate / self.per))
            if refill > 0:
                self.tokens = min(self.rate, self.tokens + refill)
                self.updated = now
            while self.tokens < weight or (self.weight_used + weight > self.weight_limit):
                await asyncio.sleep(0.1)
                now = time.monotonic()
                elapsed = now - self.updated
                refill = int(elapsed * (self.rate / self.per))
                if refill > 0:
                    self.tokens = min(self.rate, self.tokens + refill)
                    self.updated = now
            self.tokens -= weight
            self.weight_used += weight

    async def reset_weight(self):
        self.weight_used = 0
        self.weight_reset = time.monotonic()

    async def run_with_retries(self, func: Callable[..., Awaitable[Any]], *args, weight: int = 1, **kwargs):
        s = get_settings()
        attempts = getattr(s, 'api_retry_attempts', 3)
        backoff_base = getattr(s, 'api_retry_backoff_base', 2)
        for attempt in range(attempts):
            await self.acquire(weight)
            try:
                return await func(*args, **kwargs)
            except Exception as exc:
                if hasattr(exc, 'response') and getattr(exc.response, 'status_code', None) == 429:
                    delay = backoff_base ** attempt
                    log.warning(f"Binance.US rate limit hit (429). Backing off {delay}s (attempt {attempt+1}/{attempts})")
                    await asyncio.sleep(delay)
                    continue
                raise
        raise RuntimeError("API call failed after retries")

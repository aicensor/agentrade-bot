"""
Agentrade Engine v2 — Per-Key Rate Limiter

Token bucket rate limiter. Each user's API key gets its own bucket.
Prevents hitting exchange rate limits even under heavy load.

Solves Problem #2A: prevents thundering herd when 100 users
all need SL updates on the same price tick.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque

import structlog

logger = structlog.get_logger(__name__)


class RateLimiter:
    """
    Sliding window rate limiter per API key.

    Exchange limits (approximate):
      Bybit:        10 req/s   (120/min) per UID
      Binance:      10 req/s   (1200/min weight-based)
      OKX:          20 req/2s  (600/min)
      Hyperliquid:  1200/min
      MEXC:         20 req/2s

    Usage:
        limiter = RateLimiter(max_per_second=10)
        await limiter.acquire()  # blocks if at limit
        # ... make API call ...
    """

    def __init__(
        self,
        max_per_second: float = 8.0,      # Stay below exchange limit (80% safety)
        max_per_minute: float = 100.0,     # Hard cap per minute
        burst_size: int = 5,               # Allow short bursts
    ) -> None:
        self.max_per_second = max_per_second
        self.max_per_minute = max_per_minute
        self.burst_size = burst_size
        self._calls: deque[float] = deque()
        self._lock = asyncio.Lock()
        self._total_calls: int = 0
        self._total_waits: int = 0

    async def acquire(self) -> float:
        """
        Wait until we can make a request. Returns wait time in seconds.
        Thread-safe via asyncio.Lock.
        """
        async with self._lock:
            now = time.monotonic()
            wait_time = 0.0

            # Clean old entries
            self._cleanup(now)

            # Check per-second limit
            recent_1s = sum(1 for t in self._calls if t > now - 1.0)
            if recent_1s >= self.max_per_second:
                wait_time = max(wait_time, 1.0 - (now - self._calls[-1]))

            # Check per-minute limit
            if len(self._calls) >= self.max_per_minute:
                oldest_in_window = self._calls[0]
                wait_time = max(wait_time, 60.0 - (now - oldest_in_window))

            if wait_time > 0:
                self._total_waits += 1
                logger.debug("rate_limit_wait", wait_seconds=round(wait_time, 3))
                await asyncio.sleep(wait_time)
                now = time.monotonic()

            self._calls.append(now)
            self._total_calls += 1
            return wait_time

    def _cleanup(self, now: float) -> None:
        """Remove entries older than 60 seconds"""
        while self._calls and self._calls[0] < now - 60.0:
            self._calls.popleft()

    @property
    def current_rate(self) -> float:
        """Current requests per second (last 5 seconds)"""
        now = time.monotonic()
        recent = sum(1 for t in self._calls if t > now - 5.0)
        return recent / 5.0

    @property
    def stats(self) -> dict:
        return {
            "total_calls": self._total_calls,
            "total_waits": self._total_waits,
            "current_rate_per_sec": round(self.current_rate, 2),
            "calls_last_minute": len(self._calls),
        }


class RateLimiterPool:
    """
    Manages one RateLimiter per API key.
    100 users = 100 independent rate limiters = 100x throughput.
    """

    # Per-exchange default limits (80% of actual to be safe)
    EXCHANGE_LIMITS: dict[str, dict] = {
        "bybit": {"max_per_second": 8, "max_per_minute": 100},
        "binance": {"max_per_second": 8, "max_per_minute": 400},
        "okx": {"max_per_second": 8, "max_per_minute": 480},
        "hyperliquid": {"max_per_second": 16, "max_per_minute": 960},
        "mexc": {"max_per_second": 8, "max_per_minute": 400},
    }

    def __init__(self) -> None:
        self._limiters: dict[str, RateLimiter] = {}

    def get(self, api_key: str, exchange: str = "bybit") -> RateLimiter:
        """Get or create a rate limiter for an API key"""
        if api_key not in self._limiters:
            limits = self.EXCHANGE_LIMITS.get(exchange, {"max_per_second": 8, "max_per_minute": 100})
            self._limiters[api_key] = RateLimiter(**limits)
        return self._limiters[api_key]

    @property
    def stats(self) -> dict:
        return {
            key: limiter.stats for key, limiter in self._limiters.items()
        }

"""
Agentrade Engine v2 — Backtest Data Provider

Fetches historical OHLCV data from exchanges via ccxt.
Supports multiple timeframes and exchanges.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import ccxt
import structlog

logger = structlog.get_logger(__name__)


@dataclass
class Candle:
    """Single OHLCV candle."""
    timestamp: float   # Unix ms
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def timestamp_sec(self) -> float:
        return self.timestamp / 1000


@dataclass
class PriceHistory:
    """Collection of candles with metadata."""
    symbol: str
    exchange: str
    timeframe: str
    candles: list[Candle] = field(default_factory=list)

    @property
    def start_time(self) -> float:
        return self.candles[0].timestamp if self.candles else 0

    @property
    def end_time(self) -> float:
        return self.candles[-1].timestamp if self.candles else 0

    @property
    def duration_hours(self) -> float:
        if len(self.candles) < 2:
            return 0
        return (self.end_time - self.start_time) / (1000 * 3600)

    @property
    def price_range(self) -> tuple[float, float]:
        if not self.candles:
            return (0, 0)
        lows = [c.low for c in self.candles]
        highs = [c.high for c in self.candles]
        return (min(lows), max(highs))


def fetch_ohlcv(
    symbol: str = "SOL/USDC:USDC",
    exchange_id: str = "hyperliquid",
    timeframe: str = "1m",
    limit: int = 1000,
    since: int | None = None,
) -> PriceHistory:
    """
    Fetch historical OHLCV data from an exchange.

    Args:
        symbol: Trading pair (e.g., "SOL/USDC:USDC")
        exchange_id: Exchange name (e.g., "hyperliquid", "bybit")
        timeframe: Candle interval ("1m", "5m", "15m", "1h", "4h", "1d")
        limit: Max candles to fetch (exchange may cap this)
        since: Start timestamp in ms (None = most recent)

    Returns:
        PriceHistory with candles
    """
    exchange_class = getattr(ccxt, exchange_id, None)
    if not exchange_class:
        raise ValueError(f"Unknown exchange: {exchange_id}")

    exchange = exchange_class({"options": {"defaultType": "swap"}})
    exchange.load_markets()

    logger.info(
        "fetching_ohlcv",
        symbol=symbol,
        exchange=exchange_id,
        timeframe=timeframe,
        limit=limit,
    )

    raw = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit, since=since)

    candles = [
        Candle(
            timestamp=row[0],
            open=row[1],
            high=row[2],
            low=row[3],
            close=row[4],
            volume=row[5],
        )
        for row in raw
    ]

    history = PriceHistory(
        symbol=symbol,
        exchange=exchange_id,
        timeframe=timeframe,
        candles=candles,
    )

    logger.info(
        "ohlcv_loaded",
        candles=len(candles),
        duration_hours=f"{history.duration_hours:.1f}",
        price_range=history.price_range,
    )

    return history


def fetch_ohlcv_range(
    symbol: str = "SOL/USDC:USDC",
    exchange_id: str = "hyperliquid",
    timeframe: str = "1m",
    start_ms: int | None = None,
    end_ms: int | None = None,
    max_candles: int = 5000,
) -> PriceHistory:
    """
    Fetch a range of OHLCV data, paginating if needed.

    Args:
        start_ms: Start timestamp in milliseconds
        end_ms: End timestamp in milliseconds (default: now)
        max_candles: Safety cap
    """
    exchange_class = getattr(ccxt, exchange_id, None)
    if not exchange_class:
        raise ValueError(f"Unknown exchange: {exchange_id}")

    exchange = exchange_class({"options": {"defaultType": "swap"}})
    exchange.load_markets()

    if end_ms is None:
        end_ms = int(time.time() * 1000)

    all_candles: list[Candle] = []
    cursor = start_ms
    batch_size = 500

    while len(all_candles) < max_candles:
        raw = exchange.fetch_ohlcv(
            symbol, timeframe=timeframe, limit=batch_size, since=cursor
        )
        if not raw:
            break

        for row in raw:
            if row[0] > end_ms:
                break
            all_candles.append(Candle(
                timestamp=row[0],
                open=row[1],
                high=row[2],
                low=row[3],
                close=row[4],
                volume=row[5],
            ))

        last_ts = raw[-1][0]
        if last_ts >= end_ms or last_ts == cursor:
            break
        cursor = last_ts + 1

        # Rate limit
        time.sleep(0.2)

    history = PriceHistory(
        symbol=symbol,
        exchange=exchange_id,
        timeframe=timeframe,
        candles=all_candles[:max_candles],
    )

    logger.info(
        "ohlcv_range_loaded",
        candles=len(history.candles),
        duration_hours=f"{history.duration_hours:.1f}",
    )

    return history


def candles_to_ticks(candles: list[Candle]) -> list[tuple[float, float]]:
    """
    Expand candles into simulated tick sequence: (timestamp, price).

    For each candle, generates 4 ticks: open → high → low → close
    (approximating intra-candle price movement).
    """
    ticks: list[tuple[float, float]] = []
    for c in candles:
        interval = (c.timestamp / 1000)
        # Spread 4 ticks across the candle period
        ticks.append((interval, c.open))
        ticks.append((interval + 0.25, c.high))
        ticks.append((interval + 0.50, c.low))
        ticks.append((interval + 0.75, c.close))
    return ticks

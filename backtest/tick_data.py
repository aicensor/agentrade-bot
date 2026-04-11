"""
Agentrade Engine v2 — Real Tick Data Provider (Mode 2)

Fetches actual trade-by-trade (tick) data from exchanges for realistic backtesting.

Sources:
  - Binance aggTrades API (REST, up to 1000 per call)
  - Hyperliquid recent trades API
  - Local CSV/Parquet cache for previously downloaded data

Unlike OHLCV candles (which lose intra-candle detail), tick data captures
every actual trade — essential for accurate SL/TP simulation.
"""

from __future__ import annotations

import csv
import gzip
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import ccxt
import structlog

logger = structlog.get_logger(__name__)

# Cache directory for downloaded tick data
TICK_CACHE_DIR = Path(__file__).parent.parent / "data" / "ticks"


@dataclass
class Tick:
    """A single trade/tick."""
    timestamp: float   # Unix seconds
    price: float
    quantity: float
    is_buyer_maker: bool = False  # True = seller-initiated (sell aggressor)

    @property
    def timestamp_ms(self) -> float:
        return self.timestamp * 1000


@dataclass
class TickStream:
    """Collection of ticks with metadata."""
    symbol: str
    exchange: str
    ticks: list[Tick] = field(default_factory=list)

    @property
    def start_time(self) -> float:
        return self.ticks[0].timestamp if self.ticks else 0

    @property
    def end_time(self) -> float:
        return self.ticks[-1].timestamp if self.ticks else 0

    @property
    def duration_hours(self) -> float:
        if len(self.ticks) < 2:
            return 0
        return (self.end_time - self.start_time) / 3600

    @property
    def tick_count(self) -> int:
        return len(self.ticks)

    @property
    def ticks_per_second(self) -> float:
        duration = self.end_time - self.start_time
        if duration <= 0:
            return 0
        return len(self.ticks) / duration

    def timestamps(self) -> list[float]:
        """Get all timestamps as a flat list."""
        return [t.timestamp for t in self.ticks]

    def prices(self) -> list[float]:
        """Get all prices as a flat list."""
        return [t.price for t in self.ticks]

    def as_tuples(self) -> list[tuple[float, float]]:
        """Get as (timestamp, price) tuples for engine consumption."""
        return [(t.timestamp, t.price) for t in self.ticks]

    def downsample(self, max_ticks: int) -> TickStream:
        """Downsample to max_ticks evenly spaced ticks."""
        if len(self.ticks) <= max_ticks:
            return self
        step = len(self.ticks) / max_ticks
        sampled = [self.ticks[int(i * step)] for i in range(max_ticks)]
        return TickStream(symbol=self.symbol, exchange=self.exchange, ticks=sampled)


def fetch_trades_ccxt(
    symbol: str = "SOL/USDC:USDC",
    exchange_id: str = "hyperliquid",
    limit: int = 5000,
    since_ms: int | None = None,
    max_pages: int = 10,
) -> TickStream:
    """
    Fetch recent trades from any exchange via ccxt.

    Paginates backwards to get up to limit * max_pages trades.
    Each exchange may have different pagination behavior.

    Args:
        symbol: Trading pair
        exchange_id: Exchange name
        limit: Trades per API call (max varies by exchange)
        since_ms: Start timestamp in ms (None = most recent)
        max_pages: Max pagination pages
    """
    exchange_class = getattr(ccxt, exchange_id, None)
    if not exchange_class:
        raise ValueError(f"Unknown exchange: {exchange_id}")

    exchange = exchange_class({"options": {"defaultType": "swap"}})
    exchange.load_markets()

    all_ticks: list[Tick] = []
    cursor = since_ms
    pages = 0

    logger.info(
        "fetching_trades",
        symbol=symbol,
        exchange=exchange_id,
        since_ms=since_ms,
    )

    while pages < max_pages:
        try:
            trades = exchange.fetch_trades(
                symbol, since=cursor, limit=limit
            )
        except Exception as e:
            logger.error("fetch_trades_failed", error=str(e), page=pages)
            break

        if not trades:
            break

        for t in trades:
            all_ticks.append(Tick(
                timestamp=t["timestamp"] / 1000,  # ms → sec
                price=float(t["price"]),
                quantity=float(t["amount"]),
                is_buyer_maker=t.get("side") == "sell",
            ))

        pages += 1
        last_ts = trades[-1]["timestamp"]

        if cursor is not None and last_ts <= cursor:
            break

        cursor = last_ts + 1
        time.sleep(0.3)  # Rate limit

    # Deduplicate and sort
    seen = set()
    unique_ticks = []
    for tick in all_ticks:
        key = (tick.timestamp, tick.price, tick.quantity)
        if key not in seen:
            seen.add(key)
            unique_ticks.append(tick)

    unique_ticks.sort(key=lambda t: t.timestamp)

    stream = TickStream(
        symbol=symbol,
        exchange=exchange_id,
        ticks=unique_ticks,
    )

    logger.info(
        "trades_loaded",
        total_ticks=stream.tick_count,
        duration_hours=f"{stream.duration_hours:.2f}",
        ticks_per_second=f"{stream.ticks_per_second:.1f}",
    )

    return stream


def fetch_trades_range(
    symbol: str = "SOL/USDC:USDC",
    exchange_id: str = "hyperliquid",
    start_ms: int | None = None,
    end_ms: int | None = None,
    batch_size: int = 1000,
    max_ticks: int = 500_000,
) -> TickStream:
    """
    Fetch trades for a specific time range, paginating forward.

    Args:
        start_ms: Start timestamp in ms
        end_ms: End timestamp in ms (default: now)
        batch_size: Trades per API call
        max_ticks: Safety cap on total ticks
    """
    exchange_class = getattr(ccxt, exchange_id, None)
    if not exchange_class:
        raise ValueError(f"Unknown exchange: {exchange_id}")

    exchange = exchange_class({"options": {"defaultType": "swap"}})
    exchange.load_markets()

    if end_ms is None:
        end_ms = int(time.time() * 1000)

    all_ticks: list[Tick] = []
    cursor = start_ms

    logger.info(
        "fetching_trades_range",
        symbol=symbol,
        exchange=exchange_id,
        start_ms=start_ms,
        end_ms=end_ms,
    )

    while len(all_ticks) < max_ticks:
        try:
            trades = exchange.fetch_trades(
                symbol, since=cursor, limit=batch_size
            )
        except Exception as e:
            logger.error("fetch_trades_range_failed", error=str(e))
            break

        if not trades:
            break

        for t in trades:
            ts_ms = t["timestamp"]
            if ts_ms > end_ms:
                break
            all_ticks.append(Tick(
                timestamp=ts_ms / 1000,
                price=float(t["price"]),
                quantity=float(t["amount"]),
                is_buyer_maker=t.get("side") == "sell",
            ))

        last_ts = trades[-1]["timestamp"]
        if last_ts >= end_ms or last_ts == cursor:
            break

        cursor = last_ts + 1
        time.sleep(0.3)

    stream = TickStream(
        symbol=symbol,
        exchange=exchange_id,
        ticks=all_ticks[:max_ticks],
    )

    logger.info(
        "trades_range_loaded",
        total_ticks=stream.tick_count,
        duration_hours=f"{stream.duration_hours:.2f}",
    )

    return stream


# ─── Cache / Persistence ──────────────────────────────────────────────────────

def save_ticks_csv(stream: TickStream, filepath: str | Path | None = None) -> Path:
    """Save tick data to compressed CSV for reuse."""
    if filepath is None:
        TICK_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        safe_symbol = stream.symbol.replace("/", "_").replace(":", "_")
        filename = (
            f"{safe_symbol}_{stream.exchange}"
            f"_{int(stream.start_time)}_{int(stream.end_time)}"
            f"_{stream.tick_count}.csv.gz"
        )
        filepath = TICK_CACHE_DIR / filename

    filepath = Path(filepath)

    with gzip.open(filepath, "wt", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "price", "quantity", "is_buyer_maker"])
        for tick in stream.ticks:
            writer.writerow([
                tick.timestamp,
                tick.price,
                tick.quantity,
                int(tick.is_buyer_maker),
            ])

    logger.info("ticks_saved", path=str(filepath), count=stream.tick_count)
    return filepath


def load_ticks_csv(
    filepath: str | Path,
    symbol: str = "UNKNOWN",
    exchange: str = "unknown",
) -> TickStream:
    """Load tick data from compressed CSV."""
    filepath = Path(filepath)
    ticks: list[Tick] = []

    opener = gzip.open if filepath.suffix == ".gz" else open

    with opener(filepath, "rt") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ticks.append(Tick(
                timestamp=float(row["timestamp"]),
                price=float(row["price"]),
                quantity=float(row["quantity"]),
                is_buyer_maker=bool(int(row.get("is_buyer_maker", 0))),
            ))

    stream = TickStream(symbol=symbol, exchange=exchange, ticks=ticks)
    logger.info("ticks_loaded", path=str(filepath), count=stream.tick_count)
    return stream


def candles_to_tick_stream(
    candles: list[dict],
    symbol: str = "UNKNOWN",
    exchange: str = "unknown",
) -> TickStream:
    """
    Convert OHLCV candles to a synthetic tick stream.
    Fallback when real tick data is unavailable.

    Each candle → 4 ticks: open, high, low, close
    """
    ticks: list[Tick] = []
    for c in candles:
        ts = c["timestamp"] / 1000 if c["timestamp"] > 1e12 else c["timestamp"]
        ticks.append(Tick(timestamp=ts, price=c["open"], quantity=0))
        ticks.append(Tick(timestamp=ts + 1, price=c["high"], quantity=0))
        ticks.append(Tick(timestamp=ts + 2, price=c["low"], quantity=0))
        ticks.append(Tick(timestamp=ts + 3, price=c["close"], quantity=0))

    return TickStream(symbol=symbol, exchange=exchange, ticks=ticks)

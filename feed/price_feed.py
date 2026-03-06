"""
Agentrade Engine v2 — WebSocket Price Feed

ONE connection per exchange → ALL price data, ALL pairs, real-time.
This replaces the 18-second REST polling cycle from the old bot.

Solves Problem #1: Real-time price data
Solves Problem #16: Disconnect detection + REST fallback
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import ccxt.pro as ccxtpro
import structlog

from core.event_bus import EventBus
from core.types import Event, PriceUpdate

logger = structlog.get_logger(__name__)


class PriceFeed:
    """
    Manages WebSocket price subscriptions for one exchange.

    Architecture:
    - One ccxt.pro exchange instance (no auth needed for public data)
    - Subscribes to ticker stream for all active symbols
    - Emits "price_update" events to EventBus on every tick
    - Health monitor detects stale feeds and falls back to REST (Problem #16)

    Usage:
        feed = PriceFeed("bybit", event_bus)
        await feed.subscribe(["BTC/USDT:USDT", "SOL/USDT:USDT"])
        await feed.start()
    """

    def __init__(
        self,
        exchange_id: str,
        event_bus: EventBus,
        stale_threshold_sec: float = 3.0,    # Problem #16: switch to REST after 3s
        critical_threshold_sec: float = 30.0, # Alert all users after 30s
    ) -> None:
        self.exchange_id = exchange_id
        self.event_bus = event_bus
        self.stale_threshold = stale_threshold_sec
        self.critical_threshold = critical_threshold_sec

        # State
        self._symbols: set[str] = set()
        self._exchange: ccxtpro.Exchange | None = None
        self._running = False
        self._tasks: list[asyncio.Task] = []

        # Health tracking (Problem #16)
        self._last_price_time: dict[str, float] = {}
        self._is_healthy = True
        self._using_fallback = False
        self._reconnect_count = 0

        # Stats
        self._tick_count = 0
        self._start_time = 0.0

    async def start(self) -> None:
        """Start the price feed — subscribes to all symbols"""
        if self._running:
            return

        self._running = True
        self._start_time = time.time()

        # Create unauthenticated exchange instance for public data
        exchange_class = getattr(ccxtpro, self.exchange_id)
        self._exchange = exchange_class({
            "enableRateLimit": True,
            "options": {"defaultType": "swap"},
        })

        logger.info(
            "price_feed_starting",
            exchange=self.exchange_id,
            symbols=len(self._symbols),
        )

        # Launch one task per symbol (ccxt.pro handles multiplexing internally)
        for symbol in self._symbols:
            task = asyncio.create_task(
                self._watch_symbol(symbol),
                name=f"watch_{self.exchange_id}_{symbol}",
            )
            self._tasks.append(task)

        # Launch health monitor
        self._tasks.append(
            asyncio.create_task(
                self._health_monitor(),
                name=f"health_{self.exchange_id}",
            )
        )

    async def stop(self) -> None:
        """Stop all subscriptions and close connection"""
        self._running = False

        for task in self._tasks:
            task.cancel()

        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        if self._exchange:
            await self._exchange.close()
            self._exchange = None

        logger.info("price_feed_stopped", exchange=self.exchange_id)

    def subscribe(self, symbols: list[str]) -> None:
        """Add symbols to watch list. Call before start() or dynamically."""
        for s in symbols:
            self._symbols.add(s)
        logger.info("symbols_subscribed", exchange=self.exchange_id, count=len(symbols))

    def unsubscribe(self, symbols: list[str]) -> None:
        """Remove symbols from watch list"""
        for s in symbols:
            self._symbols.discard(s)

    # ─── Internal: WebSocket Watcher ──────────────────────────────────────

    async def _watch_symbol(self, symbol: str) -> None:
        """
        Watch a single symbol's ticker via WebSocket.
        Auto-reconnects on disconnect.
        """
        while self._running:
            try:
                ticker = await self._exchange.watch_ticker(symbol)

                # Build price update
                update = PriceUpdate(
                    symbol=symbol,
                    last_price=float(ticker.get("last", 0)),
                    mark_price=float(ticker.get("markPrice", 0)) if ticker.get("markPrice") else None,
                    bid=float(ticker.get("bid", 0)) if ticker.get("bid") else None,
                    ask=float(ticker.get("ask", 0)) if ticker.get("ask") else None,
                    timestamp=time.time(),
                    exchange=self.exchange_id,
                )

                # Track health
                self._last_price_time[symbol] = time.time()
                self._tick_count += 1
                self._is_healthy = True
                self._using_fallback = False

                # Emit to event bus — this triggers all position evaluations
                await self.event_bus.emit(Event(name="price_update", data=update))

            except asyncio.CancelledError:
                break

            except Exception as e:
                logger.warning(
                    "ws_error",
                    exchange=self.exchange_id,
                    symbol=symbol,
                    error=str(e),
                )
                self._reconnect_count += 1

                # Wait before reconnecting (exponential backoff, max 30s)
                backoff = min(2 ** min(self._reconnect_count, 5), 30)
                await asyncio.sleep(backoff)

    # ─── Problem #16: Health Monitor + REST Fallback ──────────────────────

    async def _health_monitor(self) -> None:
        """
        Monitor feed health. Switch to REST fallback if WebSocket goes stale.
        This is the safety net that ensures we always have price data.
        """
        while self._running:
            try:
                await asyncio.sleep(1.0)  # Check every second

                now = time.time()
                stale_symbols: list[str] = []

                for symbol in self._symbols:
                    last_time = self._last_price_time.get(symbol, 0)
                    gap = now - last_time if last_time > 0 else 0

                    # Stale feed detection
                    if last_time > 0 and gap > self.stale_threshold:
                        stale_symbols.append(symbol)

                        # Critical: no data for 30+ seconds
                        if gap > self.critical_threshold:
                            await self.event_bus.emit(Event(
                                name="alert",
                                data={
                                    "level": "critical",
                                    "message": (
                                        f"⚠️ {self.exchange_id} price feed OFFLINE for {symbol} "
                                        f"({gap:.0f}s). Exchange-side SL active. Monitor manually."
                                    ),
                                },
                            ))

                # Switch to REST fallback for stale symbols
                if stale_symbols and not self._using_fallback:
                    self._using_fallback = True
                    self._is_healthy = False
                    logger.warning(
                        "switching_to_rest_fallback",
                        exchange=self.exchange_id,
                        stale_symbols=stale_symbols,
                    )
                    await self.event_bus.emit(Event(
                        name="alert",
                        data={
                            "level": "warning",
                            "message": (
                                f"⚠️ {self.exchange_id} WebSocket stale. "
                                f"Switching to REST fallback for {len(stale_symbols)} symbols."
                            ),
                        },
                    ))

                    # Start REST polling for stale symbols
                    asyncio.create_task(self._rest_fallback(stale_symbols))

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("health_monitor_error", error=str(e))

    async def _rest_fallback(self, symbols: list[str]) -> None:
        """
        REST polling fallback when WebSocket is down.
        Polls every 1 second until WebSocket recovers.
        """
        logger.info("rest_fallback_started", symbols=symbols)

        while self._running and self._using_fallback:
            try:
                for symbol in symbols:
                    ticker = await self._exchange.fetch_ticker(symbol)

                    update = PriceUpdate(
                        symbol=symbol,
                        last_price=float(ticker.get("last", 0)),
                        mark_price=float(ticker.get("markPrice", 0)) if ticker.get("markPrice") else None,
                        bid=float(ticker.get("bid", 0)) if ticker.get("bid") else None,
                        ask=float(ticker.get("ask", 0)) if ticker.get("ask") else None,
                        timestamp=time.time(),
                        exchange=self.exchange_id,
                    )

                    self._last_price_time[symbol] = time.time()
                    await self.event_bus.emit(Event(name="price_update", data=update))

                await asyncio.sleep(1.0)  # Poll every 1 second

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("rest_fallback_error", error=str(e))
                await asyncio.sleep(2.0)

        logger.info("rest_fallback_stopped", reason="ws_recovered" if self._running else "shutdown")

    # ─── Stats ────────────────────────────────────────────────────────────

    @property
    def stats(self) -> dict:
        uptime = time.time() - self._start_time if self._start_time else 0
        return {
            "exchange": self.exchange_id,
            "symbols": len(self._symbols),
            "ticks": self._tick_count,
            "ticks_per_sec": round(self._tick_count / uptime, 2) if uptime > 0 else 0,
            "is_healthy": self._is_healthy,
            "using_fallback": self._using_fallback,
            "reconnects": self._reconnect_count,
            "uptime_sec": round(uptime),
        }

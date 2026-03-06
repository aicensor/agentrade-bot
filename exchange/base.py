"""
Agentrade Engine v2 — Exchange Adapter

Unified interface to any exchange via ccxt.pro.
Handles the 10% of quirks that ccxt doesn't abstract perfectly (Problem #5).

One adapter instance per user (each user has their own API key).
WebSocket feeds are shared across users (one connection per exchange).
"""

from __future__ import annotations

import asyncio
from typing import Any

import ccxt.pro as ccxtpro
import structlog

from core.types import (
    Action, ActionType, OrderBookSnapshot, Position, PriceUpdate,
    SLType, Side, UserConfig,
)
from exchange.rate_limiter import RateLimiter, RateLimiterPool

logger = structlog.get_logger(__name__)


class ExchangeAdapter:
    """
    Per-user exchange connection for order management (REST).

    Usage:
        adapter = await ExchangeAdapter.create("bybit", user_config, rate_pool)
        await adapter.set_stop_loss(position, price=88.0)
        await adapter.close_position(position, pct=0.33)
    """

    def __init__(
        self,
        exchange: ccxtpro.Exchange,
        user: UserConfig,
        rate_limiter: RateLimiter,
    ) -> None:
        self.exchange = exchange
        self.user = user
        self.rate_limiter = rate_limiter
        self._closed = False

    @classmethod
    async def create(
        cls,
        user: UserConfig,
        rate_pool: RateLimiterPool,
    ) -> ExchangeAdapter:
        """Factory method — creates exchange instance with user credentials"""
        exchange_class = getattr(ccxtpro, user.exchange, None)
        if exchange_class is None:
            raise ValueError(f"Unsupported exchange: {user.exchange}")

        config: dict[str, Any] = {
            "apiKey": user.api_key,
            "secret": user.api_secret,
            "enableRateLimit": False,  # We handle rate limiting ourselves
            "options": {
                "defaultType": "swap",  # Perpetual futures
            },
        }

        # OKX requires passphrase
        if user.exchange == "okx" and user.passphrase:
            config["password"] = user.passphrase

        exchange = exchange_class(config)

        # Load markets on first creation
        await exchange.load_markets()

        limiter = rate_pool.get(user.api_key, user.exchange)
        return cls(exchange, user, limiter)

    # ─── Order Operations ─────────────────────────────────────────────────

    async def set_stop_loss(
        self,
        position: Position,
        price: float,
        sl_type: SLType = SLType.MARKET,
        buffer_pct: float = 0.0,
    ) -> dict | None:
        """
        Set or update stop-loss for a position.
        Returns order info dict or None on failure.

        Problem #9: If sl_type=LIMIT, places limit order with buffer.
        """
        await self.rate_limiter.acquire()

        try:
            side = "buy" if position.side == Side.SHORT else "sell"
            symbol = position.symbol

            params: dict[str, Any] = {
                "stopLossPrice": price,
                "slTriggerBy": "MarkPrice",  # Problem #11: use mark price for trigger
            }

            # Problem #9: Limit SL with buffer
            if sl_type == SLType.LIMIT and buffer_pct > 0:
                if position.side == Side.LONG:
                    # Long position, SL below → limit slightly below trigger
                    params["slOrderType"] = "Limit"
                    params["slLimitPrice"] = price * (1 - buffer_pct / 100)
                else:
                    # Short position, SL above → limit slightly above trigger
                    params["slOrderType"] = "Limit"
                    params["slLimitPrice"] = price * (1 + buffer_pct / 100)

            # Use exchange-specific method for setting trading stop
            result = await self.exchange.set_trading_stop(
                symbol=symbol,
                side=side,
                stop_loss=price,
                params=params,
            )

            logger.info(
                "sl_updated",
                user=position.user_id,
                symbol=symbol,
                side=position.side.value,
                sl_price=price,
                sl_type=sl_type.value,
            )
            return result

        except Exception as e:
            logger.error(
                "sl_update_failed",
                user=position.user_id,
                symbol=position.symbol,
                error=str(e),
            )
            return None

    async def set_take_profit(
        self,
        position: Position,
        price: float,
        size: float | None = None,
    ) -> dict | None:
        """Set or update take-profit for a position"""
        await self.rate_limiter.acquire()

        try:
            side = "buy" if position.side == Side.SHORT else "sell"
            params: dict[str, Any] = {
                "takeProfitPrice": price,
                "tpTriggerBy": "MarkPrice",
            }

            if size is not None:
                params["tpSize"] = str(size)

            result = await self.exchange.set_trading_stop(
                symbol=position.symbol,
                side=side,
                take_profit=price,
                params=params,
            )

            logger.info(
                "tp_updated",
                user=position.user_id,
                symbol=position.symbol,
                tp_price=price,
            )
            return result

        except Exception as e:
            logger.error(
                "tp_update_failed",
                user=position.user_id,
                symbol=position.symbol,
                error=str(e),
            )
            return None

    async def close_position(
        self,
        position: Position,
        pct: float = 1.0,
    ) -> dict | None:
        """
        Close a position (full or partial).
        pct=0.33 means close 33% of the position.
        """
        await self.rate_limiter.acquire()

        try:
            side = "buy" if position.side == Side.SHORT else "sell"
            qty = position.size * pct

            result = await self.exchange.create_order(
                symbol=position.symbol,
                type="market",
                side=side,
                amount=qty,
                params={"reduceOnly": True},
            )

            logger.info(
                "position_closed",
                user=position.user_id,
                symbol=position.symbol,
                pct=pct,
                qty=qty,
                side=side,
            )
            return result

        except Exception as e:
            logger.error(
                "close_failed",
                user=position.user_id,
                symbol=position.symbol,
                error=str(e),
            )
            return None

    # ─── Position Queries ─────────────────────────────────────────────────

    async def fetch_positions(self, symbols: list[str] | None = None) -> list[dict]:
        """Fetch all open positions from exchange (Problem #4: reconciliation)"""
        await self.rate_limiter.acquire()

        try:
            positions = await self.exchange.fetch_positions(symbols)
            return [p for p in positions if float(p.get("contracts", 0)) > 0]
        except Exception as e:
            logger.error("fetch_positions_failed", error=str(e))
            return []

    async def fetch_liquidation_price(self, symbol: str) -> float:
        """Get liquidation price for a position (Problem #10)"""
        await self.rate_limiter.acquire()

        try:
            positions = await self.exchange.fetch_positions([symbol])
            for pos in positions:
                if float(pos.get("contracts", 0)) > 0:
                    return float(pos.get("liquidationPrice", 0) or 0)
            return 0.0
        except Exception as e:
            logger.error("fetch_liq_price_failed", symbol=symbol, error=str(e))
            return 0.0

    async def fetch_order_book(self, symbol: str, limit: int = 10) -> OrderBookSnapshot:
        """Fetch order book for liquidity check (Problem #15)"""
        await self.rate_limiter.acquire()

        try:
            book = await self.exchange.fetch_order_book(symbol, limit)
            return OrderBookSnapshot(
                symbol=symbol,
                bids=[(b[0], b[1]) for b in book.get("bids", [])],
                asks=[(a[0], a[1]) for a in book.get("asks", [])],
            )
        except Exception as e:
            logger.error("fetch_orderbook_failed", symbol=symbol, error=str(e))
            return OrderBookSnapshot(symbol=symbol)

    async def fetch_funding_rate(self, symbol: str) -> float:
        """Get current funding rate (Problem #12)"""
        await self.rate_limiter.acquire()

        try:
            info = await self.exchange.fetch_funding_rate(symbol)
            return float(info.get("fundingRate", 0) or 0)
        except Exception as e:
            logger.error("fetch_funding_failed", symbol=symbol, error=str(e))
            return 0.0

    # ─── Lifecycle ────────────────────────────────────────────────────────

    async def close(self) -> None:
        """Close exchange connection"""
        if not self._closed:
            await self.exchange.close()
            self._closed = True

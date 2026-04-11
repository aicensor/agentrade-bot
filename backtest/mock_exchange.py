"""
Agentrade Engine v2 — Mock Exchange Adapter (Mode 2)

Drop-in replacement for ExchangeAdapter that simulates order execution locally.
No real exchange API calls — fills orders against the simulated price feed.

Used by the Mode 2 event-driven simulation engine to validate strategies
using the SAME live trading components (PositionManager, Strategy, OrderQueue, OrderExecutor).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import structlog

from core.types import (
    Action, ActionType, OrderBookSnapshot, Position, PriceUpdate,
    SLType, Side, UserConfig,
)

logger = structlog.get_logger(__name__)


@dataclass
class SimulatedOrder:
    """An order tracked by the mock exchange."""
    order_id: str
    symbol: str
    side: str          # "buy" or "sell"
    order_type: str    # "market", "stop_loss", "take_profit"
    price: float       # trigger/limit price
    size: float
    reduce_only: bool = True
    created_at: float = field(default_factory=time.time)
    status: str = "open"  # "open", "filled", "cancelled"
    fill_price: float = 0.0


class MockExchangeAdapter:
    """
    Simulates ExchangeAdapter for backtesting.
    Same interface as ExchangeAdapter — can be swapped in via OrderExecutor.

    Tracks:
      - Current SL/TP orders per position
      - Simulated fills with slippage
      - Trade history for analysis
    """

    def __init__(
        self,
        user: UserConfig | None = None,
        slippage_pct: float = 0.05,
        fee_pct: float = 0.035,
        min_notional_usd: float = 10.0,
    ) -> None:
        self.user = user
        self.slippage_pct = slippage_pct
        self.fee_pct = fee_pct
        self.min_notional_usd = min_notional_usd
        self._closed = False

        # Current price (updated by sim engine on each tick)
        self.current_price: float = 0.0
        self.current_time: float = 0.0

        # Active SL/TP orders: position_key → SimulatedOrder
        self._sl_orders: dict[str, SimulatedOrder] = {}
        self._tp_orders: dict[str, SimulatedOrder] = {}

        # Trade log
        self._fills: list[dict] = []
        self._order_counter: int = 0

        # Position state mirror (for fetch_positions)
        self._positions: dict[str, dict] = {}

    @classmethod
    async def create(
        cls,
        user: UserConfig,
        rate_pool: Any = None,
    ) -> MockExchangeAdapter:
        """Factory — same signature as ExchangeAdapter.create()"""
        return cls(user=user)

    def update_price(self, price: float, timestamp: float) -> None:
        """Called by sim engine on each tick to update current market price."""
        self.current_price = price
        self.current_time = timestamp

    # ─── Order Operations (same interface as ExchangeAdapter) ──────────

    async def set_stop_loss(
        self,
        position: Position,
        price: float,
        sl_type: SLType = SLType.MARKET,
        buffer_pct: float = 0.0,
    ) -> dict | None:
        """Set or update stop-loss for a position."""
        self._order_counter += 1
        order_id = f"sim_sl_{self._order_counter}"

        order = SimulatedOrder(
            order_id=order_id,
            symbol=position.symbol,
            side="buy" if position.side == Side.SHORT else "sell",
            order_type="stop_loss",
            price=price,
            size=position.size,
        )

        self._sl_orders[position.position_key] = order

        logger.debug(
            "mock_sl_set",
            key=position.position_key,
            sl_price=price,
        )

        return {"id": order_id, "status": "open", "price": price}

    async def set_take_profit(
        self,
        position: Position,
        price: float,
        size: float | None = None,
    ) -> dict | None:
        """Set or update take-profit for a position."""
        self._order_counter += 1
        order_id = f"sim_tp_{self._order_counter}"

        order = SimulatedOrder(
            order_id=order_id,
            symbol=position.symbol,
            side="buy" if position.side == Side.SHORT else "sell",
            order_type="take_profit",
            price=price,
            size=size or position.size,
        )

        self._tp_orders[position.position_key] = order

        return {"id": order_id, "status": "open", "price": price}

    def calc_min_order_qty(self, price: float) -> float:
        """Calculate minimum order quantity based on min notional."""
        if price <= 0 or self.min_notional_usd <= 0:
            return 0.0
        return self.min_notional_usd / price

    async def close_position(
        self,
        position: Position,
        pct: float = 1.0,
        current_price: float = 0.0,
    ) -> dict | None:
        """Simulate closing a position (full or partial)."""
        price = current_price or self.current_price
        if price <= 0:
            price = position.entry_price

        qty = position.size * pct

        # Min notional check (same logic as real adapter)
        if price > 0 and self.min_notional_usd > 0:
            notional = qty * price
            if notional < self.min_notional_usd:
                min_qty = self.calc_min_order_qty(price)
                if position.size <= min_qty:
                    qty = position.size
                else:
                    qty = min_qty

        # Apply slippage
        if position.side == Side.LONG:
            fill_price = price * (1 - self.slippage_pct / 100)
        else:
            fill_price = price * (1 + self.slippage_pct / 100)

        # Calculate fee
        fee = fill_price * qty * self.fee_pct / 100

        # Calculate PnL
        if position.side == Side.LONG:
            pnl = (fill_price - position.entry_price) * qty - fee
        else:
            pnl = (position.entry_price - fill_price) * qty - fee

        self._order_counter += 1
        fill = {
            "id": f"sim_fill_{self._order_counter}",
            "symbol": position.symbol,
            "side": "sell" if position.side == Side.LONG else "buy",
            "price": fill_price,
            "qty": qty,
            "pnl": pnl,
            "fee": fee,
            "timestamp": self.current_time,
            "info": {"actual_close_qty": qty},
        }

        self._fills.append(fill)

        logger.debug(
            "mock_close",
            key=position.position_key,
            pct=pct,
            qty=qty,
            fill_price=fill_price,
            pnl=round(pnl, 4),
        )

        return fill

    # ─── Position Queries ──────────────────────────────────────────────

    async def fetch_positions(self, symbols: list[str] | None = None) -> list[dict]:
        """Return simulated positions."""
        positions = list(self._positions.values())
        if symbols:
            positions = [p for p in positions if p.get("symbol") in symbols]
        return positions

    async def fetch_liquidation_price(self, symbol: str) -> float:
        """Return simulated liquidation price."""
        return 0.0

    async def fetch_order_book(self, symbol: str, limit: int = 10) -> OrderBookSnapshot:
        """Return simulated order book."""
        if self.current_price <= 0:
            return OrderBookSnapshot(symbol=symbol)
        spread = self.current_price * 0.0001  # 0.01% spread
        bids = [(self.current_price - spread * (i + 1), 100.0) for i in range(limit)]
        asks = [(self.current_price + spread * (i + 1), 100.0) for i in range(limit)]
        return OrderBookSnapshot(symbol=symbol, bids=bids, asks=asks)

    async def fetch_funding_rate(self, symbol: str) -> float:
        """Return simulated funding rate."""
        return 0.0001  # 0.01% default

    # ─── SL/TP Check (called by sim engine each tick) ──────────────────

    def check_sl_tp_fills(
        self,
        positions: list[Position],
        price: float,
    ) -> list[tuple[Position, str, float]]:
        """
        Check if any SL/TP orders should fill at current price.

        Returns list of (position, fill_type, fill_price) tuples.
        Called by sim engine BEFORE strategy evaluation on each tick.
        """
        fills: list[tuple[Position, str, float]] = []

        for pos in positions:
            key = pos.position_key

            # Check SL
            sl_order = self._sl_orders.get(key)
            if sl_order and sl_order.status == "open":
                hit = False
                if pos.side == Side.LONG and price <= sl_order.price:
                    hit = True
                elif pos.side == Side.SHORT and price >= sl_order.price:
                    hit = True

                if hit:
                    # Apply slippage to fill
                    if pos.side == Side.LONG:
                        fill_price = sl_order.price * (1 - self.slippage_pct / 100)
                    else:
                        fill_price = sl_order.price * (1 + self.slippage_pct / 100)

                    sl_order.status = "filled"
                    sl_order.fill_price = fill_price
                    fills.append((pos, "sl_hit", fill_price))
                    continue  # Don't check TP if SL hit

            # Check TP
            tp_order = self._tp_orders.get(key)
            if tp_order and tp_order.status == "open":
                hit = False
                if pos.side == Side.LONG and price >= tp_order.price:
                    hit = True
                elif pos.side == Side.SHORT and price <= tp_order.price:
                    hit = True

                if hit:
                    if pos.side == Side.LONG:
                        fill_price = tp_order.price * (1 - self.slippage_pct / 100)
                    else:
                        fill_price = tp_order.price * (1 + self.slippage_pct / 100)

                    tp_order.status = "filled"
                    tp_order.fill_price = fill_price
                    fills.append((pos, "tp_hit", fill_price))

        return fills

    def clear_orders_for_position(self, position_key: str) -> None:
        """Remove all orders for a closed position."""
        self._sl_orders.pop(position_key, None)
        self._tp_orders.pop(position_key, None)

    # ─── Lifecycle ─────────────────────────────────────────────────────

    async def close(self) -> None:
        """Cleanup (no-op for mock)."""
        self._closed = True

    @property
    def stats(self) -> dict:
        return {
            "total_fills": len(self._fills),
            "active_sl_orders": sum(1 for o in self._sl_orders.values() if o.status == "open"),
            "active_tp_orders": sum(1 for o in self._tp_orders.values() if o.status == "open"),
        }

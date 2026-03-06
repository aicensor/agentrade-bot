"""
Agentrade Engine v2 — Position Manager

In-memory position state, indexed by SYMBOL for fast price-tick lookups.

When a price update arrives for SOL/USDT, we need to check ALL users'
SOL positions instantly. Dict lookup = O(1).

100 users × 10 pairs = 1,000 positions in memory ≈ 2 MB. Trivial.
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Any

import structlog

from core.event_bus import EventBus
from core.types import (
    Action, Event, Position, PositionState, PriceUpdate, Side,
)
from strategy.base import BaseStrategy
from strategy.multi_level_trail import MultiLevelTrailStrategy

logger = structlog.get_logger(__name__)


# Strategy registry — add new strategies here
STRATEGIES: dict[str, type[BaseStrategy]] = {
    "multi_level_trail": MultiLevelTrailStrategy,
}


class PositionManager:
    """
    Manages all active positions in memory.

    Data structure:
      positions_by_symbol["SOL/USDT:USDT"] = [pos_user1, pos_user2, ...]
      positions_by_key["user1:SOL/USDT:USDT:short"] = pos_user1

    On every price tick:
      1. Look up all positions for that symbol
      2. Run strategy.evaluate() for each (pure math, <0.01ms each)
      3. Emit actions to order queue

    This is the CORE of the engine — the hot path.
    """

    def __init__(self, event_bus: EventBus) -> None:
        self.event_bus = event_bus

        # Primary index: by symbol (for price tick lookups)
        self._by_symbol: dict[str, list[Position]] = defaultdict(list)

        # Secondary index: by position key (for direct access)
        self._by_key: dict[str, Position] = {}

        # Strategy instances (stateless, reusable)
        self._strategies: dict[str, BaseStrategy] = {}
        for name, cls in STRATEGIES.items():
            self._strategies[name] = cls()

        # Stats
        self._evaluations = 0
        self._actions_generated = 0

    async def start(self) -> None:
        """Register event handlers"""
        self.event_bus.on("price_update", self._on_price_update)
        logger.info("position_manager_started", positions=len(self._by_key))

    # ─── Position CRUD ────────────────────────────────────────────────────

    def add_position(self, position: Position) -> None:
        """Add a new position to track"""
        key = position.position_key
        if key in self._by_key:
            logger.warning("position_already_exists", key=key)
            return

        self._by_key[key] = position
        self._by_symbol[position.symbol].append(position)

        logger.info(
            "position_added",
            key=key,
            entry=position.entry_price,
            size=position.size,
            strategy=position.strategy_name,
        )

        # Initialize extremes
        position.highest_since_entry = position.entry_price
        position.lowest_since_entry = position.entry_price

        # Notify strategy of new position
        strategy = self._strategies.get(position.strategy_name)
        if strategy:
            action = strategy.on_position_opened(position)
            if action:
                self.event_bus.emit_nowait(Event(name="action", data=action))

    def remove_position(self, position_key: str) -> Position | None:
        """Remove a position from tracking"""
        position = self._by_key.pop(position_key, None)
        if position:
            symbol_list = self._by_symbol[position.symbol]
            self._by_symbol[position.symbol] = [
                p for p in symbol_list if p.position_key != position_key
            ]
            logger.info("position_removed", key=position_key)
        return position

    def get_position(self, position_key: str) -> Position | None:
        """Get a position by key"""
        return self._by_key.get(position_key)

    def get_positions_for_symbol(self, symbol: str) -> list[Position]:
        """Get all positions for a symbol"""
        return self._by_symbol.get(symbol, [])

    def get_positions_for_user(self, user_id: str) -> list[Position]:
        """Get all positions for a user"""
        return [p for p in self._by_key.values() if p.user_id == user_id]

    @property
    def all_positions(self) -> list[Position]:
        return list(self._by_key.values())

    @property
    def active_symbols(self) -> set[str]:
        """All symbols with at least one active position"""
        return {s for s, positions in self._by_symbol.items() if positions}

    # ─── Price Event Handler (HOT PATH) ──────────────────────────────────

    async def _on_price_update(self, event: Event) -> None:
        """
        Called on EVERY price tick from PriceFeed.
        This is the hottest path in the entire engine.

        For each position on this symbol:
          1. Run strategy.evaluate() — pure math
          2. If action returned → emit to order queue
        """
        update: PriceUpdate = event.data
        positions = self._by_symbol.get(update.symbol, [])

        if not positions:
            return

        for position in positions:
            # Skip non-active positions
            if position.state not in (PositionState.OPEN, PositionState.TRAILING):
                continue

            # Get strategy
            strategy = self._strategies.get(position.strategy_name)
            if not strategy:
                continue

            # EVALUATE — pure math, no I/O
            self._evaluations += 1
            action = strategy.evaluate(
                position=position,
                price=update.last_price,
                mark_price=update.mark_price,
            )

            if action is not None:
                self._actions_generated += 1

                # Mark position as trailing if it wasn't already
                if position.state == PositionState.OPEN:
                    position.state = PositionState.TRAILING

                # Emit action to order queue
                await self.event_bus.emit(Event(name="action", data=action))

    # ─── Reconciliation (Problem #4) ─────────────────────────────────────

    def reconcile_with_exchange(self, exchange_positions: list[dict]) -> list[str]:
        """
        Compare in-memory state with exchange state.
        Returns list of issues found.

        Called periodically (every 30-60s) by the engine.
        """
        issues: list[str] = []

        for ex_pos in exchange_positions:
            symbol = ex_pos.get("symbol", "")
            side_str = ex_pos.get("side", "").lower()
            size = float(ex_pos.get("contracts", 0))

            if size == 0:
                continue

            # Find matching in-memory position
            side = Side.LONG if "long" in side_str else Side.SHORT
            found = False

            for pos in self._by_symbol.get(symbol, []):
                if pos.side == side:
                    found = True
                    # Check for size drift
                    if abs(pos.size - size) / max(pos.size, 0.0001) > 0.01:
                        issues.append(
                            f"Size drift: {pos.position_key} "
                            f"memory={pos.size} exchange={size}"
                        )
                        pos.size = size  # Exchange wins (Problem #4)
                    break

            if not found:
                issues.append(f"Unknown exchange position: {symbol} {side.value} size={size}")

        if issues:
            logger.warning("reconciliation_drift", issues=issues)

        return issues

    # ─── Serialization (Problem #3) ──────────────────────────────────────

    def snapshot(self) -> list[dict]:
        """Serialize all positions for persistence"""
        return [pos.to_dict() for pos in self._by_key.values()]

    def restore(self, data: list[dict]) -> int:
        """Restore positions from snapshot. Returns count restored."""
        count = 0
        for item in data:
            try:
                position = Position.from_dict(item)
                self.add_position(position)
                count += 1
            except Exception as e:
                logger.error("restore_failed", error=str(e), data=item)
        return count

    # ─── Stats ────────────────────────────────────────────────────────────

    @property
    def stats(self) -> dict:
        return {
            "total_positions": len(self._by_key),
            "active_symbols": len(self.active_symbols),
            "evaluations": self._evaluations,
            "actions_generated": self._actions_generated,
            "hit_rate": (
                round(self._actions_generated / max(self._evaluations, 1) * 100, 4)
            ),
        }

"""
Agentrade Engine v2 — Base Strategy

Every strategy implements ONE method: evaluate(position, price) → Action | None

Rules:
  1. PURE MATH only. No I/O. No API calls. No DB queries.
  2. Must complete in < 0.01ms (it's called on every price tick)
  3. Returns None = no action needed
  4. Returns Action = queue an order modification
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from core.types import Action, Position


class BaseStrategy(ABC):
    """
    Abstract base class for all trading strategies.

    Strategies are stateless evaluators — all state lives in the Position object.
    This makes strategies easy to test, serialize, and hot-reload.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique strategy identifier"""
        ...

    @abstractmethod
    def evaluate(self, position: Position, price: float, mark_price: float | None = None) -> Action | None:
        """
        Evaluate whether an action is needed for this position at this price.

        Args:
            position: Current position state (includes trailing state, TP levels, etc.)
            price: Latest price from WebSocket (last traded price)
            mark_price: Mark price from exchange (used for trigger validation, Problem #11)

        Returns:
            Action if the strategy wants to modify SL/TP or close position
            None if no action is needed

        RULES:
        - Must be pure math. No I/O whatsoever.
        - Must be idempotent (same inputs → same output)
        - Must handle both LONG and SHORT positions
        """
        ...

    def on_position_opened(self, position: Position) -> Action | None:
        """
        Called once when a new position is detected.
        Use to set initial SL/TP levels.
        Default: no action (override in subclass).
        """
        return None

    def on_tp_filled(self, position: Position, tp_level: int, fill_price: float) -> Action | None:
        """
        Called when a take-profit order fills.
        Use to adjust remaining position's SL.
        Default: no action (override in subclass).
        """
        return None

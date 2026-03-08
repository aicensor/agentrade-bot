"""
Agentrade Engine v2 — Order Executor

The ONLY component that talks to exchange REST APIs for order management.
Translates Actions into exchange API calls.

Manages per-user ExchangeAdapter instances (one per user API key).
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from core.types import Action, ActionType, Position, SLType
from exchange.base import ExchangeAdapter
from exchange.rate_limiter import RateLimiterPool
from core.types import UserConfig

logger = structlog.get_logger(__name__)


class OrderExecutor:
    """
    Executes Actions by calling exchange APIs.

    Each user gets their own ExchangeAdapter (own API key, own rate limiter).
    This enables concurrent execution: 100 users = 100 parallel API calls.

    Usage:
        executor = OrderExecutor(rate_pool)
        executor.register_user(user_config)
        success = await executor.execute(action)
    """

    def __init__(self, rate_pool: RateLimiterPool) -> None:
        self.rate_pool = rate_pool

        # Per-user adapters: user_id → ExchangeAdapter
        self._adapters: dict[str, ExchangeAdapter] = {}

        # Per-user configs for lazy adapter creation
        self._user_configs: dict[str, UserConfig] = {}

        # Track position state updates
        self._position_manager = None  # Set by engine after init

    def set_position_manager(self, pm: Any) -> None:
        """Set reference to position manager for state updates"""
        self._position_manager = pm

    async def register_user(self, user: UserConfig) -> None:
        """Register a user — creates exchange adapter lazily"""
        self._user_configs[user.user_id] = user
        logger.info("user_registered", user_id=user.user_id, exchange=user.exchange)

    async def get_adapter(self, user_id: str) -> ExchangeAdapter | None:
        """Get or create adapter for a user"""
        if user_id not in self._adapters:
            config = self._user_configs.get(user_id)
            if not config:
                logger.error("no_config_for_user", user_id=user_id)
                return None

            try:
                adapter = await ExchangeAdapter.create(config, self.rate_pool)
                self._adapters[user_id] = adapter
            except Exception as e:
                logger.error("adapter_creation_failed", user_id=user_id, error=str(e))
                return None

        return self._adapters[user_id]

    async def execute(self, action: Action) -> bool:
        """
        Execute a single action. Called by OrderQueue.

        Returns True on success, False on failure.
        This is the callback passed to OrderQueue.
        """
        adapter = await self.get_adapter(action.user_id)
        if not adapter:
            return False

        try:
            if action.type == ActionType.MOVE_SL:
                return await self._execute_move_sl(adapter, action)

            elif action.type == ActionType.MOVE_TP:
                return await self._execute_move_tp(adapter, action)

            elif action.type == ActionType.PARTIAL_CLOSE:
                return await self._execute_partial_close(adapter, action)

            elif action.type == ActionType.FULL_CLOSE:
                return await self._execute_full_close(adapter, action)

            elif action.type == ActionType.ALERT:
                # Alerts don't need exchange calls — just emit
                logger.info("alert_action", user=action.user_id, message=action.message)
                return True

            else:
                logger.warning("unknown_action_type", type=action.type)
                return False

        except Exception as e:
            logger.error(
                "execute_failed",
                user=action.user_id,
                action=action.type.value,
                symbol=action.symbol,
                error=str(e),
            )
            return False

    # ─── Action Handlers ──────────────────────────────────────────────────

    async def _execute_move_sl(self, adapter: ExchangeAdapter, action: Action) -> bool:
        """Update stop-loss on exchange"""
        position = self._get_position(action.position_key)
        if not position:
            return False

        result = await adapter.set_stop_loss(
            position=position,
            price=action.price,
            sl_type=action.sl_type,
            buffer_pct=action.sl_buffer_pct,
        )

        if result:
            # Update in-memory state
            position.current_sl = action.price
            position.last_sl_update_price = action.price
            return True

        return False

    async def _execute_move_tp(self, adapter: ExchangeAdapter, action: Action) -> bool:
        """Update take-profit on exchange"""
        position = self._get_position(action.position_key)
        if not position:
            return False

        result = await adapter.set_take_profit(position=position, price=action.price)

        if result:
            position.current_tp = action.price
            return True

        return False

    async def _execute_partial_close(self, adapter: ExchangeAdapter, action: Action) -> bool:
        """
        Partial close + SL update.
        action.close_pct = how much to close (0.33 = 33%)
        action.price = new SL after partial close

        Handles minimum notional: adapter may close more than requested
        if the requested amount is below exchange minimum.
        """
        position = self._get_position(action.position_key)
        if not position:
            return False

        # Get current price for min notional check
        current_price = position.entry_price  # fallback
        if hasattr(position, "last_price") and position.last_price:
            current_price = position.last_price

        # Step 1: Close the partial amount (adapter handles min notional)
        close_result = await adapter.close_position(
            position, pct=action.close_pct, current_price=current_price
        )
        if not close_result:
            return False

        # Use actual closed qty (may differ from requested due to min notional)
        actual_qty = close_result.get("info", {}).get("actual_close_qty", 0)
        if actual_qty > 0:
            position.size = max(0.0, position.size - actual_qty)
        else:
            # Fallback to percentage-based
            position.size *= (1 - action.close_pct)

        # Step 2: Update SL on remaining position (only if position still open)
        if position.size > 0 and action.price > 0:
            sl_result = await adapter.set_stop_loss(
                position=position,
                price=action.price,
                sl_type=action.sl_type,
                buffer_pct=action.sl_buffer_pct,
            )
            if sl_result:
                position.current_sl = action.price
                position.last_sl_update_price = action.price
        elif position.size <= 0:
            from core.types import PositionState
            position.state = PositionState.CLOSED
            logger.info(
                "position_fully_closed_by_partials",
                user=action.user_id,
                symbol=action.symbol,
            )

        logger.info(
            "partial_close_executed",
            user=action.user_id,
            symbol=action.symbol,
            requested_pct=action.close_pct,
            actual_closed_qty=actual_qty,
            remaining_size=position.size,
            new_sl=action.price,
        )

        return True

    async def _execute_full_close(self, adapter: ExchangeAdapter, action: Action) -> bool:
        """Close entire position"""
        position = self._get_position(action.position_key)
        if not position:
            return False

        result = await adapter.close_position(position, pct=1.0)
        if result:
            from core.types import PositionState
            position.state = PositionState.CLOSED
            logger.info("full_close_executed", user=action.user_id, symbol=action.symbol)
            return True

        return False

    # ─── Helpers ──────────────────────────────────────────────────────────

    def _get_position(self, position_key: str) -> Position | None:
        """Get position from position manager"""
        if self._position_manager:
            return self._position_manager.get_position(position_key)
        return None

    async def close_all(self) -> None:
        """Close all exchange adapters"""
        for adapter in self._adapters.values():
            await adapter.close()
        self._adapters.clear()

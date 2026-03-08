"""
Agentrade Engine v2 — Multi-Level Trailing TP/SL Strategy

The #1 most requested feature from trading bot clients.

Behavior:
  TP1 hit (e.g. +3%)  → partial close 33% + move SL to breakeven
  TP2 hit (e.g. +5%)  → partial close 33% + move SL to TP1 price
  TP3 hit (e.g. +8%)  → start tight trailing (1-2%)
  After TP3            → trail SL behind price, only moves UP (long) or DOWN (short)

Config-driven: all percentages and modes come from YAML / user settings.
"""

from __future__ import annotations

import structlog

from core.types import (
    Action, ActionType, MultiLevelTrailConfig, Position, Priority,
    SLType, Side,
)
from strategy.base import BaseStrategy

logger = structlog.get_logger(__name__)


class MultiLevelTrailStrategy(BaseStrategy):
    """
    Multi-level trailing TP/SL.

    evaluate() is called on EVERY price tick. Must be pure math, < 0.01ms.
    Returns Action if a threshold is crossed, None otherwise.
    """

    @property
    def name(self) -> str:
        return "multi_level_trail"

    def evaluate(
        self,
        position: Position,
        price: float,
        mark_price: float | None = None,
    ) -> Action | None:
        """
        Core evaluation logic. Called per tick per position.

        Flow:
          1. Update price extremes (highest/lowest since entry)
          2. Check TP3 → TP2 → TP1 (highest first, so we don't skip levels)
          3. If trailing active (post-TP3): calculate new SL, apply smart threshold
          4. Return Action or None
        """
        cfg = MultiLevelTrailConfig.from_dict(position.strategy_config)

        # Update extremes and last price
        position.update_extremes(price, mark_price)

        # Calculate PNL percentage
        pnl_pct = self._calc_pnl_pct(position, price)

        # ── Problem #13: Wick protection ──────────────────────────────────
        # If wick protection is on, we don't trigger SL via this strategy.
        # The exchange-side SL handles it (with confirmation delay).
        # We only MOVE the SL here, never trigger closes based on single ticks.

        # ── Problem #11: Mark price divergence check ─────────────────────
        if mark_price is not None:
            divergence = abs(price - mark_price) / price * 100
            if divergence > 0.5:
                # Large divergence — don't make trailing decisions on bad data
                return Action(
                    type=ActionType.ALERT,
                    position_key=position.position_key,
                    user_id=position.user_id,
                    symbol=position.symbol,
                    exchange=position.exchange,
                    message=f"⚠️ Price divergence {divergence:.2f}% on {position.symbol}. Trailing paused.",
                    priority=Priority.HIGH,
                    reason="mark_last_divergence",
                )

        # ── Check TP levels (highest first) ───────────────────────────────

        # TP3: Start tight trailing
        if not position.tp3_hit and pnl_pct >= cfg.tp3_pct:
            position.tp3_hit = True
            new_sl = self._calc_trailing_sl(position, price, cfg)

            logger.info(
                "tp3_hit",
                user=position.user_id,
                symbol=position.symbol,
                pnl_pct=round(pnl_pct, 2),
                new_sl=round(new_sl, 4),
            )

            return self._make_sl_action(
                position, new_sl, cfg,
                priority=Priority.HIGH,
                reason=f"TP3 hit ({pnl_pct:.1f}%), start trailing at {cfg.trail_pct}%",
            )

        # TP2: Move SL to TP1 price + partial close
        if not position.tp2_hit and pnl_pct >= cfg.tp2_pct:
            position.tp2_hit = True
            tp1_price = self._calc_tp_price(position, cfg.tp1_pct)

            logger.info(
                "tp2_hit",
                user=position.user_id,
                symbol=position.symbol,
                pnl_pct=round(pnl_pct, 2),
                sl_to=round(tp1_price, 4),
            )

            # Partial close at TP2
            if cfg.tp2_close_pct > 0:
                return Action(
                    type=ActionType.PARTIAL_CLOSE,
                    position_key=position.position_key,
                    user_id=position.user_id,
                    symbol=position.symbol,
                    exchange=position.exchange,
                    price=tp1_price,  # New SL after partial close
                    close_pct=cfg.tp2_close_pct,
                    priority=Priority.HIGH,
                    sl_type=cfg.sl_type,
                    sl_buffer_pct=cfg.sl_buffer_pct,
                    reason=f"TP2 hit ({pnl_pct:.1f}%), close {cfg.tp2_close_pct*100:.0f}%, SL->TP1",
                )

            return self._make_sl_action(
                position, tp1_price, cfg,
                priority=Priority.HIGH,
                reason=f"TP2 hit ({pnl_pct:.1f}%), SL moved to TP1 price",
            )

        # TP1: Move SL to breakeven + partial close
        if not position.tp1_hit and pnl_pct >= cfg.tp1_pct:
            position.tp1_hit = True
            breakeven = position.entry_price

            logger.info(
                "tp1_hit",
                user=position.user_id,
                symbol=position.symbol,
                pnl_pct=round(pnl_pct, 2),
                sl_to_breakeven=round(breakeven, 4),
            )

            # Partial close at TP1
            if cfg.tp1_close_pct > 0:
                return Action(
                    type=ActionType.PARTIAL_CLOSE,
                    position_key=position.position_key,
                    user_id=position.user_id,
                    symbol=position.symbol,
                    exchange=position.exchange,
                    price=breakeven,  # New SL after partial close
                    close_pct=cfg.tp1_close_pct,
                    priority=Priority.HIGH,
                    sl_type=cfg.sl_type,
                    sl_buffer_pct=cfg.sl_buffer_pct,
                    reason=f"TP1 hit ({pnl_pct:.1f}%), close {cfg.tp1_close_pct*100:.0f}%, SL->breakeven",
                )

            return self._make_sl_action(
                position, breakeven, cfg,
                priority=Priority.HIGH,
                reason=f"TP1 hit ({pnl_pct:.1f}%), SL moved to breakeven",
            )

        # ── Active trailing (after TP3) ───────────────────────────────────

        if position.tp3_hit:
            new_sl = self._calc_trailing_sl(position, price, cfg)

            # Only move SL in the profitable direction
            if position.side == Side.LONG and new_sl <= position.current_sl:
                return None  # SL would move DOWN for a long — never do that
            if position.side == Side.SHORT and new_sl >= position.current_sl:
                return None  # SL would move UP for a short — never do that

            # Problem #2C: Smart threshold — skip if change is too small
            if position.last_sl_update_price > 0:
                change_pct = abs(new_sl - position.last_sl_update_price) / position.last_sl_update_price * 100
                if change_pct < cfg.min_sl_change_pct:
                    return None  # Change too small, save the API call

            return self._make_sl_action(
                position, new_sl, cfg,
                priority=Priority.NORMAL,
                reason=f"Trailing SL update: {position.current_sl:.4f} -> {new_sl:.4f}",
            )

        # No action needed
        return None

    # ─── On Position Events ───────────────────────────────────────────────

    def on_position_opened(self, position: Position) -> Action | None:
        """Set initial SL based on strategy config"""
        cfg = MultiLevelTrailConfig.from_dict(position.strategy_config)

        # Initial SL: at entry price minus a safety margin
        # For now, no initial SL — user should set it manually or via config
        # This is intentional: some users don't want an initial SL
        return None

    # ─── Helper Methods ───────────────────────────────────────────────────

    @staticmethod
    def _calc_pnl_pct(position: Position, price: float) -> float:
        """Calculate unrealized PNL percentage at current price"""
        if position.side == Side.LONG:
            return ((price - position.entry_price) / position.entry_price) * 100
        else:  # SHORT
            return ((position.entry_price - price) / position.entry_price) * 100

    @staticmethod
    def _calc_tp_price(position: Position, pct: float) -> float:
        """Calculate the price at a given TP percentage"""
        if position.side == Side.LONG:
            return position.entry_price * (1 + pct / 100)
        else:  # SHORT
            return position.entry_price * (1 - pct / 100)

    @staticmethod
    def _calc_trailing_sl(
        position: Position,
        price: float,
        cfg: MultiLevelTrailConfig,
    ) -> float:
        """
        Calculate trailing stop-loss price.

        For LONG: SL = highest_price × (1 - trail%)
        For SHORT: SL = lowest_price × (1 + trail%)

        Uses the EXTREME price (highest for long, lowest for short),
        not the current price. This ensures SL only moves in profitable direction.
        """
        trail_pct = cfg.trail_pct  # TODO: ATR-based mode (Problem #14)

        if position.side == Side.LONG:
            reference = position.highest_since_entry
            return reference * (1 - trail_pct / 100)
        else:  # SHORT
            reference = position.lowest_since_entry
            return reference * (1 + trail_pct / 100)

    @staticmethod
    def _make_sl_action(
        position: Position,
        new_sl: float,
        cfg: MultiLevelTrailConfig,
        priority: Priority = Priority.NORMAL,
        reason: str = "",
    ) -> Action:
        """Build a MOVE_SL action"""
        return Action(
            type=ActionType.MOVE_SL,
            position_key=position.position_key,
            user_id=position.user_id,
            symbol=position.symbol,
            exchange=position.exchange,
            price=new_sl,
            priority=priority,
            sl_type=cfg.sl_type,
            sl_buffer_pct=cfg.sl_buffer_pct,
            reason=reason,
        )

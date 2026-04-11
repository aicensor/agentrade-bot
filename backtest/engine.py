"""
Agentrade Engine v2 — Backtest Engine

Replays historical price data through the same strategy pipeline used in live trading.
Simulates order execution, tracks PnL, drawdown, and generates trade logs.

Key design:
  - Uses the SAME strategy code as live (no separate backtest logic)
  - Simulates partial closes, SL/TP hits at the tick level
  - Supports multiple concurrent trades, long/short, any symbol
  - No async — runs synchronously for speed (1M ticks in ~2s)
"""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass, field
from typing import Any

import structlog

from core.types import (
    Action, ActionType, MultiLevelTrailConfig, Position, PositionState, Side,
)
from strategy.base import BaseStrategy
from strategy.multi_level_trail import MultiLevelTrailStrategy
from backtest.data import Candle, PriceHistory, candles_to_ticks

logger = structlog.get_logger(__name__)


# ─── Trade Record ────────────────────────────────────────────────────────────

@dataclass
class Trade:
    """A completed trade (entry → exit)."""
    trade_id: int
    symbol: str
    side: str               # "long" or "short"
    entry_price: float
    entry_time: float
    exit_price: float = 0.0
    exit_time: float = 0.0
    exit_reason: str = ""
    size: float = 1.0
    initial_size: float = 1.0
    leverage: float = 1.0

    # Partial close tracking
    partial_closes: list[dict] = field(default_factory=list)

    # PnL
    realized_pnl: float = 0.0
    realized_pnl_pct: float = 0.0
    max_favorable: float = 0.0     # Max favorable excursion (MFE)
    max_adverse: float = 0.0       # Max adverse excursion (MAE)

    # TP/SL tracking
    tp1_hit: bool = False
    tp2_hit: bool = False
    tp3_hit: bool = False
    final_sl: float = 0.0

    @property
    def duration_sec(self) -> float:
        return self.exit_time - self.entry_time if self.exit_time else 0

    @property
    def is_winner(self) -> bool:
        return self.realized_pnl > 0


# ─── Backtest Result ─────────────────────────────────────────────────────────

@dataclass
class BacktestResult:
    """Full backtest output with metrics."""
    # Config
    symbol: str
    side: str
    timeframe: str
    strategy_config: dict
    candle_count: int
    tick_count: int

    # Time
    start_time: float = 0.0
    end_time: float = 0.0
    execution_time_sec: float = 0.0

    # Trades
    trades: list[Trade] = field(default_factory=list)
    action_log: list[dict] = field(default_factory=list)

    # Equity curve
    equity_curve: list[tuple[float, float]] = field(default_factory=list)  # (timestamp, equity)

    @property
    def total_trades(self) -> int:
        return len(self.trades)

    @property
    def winners(self) -> list[Trade]:
        return [t for t in self.trades if t.is_winner]

    @property
    def losers(self) -> list[Trade]:
        return [t for t in self.trades if not t.is_winner]

    @property
    def win_rate(self) -> float:
        return len(self.winners) / max(self.total_trades, 1) * 100

    @property
    def total_pnl(self) -> float:
        return sum(t.realized_pnl for t in self.trades)

    @property
    def total_pnl_pct(self) -> float:
        return sum(t.realized_pnl_pct for t in self.trades)

    @property
    def avg_win(self) -> float:
        wins = [t.realized_pnl for t in self.winners]
        return sum(wins) / max(len(wins), 1)

    @property
    def avg_loss(self) -> float:
        losses = [t.realized_pnl for t in self.losers]
        return sum(losses) / max(len(losses), 1)

    @property
    def avg_win_pct(self) -> float:
        wins = [t.realized_pnl_pct for t in self.winners]
        return sum(wins) / max(len(wins), 1)

    @property
    def avg_loss_pct(self) -> float:
        losses = [t.realized_pnl_pct for t in self.losers]
        return sum(losses) / max(len(losses), 1)

    @property
    def profit_factor(self) -> float:
        gross_profit = sum(t.realized_pnl for t in self.winners)
        gross_loss = abs(sum(t.realized_pnl for t in self.losers))
        return gross_profit / max(gross_loss, 0.01)

    @property
    def max_drawdown_pct(self) -> float:
        if not self.equity_curve:
            return 0.0
        peak = self.equity_curve[0][1]
        max_dd = 0.0
        for _, equity in self.equity_curve:
            if equity > peak:
                peak = equity
            dd = (peak - equity) / peak * 100 if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd
        return max_dd

    @property
    def expectancy(self) -> float:
        """Average expected PnL per trade."""
        if not self.trades:
            return 0.0
        wr = len(self.winners) / max(self.total_trades, 1)
        return (wr * self.avg_win) + ((1 - wr) * self.avg_loss)

    @property
    def sharpe_approx(self) -> float:
        """Simplified Sharpe-like ratio (mean return / std dev of returns)."""
        if len(self.trades) < 2:
            return 0.0
        returns = [t.realized_pnl_pct for t in self.trades]
        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / len(returns)
        std = variance ** 0.5
        return mean / std if std > 0 else 0.0

    def summary(self) -> str:
        """Human-readable summary."""
        lines = [
            "=" * 60,
            "  BACKTEST RESULTS",
            "=" * 60,
            f"  Symbol:          {self.symbol}",
            f"  Side:            {self.side}",
            f"  Timeframe:       {self.timeframe}",
            f"  Candles:         {self.candle_count}",
            f"  Ticks processed: {self.tick_count}",
            f"  Execution time:  {self.execution_time_sec:.2f}s",
            "",
            "  ── Trades ───────────────────────────────────────",
            f"  Total trades:    {self.total_trades}",
            f"  Winners:         {len(self.winners)}",
            f"  Losers:          {len(self.losers)}",
            f"  Win rate:        {self.win_rate:.1f}%",
            "",
            "  ── PnL ─────────────────────────────────────────",
            f"  Total PnL:       ${self.total_pnl:.2f}",
            f"  Total PnL %:     {self.total_pnl_pct:.2f}%",
            f"  Avg win:         ${self.avg_win:.2f} ({self.avg_win_pct:.2f}%)",
            f"  Avg loss:        ${self.avg_loss:.2f} ({self.avg_loss_pct:.2f}%)",
            f"  Profit factor:   {self.profit_factor:.2f}",
            f"  Expectancy:      ${self.expectancy:.2f}",
            f"  Sharpe (approx): {self.sharpe_approx:.2f}",
            "",
            "  ── Risk ─────────────────────────────────────────",
            f"  Max drawdown:    {self.max_drawdown_pct:.2f}%",
        ]

        if self.trades:
            best = max(self.trades, key=lambda t: t.realized_pnl_pct)
            worst = min(self.trades, key=lambda t: t.realized_pnl_pct)
            avg_dur = sum(t.duration_sec for t in self.trades) / len(self.trades)

            lines += [
                f"  Best trade:      {best.realized_pnl_pct:+.2f}% (#{best.trade_id})",
                f"  Worst trade:     {worst.realized_pnl_pct:+.2f}% (#{worst.trade_id})",
                f"  Avg duration:    {avg_dur/60:.1f} min",
                "",
                "  ── TP Levels ────────────────────────────────────",
                f"  TP1 hit rate:    {sum(1 for t in self.trades if t.tp1_hit)/max(self.total_trades,1)*100:.0f}%",
                f"  TP2 hit rate:    {sum(1 for t in self.trades if t.tp2_hit)/max(self.total_trades,1)*100:.0f}%",
                f"  TP3 hit rate:    {sum(1 for t in self.trades if t.tp3_hit)/max(self.total_trades,1)*100:.0f}%",
            ]

        # Strategy config
        lines += [
            "",
            "  ── Strategy Config ──────────────────────────────",
        ]
        for k, v in self.strategy_config.items():
            lines.append(f"  {k:20s} {v}")

        lines.append("=" * 60)
        return "\n".join(lines)

    def trade_table(self, max_rows: int = 50) -> str:
        """Formatted trade log table."""
        lines = [
            f"  {'#':>3s}  {'Side':5s}  {'Entry':>10s}  {'Exit':>10s}  "
            f"{'PnL%':>7s}  {'PnL$':>8s}  {'TP1':3s} {'TP2':3s} {'TP3':3s}  {'Reason':20s}",
            "  " + "─" * 85,
        ]

        for t in self.trades[:max_rows]:
            lines.append(
                f"  {t.trade_id:>3d}  {t.side:5s}  ${t.entry_price:>9.4f}  ${t.exit_price:>9.4f}  "
                f"{t.realized_pnl_pct:>+6.2f}%  ${t.realized_pnl:>+7.2f}  "
                f"{'Y' if t.tp1_hit else '.':3s} {'Y' if t.tp2_hit else '.':3s} {'Y' if t.tp3_hit else '.':3s}  "
                f"{t.exit_reason[:20]:20s}"
            )

        if len(self.trades) > max_rows:
            lines.append(f"  ... ({len(self.trades) - max_rows} more trades)")

        return "\n".join(lines)


# ─── Backtest Engine ─────────────────────────────────────────────────────────

class BacktestEngine:
    """
    Replays price history through the strategy engine.

    Synchronous for speed. No event bus, no async —
    just strategy.evaluate() in a tight loop.
    """

    def __init__(
        self,
        strategy_config: dict | None = None,
        position_size: float = 1.0,
        leverage: float = 1.0,
        initial_capital: float = 1000.0,
        slippage_pct: float = 0.05,
        fee_pct: float = 0.035,
        entry_mode: str = "every_candle",
    ):
        """
        Args:
            strategy_config: Override strategy params (merged with defaults)
            position_size: Size per trade in base asset units
            leverage: Position leverage
            initial_capital: Starting capital in USD
            slippage_pct: Simulated slippage on fills (%)
            fee_pct: Trading fee per side (% of notional)
            entry_mode: How entries are generated:
                - "every_candle": Open new trade at every candle open (stress test)
                - "signal": Use entry_signals list passed to run()
                - "single": One trade at the start, trail it
        """
        self.strategy_config = strategy_config or {}
        self.position_size = position_size
        self.leverage = leverage
        self.initial_capital = initial_capital
        self.slippage_pct = slippage_pct
        self.fee_pct = fee_pct
        self.entry_mode = entry_mode

        # Strategy instance (same as live)
        self._strategy = MultiLevelTrailStrategy()

    def run(
        self,
        history: PriceHistory,
        side: str = "short",
        entry_signals: list[int] | None = None,
        tick_mode: str = "ohlc",
    ) -> BacktestResult:
        """
        Run backtest on price history.

        Args:
            history: Price data (candles)
            side: "long" or "short"
            entry_signals: Candle indices where entries occur (for signal mode)
            tick_mode: How to simulate ticks from candles:
                - "ohlc": 4 ticks per candle (open → high → low → close)
                - "close": 1 tick per candle (close price only)

        Returns:
            BacktestResult with all metrics and trade log
        """
        t0 = time.time()
        pos_side = Side.LONG if side == "long" else Side.SHORT

        # Build tick sequence
        if tick_mode == "ohlc":
            ticks = candles_to_ticks(history.candles)
        else:
            ticks = [(c.timestamp / 1000, c.close) for c in history.candles]

        # Entry signal indices (converted to tick indices)
        entry_set: set[int] = set()
        if self.entry_mode == "signal" and entry_signals:
            entry_set = set(entry_signals)
        elif self.entry_mode == "every_candle":
            # Every N candles (avoid overlapping too much)
            interval = max(1, len(history.candles) // 100)
            entry_set = set(range(0, len(history.candles), interval))
        elif self.entry_mode == "single":
            entry_set = {0}

        # State
        active_positions: list[tuple[Position, Trade]] = []
        completed_trades: list[Trade] = []
        action_log: list[dict] = []
        equity = self.initial_capital
        equity_curve: list[tuple[float, float]] = []
        trade_counter = 0
        tick_count = 0
        candle_idx = 0

        # Default strategy config merged with overrides
        default_cfg = {
            "tp1_pct": 3.0,
            "tp2_pct": 5.0,
            "tp3_pct": 8.0,
            "tp1_close_pct": 0.33,
            "tp2_close_pct": 0.33,
            "tp3_close_pct": 0.0,
            "trail_pct": 1.5,
            "min_sl_change_pct": 0.1,
            "sl_type": "limit",
            "sl_buffer_pct": 0.3,
            "wick_protection": False,  # Disabled in backtest (we don't simulate time delays)
        }
        default_cfg.update(self.strategy_config)

        for i, (ts, price) in enumerate(ticks):
            tick_count += 1

            # Track which candle we're in (for entry signals)
            if tick_mode == "ohlc":
                candle_idx = i // 4
            else:
                candle_idx = i

            # ── Check for new entries ────────────────────────────────
            if candle_idx in entry_set and (tick_mode == "close" or i % 4 == 0):
                entry_set.discard(candle_idx)  # Only enter once per signal

                # Apply slippage to entry
                if pos_side == Side.LONG:
                    entry_price = price * (1 + self.slippage_pct / 100)
                else:
                    entry_price = price * (1 - self.slippage_pct / 100)

                trade_counter += 1
                position = Position(
                    user_id="backtest",
                    symbol=history.symbol,
                    side=pos_side,
                    exchange=history.exchange,
                    entry_price=entry_price,
                    size=self.position_size,
                    leverage=self.leverage,
                    state=PositionState.OPEN,
                    strategy_name="multi_level_trail",
                    strategy_config=dict(default_cfg),
                )
                position.highest_since_entry = entry_price
                position.lowest_since_entry = entry_price

                trade = Trade(
                    trade_id=trade_counter,
                    symbol=history.symbol,
                    side=side,
                    entry_price=entry_price,
                    entry_time=ts,
                    size=self.position_size,
                    initial_size=self.position_size,
                    leverage=self.leverage,
                )

                # Entry fee
                fee = entry_price * self.position_size * self.fee_pct / 100
                equity -= fee

                active_positions.append((position, trade))

            # ── Evaluate active positions ────────────────────────────
            closed_indices: list[int] = []

            for idx, (pos, trade) in enumerate(active_positions):
                # Track MFE / MAE
                if pos.side == Side.LONG:
                    pnl_pct = (price - pos.entry_price) / pos.entry_price * 100
                else:
                    pnl_pct = (pos.entry_price - price) / pos.entry_price * 100

                if pnl_pct > trade.max_favorable:
                    trade.max_favorable = pnl_pct
                if pnl_pct < trade.max_adverse:
                    trade.max_adverse = pnl_pct

                # ── Check if SL was hit ──────────────────────────────
                if pos.current_sl > 0:
                    sl_hit = False
                    if pos.side == Side.LONG and price <= pos.current_sl:
                        sl_hit = True
                    elif pos.side == Side.SHORT and price >= pos.current_sl:
                        sl_hit = True

                    if sl_hit:
                        # Close at SL price with slippage
                        if pos.side == Side.LONG:
                            exit_price = pos.current_sl * (1 - self.slippage_pct / 100)
                        else:
                            exit_price = pos.current_sl * (1 + self.slippage_pct / 100)

                        pnl = self._calc_pnl(pos, exit_price)
                        fee = exit_price * pos.size * self.fee_pct / 100
                        pnl -= fee
                        pnl_pct_final = pnl / (pos.entry_price * trade.initial_size) * 100

                        trade.exit_price = exit_price
                        trade.exit_time = ts
                        trade.exit_reason = "sl_hit"
                        trade.realized_pnl = pnl
                        trade.realized_pnl_pct = pnl_pct_final
                        trade.tp1_hit = pos.tp1_hit
                        trade.tp2_hit = pos.tp2_hit
                        trade.tp3_hit = pos.tp3_hit
                        trade.final_sl = pos.current_sl

                        equity += pnl
                        completed_trades.append(trade)
                        closed_indices.append(idx)

                        action_log.append({
                            "tick": tick_count,
                            "time": ts,
                            "type": "sl_hit",
                            "trade_id": trade.trade_id,
                            "price": exit_price,
                            "pnl": round(pnl, 4),
                        })
                        continue

                # ── Run strategy evaluation ──────────────────────────
                action = self._strategy.evaluate(pos, price, mark_price=price)

                if action is None:
                    continue

                # Process action
                if action.type == ActionType.MOVE_SL:
                    pos.current_sl = action.price
                    pos.last_sl_update_price = action.price

                    action_log.append({
                        "tick": tick_count,
                        "time": ts,
                        "type": "move_sl",
                        "trade_id": trade.trade_id,
                        "price": action.price,
                        "reason": action.reason,
                    })

                elif action.type == ActionType.PARTIAL_CLOSE:
                    close_size = pos.size * action.close_pct

                    # PnL for the partial close
                    if pos.side == Side.LONG:
                        exit_price = price * (1 - self.slippage_pct / 100)
                    else:
                        exit_price = price * (1 + self.slippage_pct / 100)

                    partial_pnl = self._calc_pnl_for_size(pos, exit_price, close_size)
                    fee = exit_price * close_size * self.fee_pct / 100
                    partial_pnl -= fee
                    equity += partial_pnl
                    trade.realized_pnl += partial_pnl

                    trade.partial_closes.append({
                        "pct": action.close_pct,
                        "size": close_size,
                        "price": exit_price,
                        "pnl": partial_pnl,
                    })

                    pos.size -= close_size
                    pos.current_sl = action.price
                    pos.last_sl_update_price = action.price

                    action_log.append({
                        "tick": tick_count,
                        "time": ts,
                        "type": "partial_close",
                        "trade_id": trade.trade_id,
                        "close_pct": action.close_pct,
                        "close_size": close_size,
                        "price": exit_price,
                        "pnl": round(partial_pnl, 4),
                        "new_sl": action.price,
                        "reason": action.reason,
                    })

                    # If position fully closed by partials
                    if pos.size < 0.0001:
                        pnl_pct_final = trade.realized_pnl / (pos.entry_price * trade.initial_size) * 100
                        trade.exit_price = exit_price
                        trade.exit_time = ts
                        trade.exit_reason = "partial_close_complete"
                        trade.realized_pnl_pct = pnl_pct_final
                        trade.tp1_hit = pos.tp1_hit
                        trade.tp2_hit = pos.tp2_hit
                        trade.tp3_hit = pos.tp3_hit
                        trade.final_sl = pos.current_sl
                        completed_trades.append(trade)
                        closed_indices.append(idx)

                elif action.type == ActionType.ALERT:
                    action_log.append({
                        "tick": tick_count,
                        "time": ts,
                        "type": "alert",
                        "trade_id": trade.trade_id,
                        "message": action.message,
                    })

            # Remove closed positions (reverse order to preserve indices)
            for idx in sorted(closed_indices, reverse=True):
                active_positions.pop(idx)

            # Record equity curve (sample every 10 ticks to save memory)
            if tick_count % 10 == 0:
                # Include unrealized PnL of open positions
                unrealized = sum(
                    self._calc_pnl(pos, price)
                    for pos, _ in active_positions
                )
                equity_curve.append((ts, equity + unrealized))

        # ── Close any remaining open positions at last price ──────────
        if ticks:
            last_ts, last_price = ticks[-1]
            for pos, trade in active_positions:
                if pos.side == Side.LONG:
                    exit_price = last_price * (1 - self.slippage_pct / 100)
                else:
                    exit_price = last_price * (1 + self.slippage_pct / 100)

                remaining_pnl = self._calc_pnl(pos, exit_price)
                fee = exit_price * pos.size * self.fee_pct / 100
                remaining_pnl -= fee
                trade.realized_pnl += remaining_pnl
                pnl_pct_final = trade.realized_pnl / (pos.entry_price * trade.initial_size) * 100

                trade.exit_price = exit_price
                trade.exit_time = last_ts
                trade.exit_reason = "backtest_end"
                trade.realized_pnl_pct = pnl_pct_final
                trade.tp1_hit = pos.tp1_hit
                trade.tp2_hit = pos.tp2_hit
                trade.tp3_hit = pos.tp3_hit
                trade.final_sl = pos.current_sl

                equity += remaining_pnl
                completed_trades.append(trade)

        execution_time = time.time() - t0

        result = BacktestResult(
            symbol=history.symbol,
            side=side,
            timeframe=history.timeframe,
            strategy_config=default_cfg,
            candle_count=len(history.candles),
            tick_count=tick_count,
            start_time=history.start_time / 1000 if history.candles else 0,
            end_time=history.end_time / 1000 if history.candles else 0,
            execution_time_sec=execution_time,
            trades=completed_trades,
            action_log=action_log,
            equity_curve=equity_curve,
        )

        return result

    # ─── PnL Helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _calc_pnl(position: Position, exit_price: float) -> float:
        """Calculate PnL for remaining position size."""
        if position.side == Side.LONG:
            return (exit_price - position.entry_price) * position.size
        else:
            return (position.entry_price - exit_price) * position.size

    @staticmethod
    def _calc_pnl_for_size(position: Position, exit_price: float, size: float) -> float:
        """Calculate PnL for a specific size."""
        if position.side == Side.LONG:
            return (exit_price - position.entry_price) * size
        else:
            return (position.entry_price - exit_price) * size

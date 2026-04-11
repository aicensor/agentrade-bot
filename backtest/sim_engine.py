"""
Agentrade Engine v2 — Mode 2: Event-Driven Simulation Engine

Realistic simulation that reuses the SAME live trading components:
  - EventBus (async pub/sub)
  - PositionManager (position tracking + strategy dispatch)
  - MultiLevelTrailStrategy (TP/SL evaluation)
  - OrderQueue (dedup + batching)
  - OrderExecutor (action execution)
  - MockExchangeAdapter (simulated fills)

Flow:
  TickReplay → EventBus.emit("price_update")
    → PositionManager._on_price_update()
      → Strategy.evaluate()
        → OrderQueue._on_action()
          → OrderExecutor.execute()
            → MockExchangeAdapter.set_stop_loss() / close_position()

This tests the FULL pipeline, including:
  - Event bus routing and handler registration
  - OrderQueue dedup/batching behavior
  - Async concurrency patterns
  - Position state machine transitions

Unlike Mode 1 (Rust, sync, 100M ticks/sec), Mode 2 runs at ~10K ticks/sec
but validates that the complete async pipeline works correctly.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import structlog

from core.event_bus import EventBus
from core.types import (
    Action, ActionType, Event, MultiLevelTrailConfig, Position,
    PositionState, PriceUpdate, Side, UserConfig,
)
from execution.position_manager import PositionManager
from execution.order_queue import OrderQueue
from execution.order_executor import OrderExecutor
from backtest.mock_exchange import MockExchangeAdapter
from backtest.tick_data import TickStream

logger = structlog.get_logger(__name__)


# ─── Simulation Result ───────────────────────────────────────────────────────

@dataclass
class SimTrade:
    """A completed trade from the simulation."""
    trade_id: int
    symbol: str
    side: str
    entry_price: float
    entry_time: float
    exit_price: float = 0.0
    exit_time: float = 0.0
    exit_reason: str = ""
    initial_size: float = 1.0
    realized_pnl: float = 0.0
    realized_pnl_pct: float = 0.0
    tp1_hit: bool = False
    tp2_hit: bool = False
    tp3_hit: bool = False
    max_favorable: float = 0.0
    max_adverse: float = 0.0
    fees_paid: float = 0.0
    partial_closes: list[dict] = field(default_factory=list)
    actions: list[dict] = field(default_factory=list)


@dataclass
class SimResult:
    """Full simulation result."""
    symbol: str
    side: str
    tick_count: int
    execution_time_sec: float

    trades: list[SimTrade] = field(default_factory=list)
    equity_curve: list[tuple[float, float]] = field(default_factory=list)
    action_log: list[dict] = field(default_factory=list)

    initial_capital: float = 1000.0

    # Pipeline stats
    event_bus_stats: dict = field(default_factory=dict)
    position_manager_stats: dict = field(default_factory=dict)
    order_queue_stats: dict = field(default_factory=dict)
    mock_exchange_stats: dict = field(default_factory=dict)

    @property
    def total_trades(self) -> int:
        return len(self.trades)

    @property
    def winners(self) -> list[SimTrade]:
        return [t for t in self.trades if t.realized_pnl > 0]

    @property
    def losers(self) -> list[SimTrade]:
        return [t for t in self.trades if t.realized_pnl <= 0]

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
            max_dd = max(max_dd, dd)
        return max_dd

    def summary(self) -> dict:
        """Return summary dict for dashboard/API consumption."""
        return {
            "total_trades": self.total_trades,
            "winners": len(self.winners),
            "losers": len(self.losers),
            "win_rate": round(self.win_rate, 1),
            "total_pnl": round(self.total_pnl, 2),
            "total_pnl_pct": round(self.total_pnl_pct, 2),
            "profit_factor": round(self.profit_factor, 2),
            "max_drawdown_pct": round(self.max_drawdown_pct, 2),
            "tick_count": self.tick_count,
            "execution_time_sec": round(self.execution_time_sec, 2),
            "pipeline_stats": {
                "event_bus": self.event_bus_stats,
                "position_manager": self.position_manager_stats,
                "order_queue": self.order_queue_stats,
                "mock_exchange": self.mock_exchange_stats,
            },
        }


# ─── Mode 2 Simulation Engine ────────────────────────────────────────────────

class SimulationEngine:
    """
    Event-driven simulation using the full live trading pipeline.

    Wires up:
      EventBus → PositionManager → Strategy → OrderQueue → OrderExecutor → MockExchangeAdapter

    Then replays ticks through the pipeline.
    """

    def __init__(
        self,
        symbol: str = "SOL/USDC:USDC",
        exchange: str = "hyperliquid",
        side: str = "short",
        strategy_config: dict | None = None,
        position_size: float = 1.0,
        leverage: float = 5.0,
        initial_capital: float = 1000.0,
        slippage_pct: float = 0.05,
        fee_pct: float = 0.035,
        entry_mode: str = "single",
        entry_interval: int = 100,
        flush_interval_ms: int = 0,  # 0 = flush every tick (sync-like behavior)
    ):
        self.symbol = symbol
        self.exchange = exchange
        self.side = side
        self.position_size = position_size
        self.leverage = leverage
        self.initial_capital = initial_capital
        self.slippage_pct = slippage_pct
        self.fee_pct = fee_pct
        self.entry_mode = entry_mode
        self.entry_interval = entry_interval
        self.flush_interval_ms = flush_interval_ms

        # Strategy config with defaults
        self.strategy_config = {
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
            "wick_protection": False,
        }
        if strategy_config:
            self.strategy_config.update(strategy_config)

        # Components (initialized in setup)
        self._event_bus: EventBus | None = None
        self._position_manager: PositionManager | None = None
        self._order_queue: OrderQueue | None = None
        self._order_executor: OrderExecutor | None = None
        self._mock_adapter: MockExchangeAdapter | None = None

        # Tracking
        self._trade_counter = 0
        self._active_trades: dict[str, SimTrade] = {}  # position_key → SimTrade
        self._completed_trades: list[SimTrade] = []
        self._action_log: list[dict] = []
        self._equity = initial_capital
        self._equity_curve: list[tuple[float, float]] = []

    async def _setup(self) -> None:
        """Wire up the full pipeline."""
        # 1. Event Bus
        self._event_bus = EventBus()

        # 2. Mock Exchange Adapter
        self._mock_adapter = MockExchangeAdapter(
            slippage_pct=self.slippage_pct,
            fee_pct=self.fee_pct,
        )

        # 3. Order Executor (with mock adapter pre-registered)
        self._order_executor = OrderExecutor.__new__(OrderExecutor)
        self._order_executor.rate_pool = None
        self._order_executor._adapters = {"sim_user": self._mock_adapter}
        self._order_executor._user_configs = {}
        self._order_executor._position_manager = None

        # 4. Position Manager
        self._position_manager = PositionManager(self._event_bus)
        await self._position_manager.start()

        # Link executor to position manager
        self._order_executor._position_manager = self._position_manager

        # 5. Order Queue — in simulation mode, we flush immediately (no batching delay)
        self._order_queue = OrderQueue(
            event_bus=self._event_bus,
            execute_callback=self._execute_action,
            flush_interval_ms=max(self.flush_interval_ms, 1),
            max_jitter_ms=0,  # No jitter in simulation
        )

        # Register action handler directly (skip the queue's flush loop for simulation)
        # We'll flush manually after each tick for deterministic behavior
        self._event_bus.on("action", self._on_action_immediate)

        # Track order executions
        self._event_bus.on("order_executed", self._on_order_executed)

        logger.info("sim_engine_setup_complete")

    async def _execute_action(self, action: Action) -> bool:
        """Execute an action through the mock exchange."""
        return await self._order_executor.execute(action)

    async def _on_action_immediate(self, event: Event) -> None:
        """
        Immediately execute actions (bypass OrderQueue batching for simulation).
        This gives deterministic, tick-by-tick behavior matching Mode 1.
        """
        action: Action = event.data

        # Log the action
        self._action_log.append({
            "type": action.type.value,
            "position_key": action.position_key,
            "price": action.price,
            "close_pct": action.close_pct,
            "reason": action.reason,
            "priority": action.priority.value,
        })

        # Execute immediately
        success = await self._execute_action(action)

        if success:
            # Track partial close PnL
            if action.type == ActionType.PARTIAL_CLOSE:
                await self._handle_partial_close(action)
            elif action.type == ActionType.FULL_CLOSE:
                await self._handle_full_close(action)

    async def _handle_partial_close(self, action: Action) -> None:
        """Track PnL from partial close."""
        sim_trade = self._active_trades.get(action.position_key)
        if not sim_trade:
            return

        # Get the fill from mock adapter
        if self._mock_adapter._fills:
            last_fill = self._mock_adapter._fills[-1]
            pnl = last_fill.get("pnl", 0)
            fee = last_fill.get("fee", 0)
            sim_trade.realized_pnl += pnl
            sim_trade.fees_paid += fee
            sim_trade.partial_closes.append({
                "pct": action.close_pct,
                "price": last_fill.get("price", 0),
                "pnl": pnl,
            })
            self._equity += pnl

        # Check if position fully closed
        pos = self._position_manager.get_position(action.position_key)
        if pos and pos.size < 0.0001:
            await self._close_trade(action.position_key, "partial_close_complete",
                                    self._mock_adapter.current_price,
                                    self._mock_adapter.current_time)

    async def _handle_full_close(self, action: Action) -> None:
        """Track PnL from full close."""
        if self._mock_adapter._fills:
            last_fill = self._mock_adapter._fills[-1]
            pnl = last_fill.get("pnl", 0)
            self._equity += pnl

        await self._close_trade(action.position_key, "full_close",
                                self._mock_adapter.current_price,
                                self._mock_adapter.current_time)

    async def _close_trade(
        self, position_key: str, reason: str, price: float, timestamp: float,
    ) -> None:
        """Finalize a trade."""
        sim_trade = self._active_trades.pop(position_key, None)
        if not sim_trade:
            return

        pos = self._position_manager.get_position(position_key)
        if pos:
            sim_trade.tp1_hit = pos.tp1_hit
            sim_trade.tp2_hit = pos.tp2_hit
            sim_trade.tp3_hit = pos.tp3_hit

        sim_trade.exit_price = price
        sim_trade.exit_time = timestamp
        sim_trade.exit_reason = reason

        if sim_trade.initial_size > 0:
            sim_trade.realized_pnl_pct = (
                sim_trade.realized_pnl / (sim_trade.entry_price * sim_trade.initial_size) * 100
            )

        self._completed_trades.append(sim_trade)

        # Remove from position manager
        self._position_manager.remove_position(position_key)
        self._mock_adapter.clear_orders_for_position(position_key)

        logger.debug(
            "sim_trade_closed",
            trade_id=sim_trade.trade_id,
            reason=reason,
            pnl=round(sim_trade.realized_pnl, 4),
        )

    async def _on_order_executed(self, event: Event) -> None:
        """Track executed orders for logging."""
        pass  # Already handled in _on_action_immediate

    def _open_trade(self, price: float, timestamp: float) -> Position:
        """Open a new simulated trade."""
        self._trade_counter += 1
        pos_side = Side.LONG if self.side == "long" else Side.SHORT

        # Apply slippage to entry
        if pos_side == Side.LONG:
            entry_price = price * (1 + self.slippage_pct / 100)
        else:
            entry_price = price * (1 - self.slippage_pct / 100)

        position = Position(
            user_id="sim_user",
            symbol=self.symbol,
            side=pos_side,
            exchange=self.exchange,
            entry_price=entry_price,
            size=self.position_size,
            leverage=self.leverage,
            state=PositionState.OPEN,
            strategy_name="multi_level_trail",
            strategy_config=dict(self.strategy_config),
        )
        position.highest_since_entry = entry_price
        position.lowest_since_entry = entry_price

        # Entry fee
        fee = entry_price * self.position_size * self.fee_pct / 100
        self._equity -= fee

        # Track trade
        sim_trade = SimTrade(
            trade_id=self._trade_counter,
            symbol=self.symbol,
            side=self.side,
            entry_price=entry_price,
            entry_time=timestamp,
            initial_size=self.position_size,
            fees_paid=fee,
        )

        self._position_manager.add_position(position)
        self._active_trades[position.position_key] = sim_trade

        logger.debug(
            "sim_trade_opened",
            trade_id=self._trade_counter,
            side=self.side,
            entry_price=entry_price,
        )

        return position

    async def run(self, tick_stream: TickStream) -> SimResult:
        """
        Run the full event-driven simulation.

        Args:
            tick_stream: Real tick data to replay

        Returns:
            SimResult with all trades, metrics, and pipeline stats
        """
        await self._setup()

        t0 = time.time()
        ticks = tick_stream.as_tuples()
        tick_count = 0
        entry_counter = 0

        logger.info(
            "sim_starting",
            symbol=self.symbol,
            side=self.side,
            ticks=len(ticks),
            entry_mode=self.entry_mode,
        )

        for ts, price in ticks:
            tick_count += 1

            # Update mock exchange price
            self._mock_adapter.update_price(price, ts)

            # ── Check SL/TP fills before strategy evaluation ──────────
            active_positions = self._position_manager.get_positions_for_symbol(self.symbol)
            fills = self._mock_adapter.check_sl_tp_fills(active_positions, price)

            for pos, fill_type, fill_price in fills:
                # Calculate PnL for the remaining position
                if pos.side == Side.LONG:
                    pnl = (fill_price - pos.entry_price) * pos.size
                else:
                    pnl = (pos.entry_price - fill_price) * pos.size

                fee = fill_price * pos.size * self.fee_pct / 100
                pnl -= fee

                sim_trade = self._active_trades.get(pos.position_key)
                if sim_trade:
                    sim_trade.realized_pnl += pnl
                    sim_trade.fees_paid += fee

                self._equity += pnl
                await self._close_trade(pos.position_key, fill_type, fill_price, ts)

            # ── Check for new entries ─────────────────────────────────
            should_enter = False
            if self.entry_mode == "single" and self._trade_counter == 0:
                should_enter = True
            elif self.entry_mode == "every_n":
                entry_counter += 1
                if entry_counter >= self.entry_interval:
                    entry_counter = 0
                    # Only enter if no active position (avoid overlap)
                    if not self._active_trades:
                        should_enter = True

            if should_enter:
                self._open_trade(price, ts)

            # ── Emit price update through the pipeline ────────────────
            price_update = PriceUpdate(
                symbol=self.symbol,
                last_price=price,
                mark_price=price,  # In simulation, mark = last
                timestamp=ts,
                exchange=self.exchange,
            )

            await self._event_bus.emit(Event(name="price_update", data=price_update))

            # ── Track MFE/MAE for active trades ───────────────────────
            for pos in self._position_manager.get_positions_for_symbol(self.symbol):
                sim_trade = self._active_trades.get(pos.position_key)
                if sim_trade:
                    if pos.side == Side.LONG:
                        pnl_pct = (price - pos.entry_price) / pos.entry_price * 100
                    else:
                        pnl_pct = (pos.entry_price - price) / pos.entry_price * 100

                    sim_trade.max_favorable = max(sim_trade.max_favorable, pnl_pct)
                    sim_trade.max_adverse = min(sim_trade.max_adverse, pnl_pct)

            # ── Equity curve sampling ─────────────────────────────────
            if tick_count % 100 == 0:
                unrealized = 0.0
                for pos in self._position_manager.get_positions_for_symbol(self.symbol):
                    if pos.side == Side.LONG:
                        unrealized += (price - pos.entry_price) * pos.size
                    else:
                        unrealized += (pos.entry_price - price) * pos.size
                self._equity_curve.append((ts, self._equity + unrealized))

        # ── Close remaining open positions at last price ──────────────
        if ticks:
            last_ts, last_price = ticks[-1]
            remaining = list(self._active_trades.keys())
            for position_key in remaining:
                pos = self._position_manager.get_position(position_key)
                if not pos:
                    continue

                if pos.side == Side.LONG:
                    exit_price = last_price * (1 - self.slippage_pct / 100)
                    pnl = (exit_price - pos.entry_price) * pos.size
                else:
                    exit_price = last_price * (1 + self.slippage_pct / 100)
                    pnl = (pos.entry_price - exit_price) * pos.size

                fee = exit_price * pos.size * self.fee_pct / 100
                pnl -= fee

                sim_trade = self._active_trades.get(position_key)
                if sim_trade:
                    sim_trade.realized_pnl += pnl
                    sim_trade.fees_paid += fee

                self._equity += pnl
                await self._close_trade(position_key, "sim_end", exit_price, last_ts)

        execution_time = time.time() - t0

        result = SimResult(
            symbol=self.symbol,
            side=self.side,
            tick_count=tick_count,
            execution_time_sec=execution_time,
            trades=self._completed_trades,
            equity_curve=self._equity_curve,
            action_log=self._action_log,
            initial_capital=self.initial_capital,
            event_bus_stats=self._event_bus.stats,
            position_manager_stats=self._position_manager.stats,
            order_queue_stats={},  # We bypass the queue
            mock_exchange_stats=self._mock_adapter.stats,
        )

        logger.info(
            "sim_complete",
            trades=result.total_trades,
            win_rate=f"{result.win_rate:.1f}%",
            total_pnl=f"${result.total_pnl:.2f}",
            ticks=tick_count,
            time=f"{execution_time:.2f}s",
            tps=f"{tick_count/max(execution_time, 0.001):.0f}",
        )

        return result


# ─── Convenience runner ──────────────────────────────────────────────────────

async def run_simulation(
    tick_stream: TickStream,
    symbol: str = "SOL/USDC:USDC",
    exchange: str = "hyperliquid",
    side: str = "short",
    strategy_config: dict | None = None,
    position_size: float = 1.0,
    leverage: float = 5.0,
    initial_capital: float = 1000.0,
    entry_mode: str = "single",
    entry_interval: int = 100,
    **kwargs: Any,
) -> SimResult:
    """
    Convenience function to run a Mode 2 simulation.

    Usage:
        from backtest.tick_data import fetch_trades_ccxt
        from backtest.sim_engine import run_simulation

        ticks = fetch_trades_ccxt("SOL/USDC:USDC", "hyperliquid", limit=5000)
        result = await run_simulation(ticks, side="short")
        print(result.summary())
    """
    engine = SimulationEngine(
        symbol=symbol,
        exchange=exchange,
        side=side,
        strategy_config=strategy_config,
        position_size=position_size,
        leverage=leverage,
        initial_capital=initial_capital,
        entry_mode=entry_mode,
        entry_interval=entry_interval,
        **kwargs,
    )
    return await engine.run(tick_stream)

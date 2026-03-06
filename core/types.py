"""
Agentrade Engine v2 — Core Type Definitions

All dataclasses used across the engine. Pure data, no logic.
These are the building blocks every module depends on.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any


# ─── Enums ────────────────────────────────────────────────────────────────────


class Side(str, Enum):
    LONG = "long"
    SHORT = "short"


class PositionState(str, Enum):
    """State machine for position lifecycle (Problem #6)"""
    PENDING = "pending"          # Order placed, not yet filled
    OPEN = "open"                # Filled, no trailing active yet
    TRAILING = "trailing"        # Trailing TP/SL active
    CLOSING = "closing"          # Close order sent
    CLOSED = "closed"            # Fully closed


class ActionType(str, Enum):
    MOVE_SL = "move_sl"                  # Update stop-loss price
    MOVE_TP = "move_tp"                  # Update take-profit price
    PARTIAL_CLOSE = "partial_close"      # Close portion of position
    FULL_CLOSE = "full_close"            # Close entire position
    ALERT = "alert"                      # Send notification only


class Priority(int, Enum):
    """Order execution priority (Problem #2)"""
    CRITICAL = 0      # Near liquidation, emergency close
    HIGH = 1          # TP hit, move SL to breakeven
    NORMAL = 2        # Trailing SL update
    LOW = 3           # Initial TP/SL setup, info alerts


class TrailMode(str, Enum):
    PERCENT = "percent"      # Fixed percentage trailing
    ATR = "atr"              # ATR-based adaptive trailing (Problem #14)


class SLType(str, Enum):
    MARKET = "market"        # Market stop-loss (default exchange behavior)
    LIMIT = "limit"          # Limit stop-loss with buffer (Problem #9)


# ─── Price Data ───────────────────────────────────────────────────────────────


@dataclass
class PriceUpdate:
    """Emitted by PriceFeed on every WebSocket tick"""
    symbol: str
    last_price: float
    mark_price: float | None = None    # Problem #11: track both
    bid: float | None = None
    ask: float | None = None
    timestamp: float = field(default_factory=time.time)
    exchange: str = ""


@dataclass
class OrderBookSnapshot:
    """For liquidity checks (Problem #15)"""
    symbol: str
    bids: list[tuple[float, float]] = field(default_factory=list)  # [(price, qty), ...]
    asks: list[tuple[float, float]] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

    @property
    def bid_depth_usd(self) -> float:
        """Total bid liquidity in USD (top 10 levels)"""
        return sum(p * q for p, q in self.bids[:10])

    @property
    def ask_depth_usd(self) -> float:
        """Total ask liquidity in USD (top 10 levels)"""
        return sum(p * q for p, q in self.asks[:10])


# ─── Position ─────────────────────────────────────────────────────────────────


@dataclass
class Position:
    """
    Core position state. Lives in-memory, indexed by (user_id, symbol, side).
    This is the heart of the trailing engine.
    """
    # Identity
    user_id: str
    symbol: str
    side: Side
    exchange: str

    # Position data
    entry_price: float
    size: float
    leverage: float = 1.0
    margin_mode: str = "isolated"       # "isolated" or "cross"

    # Current exchange-side orders
    current_sl: float = 0.0
    current_tp: float = 0.0
    sl_order_id: str = ""
    tp_order_id: str = ""

    # State machine (Problem #6)
    state: PositionState = PositionState.OPEN

    # Multi-level trailing state
    tp1_hit: bool = False
    tp2_hit: bool = False
    tp3_hit: bool = False

    # Trailing tracking
    highest_since_entry: float = 0.0    # For long trailing
    lowest_since_entry: float = float('inf')  # For short trailing

    # Risk data (Problem #10)
    liquidation_price: float = 0.0

    # Funding tracking (Problem #12)
    cumulative_funding: float = 0.0

    # Strategy reference
    strategy_name: str = "multi_level_trail"
    strategy_config: dict = field(default_factory=dict)

    # Metadata
    opened_at: float = field(default_factory=time.time)
    last_updated: float = field(default_factory=time.time)
    last_sl_update_price: float = 0.0   # For smart threshold (Problem #2C)

    def update_extremes(self, price: float) -> None:
        """Track highest/lowest price since entry for trailing calculation"""
        if price > self.highest_since_entry:
            self.highest_since_entry = price
        if price < self.lowest_since_entry:
            self.lowest_since_entry = price
        self.last_updated = time.time()

    @property
    def position_key(self) -> str:
        return f"{self.user_id}:{self.symbol}:{self.side.value}"

    @property
    def unrealized_pnl_pct(self) -> float:
        """Calculate unrealized PNL percentage based on current extremes"""
        if self.side == Side.LONG:
            return ((self.highest_since_entry - self.entry_price) / self.entry_price) * 100
        else:
            return ((self.entry_price - self.lowest_since_entry) / self.entry_price) * 100

    def to_dict(self) -> dict:
        """Serialize for Redis/MongoDB persistence (Problem #3)"""
        return {
            "user_id": self.user_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "exchange": self.exchange,
            "entry_price": self.entry_price,
            "size": self.size,
            "leverage": self.leverage,
            "margin_mode": self.margin_mode,
            "current_sl": self.current_sl,
            "current_tp": self.current_tp,
            "sl_order_id": self.sl_order_id,
            "tp_order_id": self.tp_order_id,
            "state": self.state.value,
            "tp1_hit": self.tp1_hit,
            "tp2_hit": self.tp2_hit,
            "tp3_hit": self.tp3_hit,
            "highest_since_entry": self.highest_since_entry,
            "lowest_since_entry": self.lowest_since_entry,
            "liquidation_price": self.liquidation_price,
            "cumulative_funding": self.cumulative_funding,
            "strategy_name": self.strategy_name,
            "strategy_config": self.strategy_config,
            "opened_at": self.opened_at,
            "last_updated": self.last_updated,
            "last_sl_update_price": self.last_sl_update_price,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Position:
        """Deserialize from Redis/MongoDB (Problem #3)"""
        data["side"] = Side(data["side"])
        data["state"] = PositionState(data["state"])
        if data["lowest_since_entry"] is None:
            data["lowest_since_entry"] = float('inf')
        return cls(**data)


# ─── Actions (Strategy Output) ───────────────────────────────────────────────


@dataclass
class Action:
    """
    Output of Strategy.evaluate(). Tells the execution layer what to do.
    Pure data — no side effects.
    """
    type: ActionType
    position_key: str
    user_id: str
    symbol: str
    exchange: str

    # For MOVE_SL / MOVE_TP
    price: float = 0.0

    # For PARTIAL_CLOSE
    close_pct: float = 0.0           # 0.0-1.0 (e.g., 0.33 = close 33%)

    # For ALERT
    message: str = ""

    # Execution metadata
    priority: Priority = Priority.NORMAL
    sl_type: SLType = SLType.MARKET
    sl_buffer_pct: float = 0.0       # Problem #9: limit SL buffer

    # Tracking
    created_at: float = field(default_factory=time.time)
    reason: str = ""                 # Human-readable reason for logging


# ─── User / Credentials ──────────────────────────────────────────────────────


@dataclass
class UserConfig:
    """Per-user configuration. Loaded from DB, cached in memory."""
    user_id: str
    exchange: str
    api_key: str
    api_secret: str
    passphrase: str = ""             # OKX requires this

    # Risk limits (Problem #10)
    max_leverage: float = 10.0
    require_isolated: bool = True

    # Notification
    telegram_chat_id: int = 0

    # Strategy defaults (can be overridden per position)
    default_strategy: str = "multi_level_trail"
    default_strategy_config: dict = field(default_factory=dict)


# ─── Strategy Config ──────────────────────────────────────────────────────────


@dataclass
class MultiLevelTrailConfig:
    """Configuration for the multi-level trailing strategy"""
    # Take-profit levels (percentage from entry)
    tp1_pct: float = 3.0
    tp2_pct: float = 5.0
    tp3_pct: float = 8.0

    # Partial close percentages at each TP level
    tp1_close_pct: float = 0.33      # Close 33% at TP1
    tp2_close_pct: float = 0.33      # Close 33% at TP2
    tp3_close_pct: float = 0.0       # Don't close at TP3, just trail

    # Trailing after TP3
    trail_mode: TrailMode = TrailMode.PERCENT
    trail_pct: float = 1.5           # Fixed % trail
    atr_period: int = 14             # ATR lookback
    atr_multiplier: float = 2.0      # ATR × multiplier = trail distance

    # Smart threshold — minimum SL change to trigger API call (Problem #2C)
    min_sl_change_pct: float = 0.1

    # Slippage protection (Problem #9)
    sl_type: SLType = SLType.LIMIT
    sl_buffer_pct: float = 0.3       # Limit SL placed 0.3% beyond trigger

    # Wick protection (Problem #13)
    wick_protection: bool = True
    confirmation_seconds: float = 3.0

    # Funding drain protection (Problem #12)
    max_funding_drain_pct: float = 5.0

    @classmethod
    def from_dict(cls, data: dict) -> MultiLevelTrailConfig:
        cfg = cls()
        for key, value in data.items():
            if key == "trail_mode":
                value = TrailMode(value)
            elif key == "sl_type":
                value = SLType(value)
            if hasattr(cfg, key):
                setattr(cfg, key, value)
        return cfg


# ─── Events ──────────────────────────────────────────────────────────────────


@dataclass
class Event:
    """Base event for the event bus"""
    name: str
    data: Any = None
    timestamp: float = field(default_factory=time.time)


# Convenience event constructors
def price_event(update: PriceUpdate) -> Event:
    return Event(name="price_update", data=update)


def position_event(position: Position, action: str) -> Event:
    return Event(name=f"position_{action}", data=position)


def order_event(action: Action, status: str) -> Event:
    return Event(name=f"order_{status}", data=action)


def alert_event(user_id: str, message: str, level: str = "info") -> Event:
    return Event(name="alert", data={"user_id": user_id, "message": message, "level": level})

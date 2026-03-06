"""Quick test — verify all imports and strategy logic."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from core.types import (
    Position, Side, PositionState, Action, ActionType, Priority,
    PriceUpdate, MultiLevelTrailConfig, UserConfig, Event, SLType, TrailMode
)
from core.event_bus import EventBus
from exchange.rate_limiter import RateLimiter, RateLimiterPool
from strategy.base import BaseStrategy
from strategy.multi_level_trail import MultiLevelTrailStrategy
from execution.position_manager import PositionManager
from execution.order_queue import OrderQueue
from execution.order_executor import OrderExecutor
from persistence.state_store import StateStore
from notification.telegram import TelegramNotifier
import yaml

print("=== ALL IMPORTS OK ===\n")

# --- Position (your actual SOL short) ---
pos = Position(
    user_id="bill", symbol="SOL/USDT:USDT", side=Side.SHORT,
    exchange="bybit", entry_price=90.83, size=22.7, leverage=20.0,
    strategy_config={
        "tp1_pct": 3.0, "tp2_pct": 5.0, "tp3_pct": 8.0,
        "trail_pct": 1.5, "min_sl_change_pct": 0.1,
        "sl_type": "limit", "sl_buffer_pct": 0.3,
    }
)
pos.highest_since_entry = 90.83
pos.lowest_since_entry = 90.83

print(f"Position: {pos.position_key}")
print(f"  SHORT SOL @ $90.83, 22.7 SOL, 20x\n")

# --- Strategy test ---
strategy = MultiLevelTrailStrategy()

tp1 = 90.83 * (1 - 3.0 / 100)
tp2 = 90.83 * (1 - 5.0 / 100)
tp3 = 90.83 * (1 - 8.0 / 100)
print(f"TP1(3%)=${tp1:.2f} | TP2(5%)=${tp2:.2f} | TP3(8%)=${tp3:.2f}\n")

# Simulate price drops
prices = [
    (90.00, "small profit"),
    (88.00, "past TP1 (~3.1%)"),
    (86.00, "past TP2 (~5.3%)"),
    (83.00, "past TP3 (~8.6%)"),
]

for price, label in prices:
    pos.update_extremes(price)
    action = strategy.evaluate(pos, price=price, mark_price=price)
    pnl = ((90.83 - price) / 90.83) * 100
    if action:
        print(f"  ${price:.2f} | PNL={pnl:+.1f}% | {label}")
        print(f"    >> {action.type.value}: {action.reason}")
        if action.type in (ActionType.MOVE_SL, ActionType.PARTIAL_CLOSE):
            pos.current_sl = action.price
            pos.last_sl_update_price = action.price
    else:
        print(f"  ${price:.2f} | PNL={pnl:+.1f}% | {label} -> no action")

# --- Trailing after TP3 ---
print("\n--- TRAILING (after TP3) ---")
pos.lowest_since_entry = 83.00
pos.current_sl = 83.00
pos.last_sl_update_price = 83.00

for price in [80.0, 79.0, 78.0, 78.5, 79.0, 80.0, 80.5]:
    pos.update_extremes(price)
    action = strategy.evaluate(pos, price=price, mark_price=price)
    if action and action.type == ActionType.MOVE_SL:
        print(f"  ${price:.2f} | low=${pos.lowest_since_entry:.2f} | SL->{action.price:.4f} | UPDATE")
        pos.current_sl = action.price
        pos.last_sl_update_price = action.price
    else:
        reason = "no move needed" if price >= pos.lowest_since_entry else "threshold"
        print(f"  ${price:.2f} | low=${pos.lowest_since_entry:.2f} | SL=${pos.current_sl:.4f} | skip ({reason})")

# --- Serialization ---
print()
d = pos.to_dict()
pos2 = Position.from_dict(d)
print(f"Snapshot: {pos2.position_key} tp1={pos2.tp1_hit} tp2={pos2.tp2_hit} tp3={pos2.tp3_hit}")

# --- Config ---
cfg = yaml.safe_load(Path("config/default.yaml").read_text())
scfg = yaml.safe_load(Path("config/strategies/multi_trail.yaml").read_text())
print(f"Config: {list(cfg.keys())}")
print(f"Strategy: tp1={scfg['tp1_pct']}% tp2={scfg['tp2_pct']}% tp3={scfg['tp3_pct']}% trail={scfg['trail_pct']}%")

# --- EventBus ---
import asyncio
bus = EventBus()
received = []
async def handler(e): received.append(e.data)
bus.on("test", handler)
asyncio.get_event_loop().run_until_complete(bus.emit(Event(name="test", data="hello")))
print(f"EventBus: {received}")

# --- RateLimiter ---
pool = RateLimiterPool()
lim = pool.get("test_key", "bybit")
print(f"RateLimiter(bybit): {lim.max_per_second}/s, {lim.max_per_minute}/min")

print("\n=== ALL TESTS PASSED ===")

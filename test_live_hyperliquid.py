"""
LIVE TEST — WebSocket Price Feed + Strategy Evaluation

No API key needed (public price data only).
Connects to exchange, streams SOL price, runs trailing strategy on every tick.

Usage: python test_live_hyperliquid.py
       python test_live_hyperliquid.py --exchange bybit
       python test_live_hyperliquid.py --duration 30
"""
import sys
import asyncio
import argparse
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

import structlog

# Setup logging
structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="%H:%M:%S"),
        structlog.dev.ConsoleRenderer(colors=False),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
)

from core.event_bus import EventBus
from core.types import (
    Event, Position, PositionState, PriceUpdate, Side,
    Action, ActionType,
)
from execution.position_manager import PositionManager


logger = structlog.get_logger("live_test")


async def try_connect(exchange_id: str, timeout: float = 20.0):
    """Try to connect to an exchange with timeout. Returns (exchange, symbol) or raises."""
    import ccxt.pro as ccxtpro

    exchange_class = getattr(ccxtpro, exchange_id)
    config = {"enableRateLimit": True}

    if exchange_id == "hyperliquid":
        config["options"] = {"defaultType": "swap"}
        symbol = "SOL/USDT:USDT"
    elif exchange_id == "bybit":
        config["options"] = {"defaultType": "swap"}
        symbol = "SOL/USDT:USDT"
    else:
        symbol = "SOL/USDT:USDT"

    exchange = exchange_class(config)

    print(f"  Connecting to {exchange_id}...")
    print(f"  Loading markets (timeout={timeout:.0f}s)...")

    await asyncio.wait_for(exchange.load_markets(), timeout=timeout)
    print(f"  OK: {len(exchange.markets)} markets loaded")

    return exchange, symbol


async def main(exchange_id: str = "hyperliquid", duration: int = 60):
    print("=" * 65)
    print("  AGENTRADE ENGINE v2 -- LIVE TEST")
    print(f"  Exchange: {exchange_id} (WebSocket)")
    print("  Symbol: SOL/USDT:USDT")
    print("  Mode: READ ONLY (no orders, no API key)")
    print(f"  Duration: {duration}s (auto-stop)")
    print("=" * 65)
    print()

    # --- Setup ---
    event_bus = EventBus()
    position_manager = PositionManager(event_bus)
    await position_manager.start()

    # Track actions emitted by strategy
    actions_log = []

    async def on_action(event: Event):
        action: Action = event.data
        actions_log.append(action)
        action_str = f"{action.type.value}: {action.reason}"
        if action.type == ActionType.MOVE_SL:
            action_str += f" | new_sl=${action.price:.4f}"
            pos = position_manager.get_position(action.position_key)
            if pos:
                pos.current_sl = action.price
                pos.last_sl_update_price = action.price
        elif action.type == ActionType.PARTIAL_CLOSE:
            action_str += f" | close {action.close_pct*100:.0f}%, sl=${action.price:.4f}"
            pos = position_manager.get_position(action.position_key)
            if pos:
                pos.current_sl = action.price
                pos.last_sl_update_price = action.price
                pos.size *= (1 - action.close_pct)
        print(f"  >>> ACTION: {action_str}")

    event_bus.on("action", on_action)

    # --- Connect to exchange ---
    try:
        exchange, symbol = await try_connect(exchange_id, timeout=30.0)
    except asyncio.TimeoutError:
        print(f"\n  TIMEOUT: {exchange_id} took too long (30s).")
        return
    except Exception as e:
        print(f"\n  ERROR: {type(e).__name__}: {e}")
        return

    print(f"\n  Connected to: {exchange_id}")

    # Get first price to use as entry
    print(f"  Waiting for first tick...")
    first_ticker = await asyncio.wait_for(
        exchange.watch_ticker(symbol), timeout=15.0
    )
    entry_price = float(first_ticker["last"])

    print(f"  Current SOL price: ${entry_price:.4f}")
    print()

    # --- Create test position (simulated short at current price) ---
    position = Position(
        user_id="live_test",
        symbol=symbol,
        side=Side.SHORT,
        exchange=exchange_id,
        entry_price=entry_price,
        size=10.0,
        leverage=10.0,
        state=PositionState.OPEN,
        strategy_name="multi_level_trail",
        strategy_config={
            "tp1_pct": 0.3,       # 0.3% -- small for testing (triggers fast)
            "tp2_pct": 0.5,
            "tp3_pct": 0.8,
            "tp1_close_pct": 0.33,
            "tp2_close_pct": 0.33,
            "tp3_close_pct": 0.0,
            "trail_pct": 0.2,     # 0.2% tight trail for testing
            "min_sl_change_pct": 0.05,
            "sl_type": "limit",
            "sl_buffer_pct": 0.3,
            "wick_protection": False,
        },
    )
    position.highest_since_entry = entry_price
    position.lowest_since_entry = entry_price
    position_manager.add_position(position)

    tp1_price = entry_price * (1 - 0.3/100)
    tp2_price = entry_price * (1 - 0.5/100)
    tp3_price = entry_price * (1 - 0.8/100)

    print(f"  Simulated SHORT @ ${entry_price:.4f}")
    print(f"  TP1 (0.3%) = ${tp1_price:.4f}  (price must drop to here)")
    print(f"  TP2 (0.5%) = ${tp2_price:.4f}")
    print(f"  TP3 (0.8%) = ${tp3_price:.4f}")
    print(f"  (using tight levels for testing -- normally 3/5/8%)")
    print()
    print(f"  Streaming live ticks for {duration}s... (Ctrl+C to stop early)")
    print("-" * 65)

    # --- Stream live prices ---
    tick_count = 0
    start_time = time.time()
    last_print_price = 0
    latencies = []
    price_min = float('inf')
    price_max = 0.0

    try:
        while True:
            # Auto-stop after duration
            elapsed = time.time() - start_time
            if elapsed >= duration:
                print(f"\n  Auto-stop: {duration}s reached.")
                break

            ws_start = time.monotonic()
            ticker = await asyncio.wait_for(
                exchange.watch_ticker(symbol), timeout=10.0
            )
            ws_end = time.monotonic()

            price = float(ticker["last"])
            mark = float(ticker.get("markPrice", 0)) if ticker.get("markPrice") else price
            tick_count += 1

            # Track stats
            latency_ms = (ws_end - ws_start) * 1000
            latencies.append(latency_ms)
            price_min = min(price_min, price)
            price_max = max(price_max, price)

            # Emit price event (triggers strategy evaluation)
            update = PriceUpdate(
                symbol=symbol,
                last_price=price,
                mark_price=mark,
                bid=float(ticker.get("bid", 0)) if ticker.get("bid") else None,
                ask=float(ticker.get("ask", 0)) if ticker.get("ask") else None,
                timestamp=time.time(),
                exchange=exchange_id,
            )
            await event_bus.emit(Event(name="price_update", data=update))

            # Print every meaningful price change (>$0.01) or first 3 ticks
            if abs(price - last_print_price) >= 0.01 or tick_count <= 3:
                pos = position_manager.get_position(position.position_key)
                pnl_pct = ((entry_price - price) / entry_price) * 100

                status = ""
                if pos:
                    if pos.tp3_hit:
                        status = f"TRAILING sl=${pos.current_sl:.4f}"
                    elif pos.tp2_hit:
                        status = "TP2 hit"
                    elif pos.tp1_hit:
                        status = "TP1 hit"
                    else:
                        status = "watching"

                avg_lat = sum(latencies[-50:]) / min(len(latencies), 50)
                print(
                    f"  #{tick_count:>4d} | ${price:.4f} | "
                    f"PNL={pnl_pct:+.3f}% | {status} | "
                    f"lat={latency_ms:.0f}ms avg={avg_lat:.0f}ms | "
                    f"{elapsed:.0f}s"
                )
                last_print_price = price

    except KeyboardInterrupt:
        print("\n  Stopped by user.")
    except asyncio.TimeoutError:
        print("\n  WebSocket timeout - no data received for 10s")
    except Exception as e:
        print(f"\n  Error: {type(e).__name__}: {e}")
    finally:
        elapsed = time.time() - start_time
        avg_lat = sum(latencies) / max(len(latencies), 1)
        min_lat = min(latencies) if latencies else 0
        max_lat = max(latencies) if latencies else 0

        print()
        print("=" * 65)
        print("  RESULTS")
        print("=" * 65)
        print(f"  Exchange:     {exchange_id}")
        print(f"  Duration:     {elapsed:.1f}s")
        print(f"  Ticks:        {tick_count}")
        print(f"  Ticks/sec:    {tick_count/max(elapsed,1):.1f}")
        print(f"  Latency avg:  {avg_lat:.1f}ms")
        print(f"  Latency min:  {min_lat:.1f}ms")
        print(f"  Latency max:  {max_lat:.1f}ms")
        if latencies:
            # p50, p95, p99
            sorted_lat = sorted(latencies)
            p50 = sorted_lat[len(sorted_lat)//2]
            p95 = sorted_lat[int(len(sorted_lat)*0.95)]
            p99 = sorted_lat[int(len(sorted_lat)*0.99)]
            print(f"  Latency p50:  {p50:.1f}ms")
            print(f"  Latency p95:  {p95:.1f}ms")
            print(f"  Latency p99:  {p99:.1f}ms")
        print(f"  Price range:  ${price_min:.4f} - ${price_max:.4f}")
        print(f"  Actions:      {len(actions_log)}")
        print(f"  Evaluations:  {position_manager.stats['evaluations']}")
        print(f"  Hit rate:     {position_manager.stats['hit_rate']}%")

        pos = position_manager.get_position(position.position_key)
        if pos:
            print(f"  TP1 hit:      {pos.tp1_hit}")
            print(f"  TP2 hit:      {pos.tp2_hit}")
            print(f"  TP3 hit:      {pos.tp3_hit}")
            if pos.current_sl:
                print(f"  Current SL:   ${pos.current_sl:.4f}")
            else:
                print(f"  Current SL:   none")
            print(f"  Lowest seen:  ${pos.lowest_since_entry:.4f}")
            print(f"  Highest seen: ${pos.highest_since_entry:.4f}")

        if actions_log:
            print()
            print("  Action Log:")
            for i, a in enumerate(actions_log[:30]):
                extra = ""
                if hasattr(a, 'price') and a.price:
                    extra = f" @ ${a.price:.4f}"
                print(f"    {i+1}. {a.type.value}: {a.reason}{extra}")

        print("=" * 65)

        try:
            await exchange.close()
        except Exception:
            pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Agentrade Engine v2 - Live Test")
    parser.add_argument("--exchange", default="hyperliquid", choices=["hyperliquid", "bybit", "binance", "okx"])
    parser.add_argument("--duration", type=int, default=60, help="Test duration in seconds")
    args = parser.parse_args()

    asyncio.run(main(exchange_id=args.exchange, duration=args.duration))

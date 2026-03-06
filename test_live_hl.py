"""
LIVE TEST — Hyperliquid WebSocket (lean version)
Pre-loads markets with sync ccxt, then streams WebSocket via ccxt.pro.
"""
import sys
import asyncio
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

import structlog
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

DURATION = 60
SYMBOL = "SOL/USDC:USDC"


async def main():
    print("=" * 65)
    print("  AGENTRADE ENGINE v2 -- LIVE HYPERLIQUID TEST")
    print("  Mode: READ ONLY | No API key | WebSocket")
    print(f"  Symbol: {SYMBOL}")
    print(f"  Duration: {DURATION}s")
    print("=" * 65)

    # --- Setup engine components ---
    event_bus = EventBus()
    position_manager = PositionManager(event_bus)
    await position_manager.start()

    actions_log = []

    async def on_action(event: Event):
        action: Action = event.data
        actions_log.append((time.time(), action))
        line = f"  >>> ACTION: {action.type.value}: {action.reason}"
        if action.type == ActionType.MOVE_SL and action.price:
            line += f" | new_sl=${action.price:.4f}"
            pos = position_manager.get_position(action.position_key)
            if pos:
                pos.current_sl = action.price
                pos.last_sl_update_price = action.price
        elif action.type == ActionType.PARTIAL_CLOSE and action.price:
            line += f" | close {action.close_pct*100:.0f}%, sl=${action.price:.4f}"
            pos = position_manager.get_position(action.position_key)
            if pos:
                pos.current_sl = action.price
                pos.last_sl_update_price = action.price
                pos.size *= (1 - action.close_pct)
        print(line)

    event_bus.on("action", on_action)

    # --- Step 1: Pre-load markets with SYNC ccxt (bypasses aiohttp hang) ---
    import ccxt
    import ccxt.pro as ccxtpro

    print("\n  Step 1: Loading markets (sync)...")
    t0 = time.time()
    sync_ex = ccxt.hyperliquid({"options": {"defaultType": "swap"}})
    sync_ex.load_markets()
    print(f"  OK: {len(sync_ex.markets)} markets in {time.time()-t0:.1f}s")

    if SYMBOL not in sync_ex.markets:
        print(f"  ERROR: {SYMBOL} not found!")
        sol_markets = [s for s in sync_ex.markets if "SOL" in s]
        print(f"  Available SOL markets: {sol_markets}")
        return

    # --- Step 2: Create async exchange and inject pre-loaded markets ---
    print("  Step 2: Creating WebSocket connection...")
    exchange = ccxtpro.hyperliquid({
        "enableRateLimit": True,
        "options": {"defaultType": "swap"},
    })
    # Inject pre-loaded market data to skip async load_markets()
    exchange.markets = sync_ex.markets
    exchange.markets_by_id = sync_ex.markets_by_id
    exchange.symbols = sync_ex.symbols
    exchange.ids = sync_ex.ids
    exchange.currencies = sync_ex.currencies
    exchange.currencies_by_id = sync_ex.currencies_by_id
    exchange.loaded_fees = sync_ex.loaded_fees if hasattr(sync_ex, 'loaded_fees') else {}
    print("  OK: Markets injected into async exchange")

    # --- Step 3: Get first tick (use allMids channel for speed) ---
    ws_params = {"channel": "allMids"}
    print(f"  Step 3: Subscribing to {SYMBOL} WebSocket (allMids)...")
    t0 = time.time()
    try:
        first_ticker = await asyncio.wait_for(
            exchange.watch_ticker(SYMBOL, ws_params), timeout=15.0
        )
    except asyncio.TimeoutError:
        print(f"  WebSocket timeout after {time.time()-t0:.0f}s")
        await exchange.close()
        return
    except Exception as e:
        print(f"  WebSocket error: {type(e).__name__}: {e}")
        await exchange.close()
        return

    entry_price = float(first_ticker["last"])
    print(f"  First tick in {time.time()-t0:.1f}s: SOL = ${entry_price:.4f}")

    # --- Create simulated SHORT position ---
    position = Position(
        user_id="live_test",
        symbol=SYMBOL,
        side=Side.SHORT,
        exchange="hyperliquid",
        entry_price=entry_price,
        size=10.0,
        leverage=10.0,
        state=PositionState.OPEN,
        strategy_name="multi_level_trail",
        strategy_config={
            "tp1_pct": 0.3,
            "tp2_pct": 0.5,
            "tp3_pct": 0.8,
            "tp1_close_pct": 0.33,
            "tp2_close_pct": 0.33,
            "tp3_close_pct": 0.0,
            "trail_pct": 0.2,
            "min_sl_change_pct": 0.05,
            "sl_type": "limit",
            "sl_buffer_pct": 0.3,
            "wick_protection": False,
        },
    )
    position.highest_since_entry = entry_price
    position.lowest_since_entry = entry_price
    position_manager.add_position(position)

    tp1 = entry_price * (1 - 0.3/100)
    tp2 = entry_price * (1 - 0.5/100)
    tp3 = entry_price * (1 - 0.8/100)

    print(f"\n  SHORT @ ${entry_price:.4f}")
    print(f"  TP1 (0.3%) = ${tp1:.4f}")
    print(f"  TP2 (0.5%) = ${tp2:.4f}")
    print(f"  TP3 (0.8%) = ${tp3:.4f}")
    print(f"\n  Streaming for {DURATION}s...")
    print("-" * 65)

    # --- Stream ---
    tick_count = 0
    start_time = time.time()
    last_print = 0
    latencies = []
    price_min = float('inf')
    price_max = 0.0

    try:
        while (time.time() - start_time) < DURATION:
            ws_t0 = time.monotonic()
            ticker = await asyncio.wait_for(
                exchange.watch_ticker(SYMBOL, ws_params), timeout=10.0
            )
            lat = (time.monotonic() - ws_t0) * 1000

            price = float(ticker["last"])
            mark = float(ticker.get("markPrice") or price)
            tick_count += 1
            latencies.append(lat)
            price_min = min(price_min, price)
            price_max = max(price_max, price)

            # Feed to engine
            update = PriceUpdate(
                symbol=SYMBOL, last_price=price, mark_price=mark,
                bid=float(ticker.get("bid") or 0) or None,
                ask=float(ticker.get("ask") or 0) or None,
                timestamp=time.time(), exchange="hyperliquid",
            )
            await event_bus.emit(Event(name="price_update", data=update))

            # Print on meaningful price change or first 3 ticks
            elapsed = time.time() - start_time
            if abs(price - last_print) >= 0.01 or tick_count <= 3:
                pos = position_manager.get_position(position.position_key)
                pnl = ((entry_price - price) / entry_price) * 100
                status = "watching"
                if pos:
                    if pos.tp3_hit:
                        status = f"TRAILING sl=${pos.current_sl:.4f}" if pos.current_sl else "TRAILING"
                    elif pos.tp2_hit:
                        status = "TP2 hit"
                    elif pos.tp1_hit:
                        status = "TP1 hit"

                avg_l = sum(latencies[-50:]) / min(len(latencies), 50)
                print(
                    f"  #{tick_count:>4d} | ${price:.4f} | "
                    f"PNL={pnl:+.3f}% | {status} | "
                    f"lat={lat:.0f}ms avg={avg_l:.0f}ms | "
                    f"{elapsed:.0f}s"
                )
                last_print = price

    except KeyboardInterrupt:
        print("\n  Stopped by user.")
    except asyncio.TimeoutError:
        print("\n  WebSocket timeout - no data for 10s")
    except Exception as e:
        print(f"\n  Error: {type(e).__name__}: {e}")
    finally:
        elapsed = time.time() - start_time
        avg_lat = sum(latencies) / max(len(latencies), 1)

        print()
        print("=" * 65)
        print("  RESULTS")
        print("=" * 65)
        print(f"  Exchange:     hyperliquid")
        print(f"  Duration:     {elapsed:.1f}s")
        print(f"  Ticks:        {tick_count}")
        print(f"  Ticks/sec:    {tick_count/max(elapsed,1):.1f}")
        print(f"  Latency avg:  {avg_lat:.1f}ms")
        if latencies:
            s = sorted(latencies)
            print(f"  Latency min:  {s[0]:.1f}ms")
            print(f"  Latency p50:  {s[len(s)//2]:.1f}ms")
            print(f"  Latency p95:  {s[int(len(s)*0.95)]:.1f}ms")
            print(f"  Latency max:  {s[-1]:.1f}ms")
        print(f"  Price range:  ${price_min:.4f} - ${price_max:.4f}")
        print(f"  Actions:      {len(actions_log)}")
        print(f"  Evaluations:  {position_manager.stats['evaluations']}")

        pos = position_manager.get_position(position.position_key)
        if pos:
            print(f"  TP1 hit:      {pos.tp1_hit}")
            print(f"  TP2 hit:      {pos.tp2_hit}")
            print(f"  TP3 hit:      {pos.tp3_hit}")
            print(f"  Current SL:   ${pos.current_sl:.4f}" if pos.current_sl else "  Current SL:   none")
            print(f"  Lowest:       ${pos.lowest_since_entry:.4f}")
            print(f"  Highest:      ${pos.highest_since_entry:.4f}")

        if actions_log:
            print()
            print("  Action Log:")
            for i, (ts, a) in enumerate(actions_log[:30]):
                extra = f" @ ${a.price:.4f}" if a.price else ""
                t = ts - start_time
                print(f"    [{t:6.1f}s] {a.type.value}: {a.reason}{extra}")

        print("=" * 65)

        try:
            await exchange.close()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())

"""
REAL TRADE TEST — Hyperliquid (Mainnet / Testnet)
Opens a small SOL short, trails it with the engine, auto-closes after 2 min.

Set HL_TESTNET=true in .env for testnet, or HL_TESTNET=false / omit for mainnet.

Usage: python test_real_trade.py
"""
import sys
import asyncio
import time
import os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

# Fix Windows Python 3.9 asyncio segfault
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from dotenv import load_dotenv
load_dotenv()

import ccxt
import ccxt.pro as ccxtpro
import structlog
import requests
import json

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

PRIVATE_KEY = os.getenv("HL_PRIVATE_KEY")
WALLET_ADDRESS = os.getenv("HL_WALLET_ADDRESS")
IS_TESTNET = os.getenv("HL_TESTNET", "false").lower() in ("true", "1", "yes")

SYMBOL = "SOL/USDC:USDC"
DURATION = 300  # 5 minutes — more time for price to move
ORDER_SIZE = 0.5  # 0.5 SOL (~$70, above $10 min)

# --- API URLs ---
API_URL = (
    "https://api.hyperliquid-testnet.xyz/info"
    if IS_TESTNET else
    "https://api.hyperliquid.xyz/info"
)
NET_LABEL = "TESTNET" if IS_TESTNET else "MAINNET"


async def main():
    print("=" * 65)
    print("  AGENTRADE ENGINE v2 -- REAL TRADE TEST")
    print(f"  Exchange: Hyperliquid {NET_LABEL}")
    print(f"  Symbol: {SYMBOL}")
    print(f"  Size: {ORDER_SIZE} SOL SHORT")
    print(f"  Duration: {DURATION}s (auto-close)")
    if IS_TESTNET:
        print("  WARNING: This places REAL orders on TESTNET")
    else:
        print("  *** WARNING: This places REAL orders on MAINNET ***")
        print("  *** REAL MONEY AT RISK ***")
    print("=" * 65)

    if not PRIVATE_KEY or not WALLET_ADDRESS:
        print("\n  ERROR: Set HL_PRIVATE_KEY and HL_WALLET_ADDRESS in .env")
        return

    # --- Safety confirmation for mainnet ---
    if not IS_TESTNET:
        confirm = input("\n  Type 'YES' to confirm MAINNET trade: ").strip()
        if confirm != "YES":
            print("  Aborted.")
            return

    # --- Check balance ---
    print(f"\n  Network:  {NET_LABEL}")
    print(f"  API URL:  {API_URL}")
    print(f"  Wallet:   {WALLET_ADDRESS}")

    r = requests.post(API_URL, json={
        "type": "clearinghouseState",
        "user": WALLET_ADDRESS
    }, timeout=15)
    state = r.json()
    perp_bal = float(state.get("marginSummary", {}).get("accountValue", 0))
    print(f"  Perp balance: ${perp_bal:.2f}")
    if perp_bal < 10:
        print("  ERROR: Need at least $10 in perp account to trade")
        return

    # --- Setup engine ---
    event_bus = EventBus()
    position_manager = PositionManager(event_bus)
    await position_manager.start()

    actions_log = []

    # --- Load markets from mainnet (always works) ---
    print("\n  Loading markets (mainnet reference)...")
    t0 = time.time()
    sync_ex = ccxt.hyperliquid({"options": {"defaultType": "swap"}})
    sync_ex.load_markets()
    print(f"  OK: {len(sync_ex.markets)} markets in {time.time()-t0:.1f}s")

    # --- TESTNET ONLY: Patch asset indices ---
    # Testnet and mainnet have different asset indices (e.g. SOL=0 on testnet, SOL=5 on mainnet)
    # ccxt uses market['baseId'] as the asset index in order requests
    # On mainnet, the loaded markets already have correct indices — no patching needed
    if IS_TESTNET:
        print("  Patching asset indices for testnet...")
        tn_meta = requests.post(
            "https://api.hyperliquid-testnet.xyz/info",
            json={"type": "meta"}, timeout=15
        ).json()
        tn_index_map = {u["name"]: i for i, u in enumerate(tn_meta["universe"])}
        patched = 0
        for sym, market in sync_ex.markets.items():
            if market.get("swap") and market.get("info"):
                coin = market["info"].get("name", "")
                if coin in tn_index_map:
                    market["baseId"] = tn_index_map[coin]
                    patched += 1
        print(f"  OK: {patched} markets patched (SOL=idx {tn_index_map.get('SOL')})")
    else:
        print("  Mainnet: no baseId patching needed")

    # --- Create authenticated exchange for TRADING ---
    trade_ex = ccxt.hyperliquid({
        "privateKey": PRIVATE_KEY,
        "walletAddress": WALLET_ADDRESS,
        "options": {"defaultType": "swap"},
    })
    if IS_TESTNET:
        trade_ex.set_sandbox_mode(True)
    trade_ex.markets = sync_ex.markets
    trade_ex.markets_by_id = sync_ex.markets_by_id
    trade_ex.symbols = sync_ex.symbols
    trade_ex.ids = sync_ex.ids
    trade_ex.currencies = sync_ex.currencies
    trade_ex.currencies_by_id = sync_ex.currencies_by_id

    # --- Create async exchange for WebSocket price feed ---
    ws_ex = ccxtpro.hyperliquid({
        "enableRateLimit": True,
        "options": {"defaultType": "swap"},
    })
    if IS_TESTNET:
        ws_ex.set_sandbox_mode(True)
    ws_ex.markets = sync_ex.markets
    ws_ex.markets_by_id = sync_ex.markets_by_id
    ws_ex.symbols = sync_ex.symbols
    ws_ex.ids = sync_ex.ids
    ws_ex.currencies = sync_ex.currencies
    ws_ex.currencies_by_id = sync_ex.currencies_by_id

    # --- Set leverage ---
    LEVERAGE = 5
    print(f"  Setting leverage to {LEVERAGE}x...")
    try:
        trade_ex.set_leverage(LEVERAGE, SYMBOL)
        print(f"  OK: {LEVERAGE}x leverage set")
    except Exception as e:
        print(f"  Leverage warning: {e} (may already be set)")

    # --- Get current price ---
    print(f"  Getting {SYMBOL} price...")
    ws_params = {"channel": "allMids"}
    first_ticker = await asyncio.wait_for(
        ws_ex.watch_ticker(SYMBOL, ws_params), timeout=15.0
    )
    current_price = float(first_ticker["last"])
    print(f"  SOL price: ${current_price:.4f}")

    # --- Open SHORT position ---
    print(f"\n  Opening SHORT {ORDER_SIZE} SOL @ market...")
    try:
        # Use limit IoC with 1% slippage (acts like market order)
        slippage_price = round(current_price * 0.99, 2)
        order = trade_ex.create_order(
            symbol=SYMBOL,
            type="limit",
            side="sell",
            amount=ORDER_SIZE,
            price=slippage_price,
            params={"timeInForce": "Ioc"},
        )
        print(f"  ORDER PLACED: {order['id']}")
        print(f"  Status: {order.get('status')}")
        print(f"  Price: ${float(order.get('average') or order.get('price') or current_price):.4f}")
        entry_price = float(order.get("average") or order.get("price") or current_price)
    except Exception as e:
        print(f"  ORDER ERROR: {type(e).__name__}: {e}")
        await ws_ex.close()
        return

    # Wait for fill
    await asyncio.sleep(2)

    # Verify position on exchange
    print("\n  Checking position on exchange...")
    r = requests.post(API_URL, json={
        "type": "clearinghouseState",
        "user": WALLET_ADDRESS
    }, timeout=15)
    state = r.json()
    for pos in state.get("assetPositions", []):
        info = pos.get("position", {})
        coin = info.get("coin", "")
        if "SOL" in coin:
            sz = info.get("szi", "0")
            ep = info.get("entryPx", "0")
            pnl = info.get("unrealizedPnl", "0")
            print(f"  CONFIRMED: SOL size={sz} entry=${ep} pnl=${pnl}")
            entry_price = float(ep)
            break
    else:
        print("  WARNING: Position not found on exchange yet")

    # --- Register position in engine ---
    position = Position(
        user_id=NET_LABEL.lower(),
        symbol=SYMBOL,
        side=Side.SHORT,
        exchange="hyperliquid",
        entry_price=entry_price,
        size=ORDER_SIZE,
        leverage=LEVERAGE,
        state=PositionState.OPEN,
        strategy_name="multi_level_trail",
        strategy_config={
            # Ultra-tight levels for testnet: SOL swings ~$0.02-0.05 in 5min
            # Entry ~$84.7 → TP1=$84.68, TP2=$84.66, TP3=$84.62
            "tp1_pct": 0.02,   # 0.02% = ~$0.017
            "tp2_pct": 0.04,   # 0.04% = ~$0.034
            "tp3_pct": 0.06,   # 0.06% = ~$0.051
            "tp1_close_pct": 0.33,
            "tp2_close_pct": 0.33,
            "tp3_close_pct": 0.0,
            "trail_pct": 0.02,           # tight trail
            "min_sl_change_pct": 0.01,   # tiny SL steps
            "sl_type": "limit",
            "sl_buffer_pct": 0.05,
            "wick_protection": False,
        },
    )
    position.highest_since_entry = entry_price
    position.lowest_since_entry = entry_price
    position_manager.add_position(position)

    tp1 = entry_price * (1 - 0.02/100)
    tp2 = entry_price * (1 - 0.04/100)
    tp3 = entry_price * (1 - 0.06/100)

    # --- Action handler: EXECUTE REAL ORDERS ---
    # Shared mutable ref so on_action can access latest price
    latest_price = {"value": current_price}

    async def on_action(event: Event):
        action: Action = event.data
        actions_log.append((time.time(), action))
        line = f"  >>> ACTION: {action.type.value}: {action.reason}"
        price_now = latest_price["value"]

        pos = position_manager.get_position(action.position_key)

        if action.type == ActionType.MOVE_SL and action.price:
            line += f" | new_sl=${action.price:.4f}"
            print(line)
            # Place real SL order on exchange
            try:
                # Hyperliquid: use trigger order (stop-market) for SL
                sl_trigger = action.price
                # For SHORT: SL is above entry (buy to close), trigger when price >= sl
                # Slippage buffer: 0.3% above trigger for fill
                sl_limit = round(sl_trigger * 1.003, 2)
                sl_order = trade_ex.create_order(
                    symbol=SYMBOL,
                    type="limit",
                    side="buy",
                    amount=pos.size if pos else ORDER_SIZE,
                    price=sl_limit,
                    params={
                        "triggerPrice": sl_trigger,
                        "reduceOnly": True,
                        "timeInForce": "Gtc",
                    },
                )
                print(f"  [SL PLACED] trigger=${sl_trigger:.4f} limit=${sl_limit:.4f} id={sl_order['id']}")
                # Cancel previous SL if exists
                if pos and pos.sl_order_id:
                    try:
                        trade_ex.cancel_order(pos.sl_order_id, SYMBOL)
                        print(f"  [SL CANCELLED] old id={pos.sl_order_id}")
                    except Exception:
                        pass  # May already be cancelled
                if pos:
                    pos.sl_order_id = sl_order["id"]
            except Exception as e:
                print(f"  [SL ERROR] {type(e).__name__}: {e}")
                print(f"  [FALLBACK] SL tracked in engine only @ ${action.price:.4f}")
            if pos:
                pos.current_sl = action.price
                pos.last_sl_update_price = action.price

        elif action.type == ActionType.PARTIAL_CLOSE and action.price:
            # Use current remaining size, not original ORDER_SIZE
            current_size = pos.size if pos else ORDER_SIZE
            close_size = round(current_size * action.close_pct, 2)
            # Ensure minimum order value ($10 on Hyperliquid)
            min_size = round(10.0 / price_now + 0.01, 2)  # ~0.12 SOL
            if close_size < min_size:
                close_size = min(min_size, round(current_size, 2))  # close min or all remaining
            line += f" | close {action.close_pct*100:.0f}% ({close_size:.2f} SOL), sl=${action.price:.4f}"
            print(line)
            # Execute partial close on exchange
            try:
                buy_slippage = round(price_now * 1.01, 2)
                close_order = trade_ex.create_order(
                    symbol=SYMBOL,
                    type="limit",
                    side="buy",  # buy to close short
                    amount=close_size,
                    price=buy_slippage,
                    params={"timeInForce": "Ioc", "reduceOnly": True},
                )
                print(f"  [EXECUTED] Partial close: {close_order['id']} status={close_order.get('status')}")
            except Exception as e:
                print(f"  [ERROR] Partial close failed: {e}")
            if pos:
                pos.current_sl = action.price
                pos.last_sl_update_price = action.price
                pos.size = round(current_size - close_size, 4)
        else:
            print(line)

    event_bus.on("action", on_action)

    print(f"\n  SHORT @ ${entry_price:.4f}")
    print(f"  TP1 (0.02%) = ${tp1:.4f}")
    print(f"  TP2 (0.04%) = ${tp2:.4f}")
    print(f"  TP3 (0.06%) = ${tp3:.4f}")
    print(f"\n  Trailing for {DURATION}s...")
    print("-" * 65)

    # --- Resilient price feed ---
    # Layer 1: WebSocket (primary, ~1s latency)
    # Layer 2: REST API polling (fallback, ~2-3s latency)
    # Layer 3: Emergency close if all feeds fail

    WS_TIMEOUT = 10.0          # seconds before WS is considered dead
    WS_MAX_RETRIES = 3         # consecutive WS failures before REST fallback
    REST_POLL_INTERVAL = 2.0   # seconds between REST polls
    REST_MAX_FAILURES = 5      # consecutive REST failures before emergency close
    FEED_DEAD_TIMEOUT = 30.0   # seconds with no price at all → emergency close

    async def fetch_price_rest():
        """Fallback: get price via REST API"""
        r = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: requests.post(API_URL, json={"type": "allMids"}, timeout=10)
        )
        mids = r.json()
        price = float(mids["SOL"])
        return price

    tick_count = 0
    start_time = time.time()
    last_print = 0
    latencies = []
    ws_failures = 0
    rest_failures = 0
    using_rest = False
    last_price_time = time.time()

    try:
        while (time.time() - start_time) < DURATION:
            price = None
            mark = None
            lat = 0
            source = "ws"

            # --- Layer 1: WebSocket ---
            if not using_rest:
                try:
                    ws_t0 = time.monotonic()
                    ticker = await asyncio.wait_for(
                        ws_ex.watch_ticker(SYMBOL, ws_params), timeout=WS_TIMEOUT
                    )
                    lat = (time.monotonic() - ws_t0) * 1000
                    price = float(ticker["last"])
                    mark = float(ticker.get("markPrice") or price)
                    ws_failures = 0  # reset on success
                except (asyncio.TimeoutError, Exception) as e:
                    ws_failures += 1
                    print(f"  [WARN] WebSocket fail #{ws_failures}: {type(e).__name__}: {e}")
                    if ws_failures >= WS_MAX_RETRIES:
                        print(f"  [FALLBACK] Switching to REST API after {ws_failures} WS failures")
                        using_rest = True
                        # Try to reconnect WS in background
                        try:
                            await ws_ex.close()
                        except Exception:
                            pass

            # --- Layer 2: REST API fallback ---
            if price is None:
                try:
                    rest_t0 = time.monotonic()
                    price = await fetch_price_rest()
                    lat = (time.monotonic() - rest_t0) * 1000
                    mark = price
                    source = "rest"
                    rest_failures = 0  # reset on success
                except Exception as e:
                    rest_failures += 1
                    print(f"  [WARN] REST fail #{rest_failures}: {type(e).__name__}: {e}")

            # --- Layer 3: Emergency close ---
            if price is None:
                time_since_last = time.time() - last_price_time
                if time_since_last > FEED_DEAD_TIMEOUT:
                    print(f"\n  [EMERGENCY] No price data for {time_since_last:.0f}s!")
                    print(f"  [EMERGENCY] Closing position for safety!")
                    break
                if rest_failures >= REST_MAX_FAILURES:
                    print(f"\n  [EMERGENCY] {REST_MAX_FAILURES} consecutive REST failures!")
                    print(f"  [EMERGENCY] Closing position for safety!")
                    break
                await asyncio.sleep(1)
                continue

            # --- Got a valid price ---
            last_price_time = time.time()
            latest_price["value"] = price  # Update for on_action handler
            tick_count += 1
            latencies.append(lat)

            update = PriceUpdate(
                symbol=SYMBOL, last_price=price, mark_price=mark,
                bid=None, ask=None,
                timestamp=time.time(), exchange="hyperliquid",
            )
            await event_bus.emit(Event(name="price_update", data=update))

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

                src_tag = f" [{source}]" if source != "ws" else ""
                print(
                    f"  #{tick_count:>4d} | ${price:.4f} | "
                    f"PNL={pnl:+.3f}% | {status} | "
                    f"lat={lat:.0f}ms | {elapsed:.0f}s{src_tag}"
                )
                last_print = price

            # REST fallback: try to recover WebSocket periodically
            if using_rest:
                await asyncio.sleep(REST_POLL_INTERVAL)
                # Every 30s, try to reconnect WS
                if tick_count % 15 == 0:
                    try:
                        print("  [RECOVERY] Attempting WebSocket reconnect...")
                        ws_ex2 = ccxtpro.hyperliquid({
                            "enableRateLimit": True,
                            "options": {"defaultType": "swap"},
                        })
                        if IS_TESTNET:
                            ws_ex2.set_sandbox_mode(True)
                        ws_ex2.markets = sync_ex.markets
                        ws_ex2.markets_by_id = sync_ex.markets_by_id
                        ws_ex2.symbols = sync_ex.symbols
                        ws_ex2.ids = sync_ex.ids
                        ws_ex2.currencies = sync_ex.currencies
                        ws_ex2.currencies_by_id = sync_ex.currencies_by_id
                        test_tick = await asyncio.wait_for(
                            ws_ex2.watch_ticker(SYMBOL, ws_params), timeout=5.0
                        )
                        # WS recovered!
                        ws_ex = ws_ex2
                        using_rest = False
                        ws_failures = 0
                        print("  [RECOVERY] WebSocket restored! ✓")
                    except Exception:
                        print("  [RECOVERY] WebSocket still down, staying on REST")

    except KeyboardInterrupt:
        print("\n  Stopped by user.")
    except Exception as e:
        print(f"\n  Error: {type(e).__name__}: {e}")

    # --- Cancel any pending SL trigger orders ---
    pos = position_manager.get_position(position.position_key)
    if pos and pos.sl_order_id:
        try:
            trade_ex.cancel_order(pos.sl_order_id, SYMBOL)
            print(f"  [SL CANCELLED] {pos.sl_order_id}")
        except Exception as e:
            print(f"  [SL CANCEL WARN] {e}")

    # --- Close remaining position (query EXCHANGE for actual size) ---
    print("\n  Closing remaining position...")
    try:
        r_state = requests.post(API_URL, json={
            "type": "clearinghouseState", "user": WALLET_ADDRESS
        }, timeout=15)
        exchange_pos = r_state.json()
        remaining = 0.0
        pos_side = None
        for ep in exchange_pos.get("assetPositions", []):
            info = ep.get("position", {})
            if "SOL" in info.get("coin", ""):
                remaining = abs(float(info.get("szi", "0")))
                pos_side = "buy" if float(info.get("szi", "0")) < 0 else "sell"
                break

        if remaining > 0.001:
            r_price = requests.post(API_URL, json={"type": "allMids"}, timeout=15)
            sol_mid = float(r_price.json().get("SOL", current_price))
            # Slippage direction based on position side
            close_price = sol_mid * (1.01 if pos_side == "buy" else 0.99)
            close_order = trade_ex.create_order(
                symbol=SYMBOL,
                type="limit",
                side=pos_side,
                amount=remaining,
                price=round(close_price, 2),
                params={"timeInForce": "Ioc", "reduceOnly": True},
            )
            print(f"  CLOSED: {close_order['id']} size={remaining} side={pos_side}")
        else:
            print("  Already fully closed")
    except Exception as e:
        print(f"  Close error: {e}")

    # --- Final results ---
    elapsed = time.time() - start_time
    avg_lat = sum(latencies) / max(len(latencies), 1)

    print()
    print("=" * 65)
    print(f"  RESULTS ({NET_LABEL})")
    print("=" * 65)
    print(f"  Duration:     {elapsed:.1f}s")
    print(f"  Ticks:        {tick_count}")
    print(f"  Ticks/sec:    {tick_count/max(elapsed,1):.1f}")
    print(f"  Latency avg:  {avg_lat:.1f}ms")
    if latencies:
        s = sorted(latencies)
        print(f"  Latency min:  {s[0]:.1f}ms")
        print(f"  Latency p50:  {s[len(s)//2]:.1f}ms")
    print(f"  Actions:      {len(actions_log)}")
    print(f"  Evaluations:  {position_manager.stats['evaluations']}")

    pos = position_manager.get_position(position.position_key)
    if pos:
        print(f"  TP1 hit:      {pos.tp1_hit}")
        print(f"  TP2 hit:      {pos.tp2_hit}")
        print(f"  TP3 hit:      {pos.tp3_hit}")
        if pos.current_sl:
            print(f"  Final SL:     ${pos.current_sl:.4f}")

    if actions_log:
        print()
        print("  Action Log:")
        for i, (ts, a) in enumerate(actions_log[:30]):
            extra = f" @ ${a.price:.4f}" if a.price else ""
            t = ts - start_time
            print(f"    [{t:6.1f}s] {a.type.value}: {a.reason}{extra}")

    # Check final perp balance
    r = requests.post(API_URL, json={
        "type": "clearinghouseState",
        "user": WALLET_ADDRESS
    }, timeout=15)
    state = r.json()
    final_bal = float(state.get("marginSummary", {}).get("accountValue", 0))
    print(f"\n  Final Perp Balance: ${final_bal:.2f}")

    print("=" * 65)

    try:
        await ws_ex.close()
    except Exception:
        pass


if __name__ == "__main__":
    asyncio.run(main())

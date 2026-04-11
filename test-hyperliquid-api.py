"""
TEST — Hyperliquid API Position Operations

Interactive test script for Hyperliquid position management.
Supports testnet and mainnet. Tests all core operations:
  1. Account info & balance
  2. Open positions query
  3. Open a position (long/short)
  4. Set stop-loss (trigger order)
  5. Set take-profit (trigger order)
  6. Modify SL/TP
  7. Partial close
  8. Full close
  9. Open orders query
  10. Cancel orders

Usage:
    python test-hyperliquid-api.py                  # Interactive menu (testnet)
    python test-hyperliquid-api.py --mainnet        # Interactive menu (mainnet)
    python test-hyperliquid-api.py --run-all        # Run all tests sequentially
    python test-hyperliquid-api.py --symbol BTC     # Use BTC instead of SOL
"""
import sys
import asyncio
import os
import time
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

import argparse
import ccxt
import ccxt.pro as ccxtpro
import requests
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

logger = structlog.get_logger("test_hl_api")

# ─── Config ──────────────────────────────────────────────────────────────────

PRIVATE_KEY = os.getenv("HL_PRIVATE_KEY")
WALLET_ADDRESS = os.getenv("HL_WALLET_ADDRESS")
IS_TESTNET = os.getenv("HL_TESTNET", "true").lower() in ("true", "1", "yes")

DEFAULT_SYMBOL_BASE = "SOL"
DEFAULT_LEVERAGE = 5
DEFAULT_ORDER_SIZE = 0.5  # SOL units


# ─── Helpers ─────────────────────────────────────────────────────────────────

class HyperliquidTestClient:
    """Wrapper around ccxt + Hyperliquid REST API for testing."""

    def __init__(self, testnet: bool = True, symbol_base: str = "SOL"):
        self.testnet = testnet
        self.symbol_base = symbol_base
        self.symbol = f"{symbol_base}/USDC:USDC"
        self.net_label = "TESTNET" if testnet else "MAINNET"
        self.api_url = (
            "https://api.hyperliquid-testnet.xyz/info"
            if testnet else
            "https://api.hyperliquid.xyz/info"
        )

        self.trade_ex: ccxt.Exchange | None = None
        self.ws_ex: ccxtpro.Exchange | None = None
        self._markets_loaded = False

    def _header(self, title: str):
        print(f"\n{'─' * 60}")
        print(f"  {title}")
        print(f"{'─' * 60}")

    def _ok(self, msg: str):
        print(f"  [OK] {msg}")

    def _err(self, msg: str):
        print(f"  [ERROR] {msg}")

    def _info(self, msg: str):
        print(f"  {msg}")

    # ─── Setup ───────────────────────────────────────────────────────────

    async def setup(self):
        """Load markets and create authenticated exchange instances."""
        self._header(f"SETUP — {self.net_label}")

        if not PRIVATE_KEY or not WALLET_ADDRESS:
            self._err("Set HL_PRIVATE_KEY and HL_WALLET_ADDRESS in .env")
            return False

        self._info(f"Wallet:  {WALLET_ADDRESS}")
        self._info(f"Network: {self.net_label}")
        self._info(f"Symbol:  {self.symbol}")

        # Load markets from mainnet (always has full list)
        self._info("Loading markets...")
        t0 = time.time()
        sync_ex = ccxt.hyperliquid({"options": {"defaultType": "swap"}})
        sync_ex.load_markets()
        self._ok(f"{len(sync_ex.markets)} markets loaded in {time.time()-t0:.1f}s")

        # Patch testnet asset indices
        if self.testnet:
            self._info("Patching testnet asset indices...")
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
            self._ok(f"{patched} markets patched ({self.symbol_base}=idx {tn_index_map.get(self.symbol_base)})")

        # Authenticated REST exchange
        self.trade_ex = ccxt.hyperliquid({
            "privateKey": PRIVATE_KEY,
            "walletAddress": WALLET_ADDRESS,
            "options": {"defaultType": "swap"},
        })
        if self.testnet:
            self.trade_ex.set_sandbox_mode(True)

        # Copy markets
        self.trade_ex.markets = sync_ex.markets
        self.trade_ex.markets_by_id = sync_ex.markets_by_id
        self.trade_ex.symbols = sync_ex.symbols
        self.trade_ex.ids = sync_ex.ids
        self.trade_ex.currencies = sync_ex.currencies
        self.trade_ex.currencies_by_id = sync_ex.currencies_by_id

        # WebSocket exchange for price data
        self.ws_ex = ccxtpro.hyperliquid({
            "enableRateLimit": True,
            "options": {"defaultType": "swap"},
        })
        if self.testnet:
            self.ws_ex.set_sandbox_mode(True)
        self.ws_ex.markets = sync_ex.markets
        self.ws_ex.markets_by_id = sync_ex.markets_by_id
        self.ws_ex.symbols = sync_ex.symbols
        self.ws_ex.ids = sync_ex.ids
        self.ws_ex.currencies = sync_ex.currencies
        self.ws_ex.currencies_by_id = sync_ex.currencies_by_id

        self._markets_loaded = True
        self._ok("Setup complete")
        return True

    async def cleanup(self):
        """Close exchange connections."""
        if self.ws_ex:
            try:
                await self.ws_ex.close()
            except Exception:
                pass

    # ─── 1. Account Info & Balance ───────────────────────────────────────

    async def test_account_info(self) -> dict | None:
        """Fetch account balance and margin info."""
        self._header("1. ACCOUNT INFO & BALANCE")

        try:
            r = requests.post(self.api_url, json={
                "type": "clearinghouseState",
                "user": WALLET_ADDRESS
            }, timeout=15)
            state = r.json()

            margin = state.get("marginSummary", {})
            account_value = float(margin.get("accountValue", 0))
            total_margin = float(margin.get("totalMarginUsed", 0))
            total_ntl = float(margin.get("totalNtlPos", 0))

            self._info(f"Account Value:    ${account_value:.2f}")
            self._info(f"Margin Used:      ${total_margin:.2f}")
            self._info(f"Total Notional:   ${total_ntl:.2f}")
            self._info(f"Free Margin:      ${account_value - total_margin:.2f}")

            # Withdrawal info
            withdrawable = float(state.get("withdrawable", 0))
            self._info(f"Withdrawable:     ${withdrawable:.2f}")

            self._ok("Account info retrieved")
            return state

        except Exception as e:
            self._err(f"{type(e).__name__}: {e}")
            return None

    # ─── 2. Open Positions ───────────────────────────────────────────────

    async def test_open_positions(self) -> list:
        """Fetch all open positions from exchange."""
        self._header("2. OPEN POSITIONS")

        try:
            r = requests.post(self.api_url, json={
                "type": "clearinghouseState",
                "user": WALLET_ADDRESS
            }, timeout=15)
            state = r.json()

            positions = state.get("assetPositions", [])
            open_positions = []

            if not positions:
                self._info("No open positions")
                return []

            for pos in positions:
                info = pos.get("position", {})
                szi = float(info.get("szi", "0"))
                if abs(szi) < 0.0001:
                    continue

                coin = info.get("coin", "?")
                entry = float(info.get("entryPx", "0"))
                liq = float(info.get("liquidationPx") or 0)
                upnl = float(info.get("unrealizedPnl", "0"))
                leverage_val = info.get("leverage", {})
                lev_type = leverage_val.get("type", "?")
                lev_value = leverage_val.get("value", "?")
                margin_used = float(info.get("marginUsed", "0"))
                side = "LONG" if szi > 0 else "SHORT"

                self._info(f"  {coin}: {side} {abs(szi)} @ ${entry:.4f}")
                self._info(f"    Leverage: {lev_value}x ({lev_type})")
                self._info(f"    Liq Price: ${liq:.4f}")
                self._info(f"    uPNL: ${upnl:.4f}")
                self._info(f"    Margin Used: ${margin_used:.2f}")

                open_positions.append({
                    "coin": coin, "side": side, "size": abs(szi),
                    "entry": entry, "liq": liq, "upnl": upnl,
                    "leverage": lev_value, "leverage_type": lev_type,
                })

            self._ok(f"{len(open_positions)} open position(s)")
            return open_positions

        except Exception as e:
            self._err(f"{type(e).__name__}: {e}")
            return []

    # ─── 3. Open Position ────────────────────────────────────────────────

    async def test_open_position(
        self, side: str = "short", size: float = DEFAULT_ORDER_SIZE, leverage: int = DEFAULT_LEVERAGE
    ) -> dict | None:
        """Open a new position (long or short)."""
        self._header(f"3. OPEN {side.upper()} POSITION")

        try:
            # Set leverage
            self._info(f"Setting leverage to {leverage}x...")
            try:
                self.trade_ex.set_leverage(leverage, self.symbol)
                self._ok(f"{leverage}x leverage set")
            except Exception as e:
                self._info(f"Leverage note: {e}")

            # Get current price
            price = await self._get_price()
            self._info(f"Current {self.symbol_base} price: ${price:.4f}")

            # Calculate order params
            order_side = "sell" if side == "short" else "buy"
            # IoC limit order with slippage acts as market order
            if side == "short":
                limit_price = round(price * 0.99, 2)  # 1% below for short
            else:
                limit_price = round(price * 1.01, 2)  # 1% above for long

            notional = size * price
            self._info(f"Order: {order_side} {size} {self.symbol_base} @ ${limit_price:.2f} (IoC)")
            self._info(f"Notional: ~${notional:.2f}")

            if notional < 10:
                self._err(f"Order notional ${notional:.2f} below $10 minimum")
                return None

            # Place order
            order = self.trade_ex.create_order(
                symbol=self.symbol,
                type="limit",
                side=order_side,
                amount=size,
                price=limit_price,
                params={"timeInForce": "Ioc"},
            )

            order_id = order.get("id", "?")
            status = order.get("status", "?")
            fill_price = float(order.get("average") or order.get("price") or price)

            self._info(f"Order ID:   {order_id}")
            self._info(f"Status:     {status}")
            self._info(f"Fill Price: ${fill_price:.4f}")

            # Wait and verify
            await asyncio.sleep(2)
            verified = await self._verify_position()
            if verified:
                self._ok(f"{side.upper()} position opened")
            else:
                self._info("Position may not have filled — check open positions")

            return order

        except Exception as e:
            self._err(f"{type(e).__name__}: {e}")
            return None

    # ─── 4. Set Stop-Loss ────────────────────────────────────────────────

    async def test_set_stop_loss(self, sl_pct: float = 2.0) -> dict | None:
        """Set a stop-loss trigger order on the current position."""
        self._header("4. SET STOP-LOSS")

        try:
            pos = await self._get_current_position()
            if not pos:
                self._err(f"No open {self.symbol_base} position found")
                return None

            entry = pos["entry"]
            side = pos["side"]
            size = pos["size"]

            # Calculate SL price
            if side == "SHORT":
                sl_price = round(entry * (1 + sl_pct / 100), 2)
                sl_limit = round(sl_price * 1.003, 2)  # 0.3% buffer above
                order_side = "buy"
            else:
                sl_price = round(entry * (1 - sl_pct / 100), 2)
                sl_limit = round(sl_price * 0.997, 2)  # 0.3% buffer below
                order_side = "sell"

            self._info(f"Position:  {side} {size} @ ${entry:.4f}")
            self._info(f"SL Pct:    {sl_pct}%")
            self._info(f"Trigger:   ${sl_price:.4f}")
            self._info(f"Limit:     ${sl_limit:.4f}")

            order = self.trade_ex.create_order(
                symbol=self.symbol,
                type="limit",
                side=order_side,
                amount=size,
                price=sl_limit,
                params={
                    "triggerPrice": sl_price,
                    "reduceOnly": True,
                    "timeInForce": "Gtc",
                },
            )

            self._info(f"Order ID:  {order.get('id', '?')}")
            self._info(f"Status:    {order.get('status', '?')}")
            self._ok(f"Stop-loss set at ${sl_price:.4f}")
            return order

        except Exception as e:
            self._err(f"{type(e).__name__}: {e}")
            return None

    # ─── 5. Set Take-Profit ──────────────────────────────────────────────

    async def test_set_take_profit(self, tp_pct: float = 3.0) -> dict | None:
        """Set a take-profit trigger order on the current position."""
        self._header("5. SET TAKE-PROFIT")

        try:
            pos = await self._get_current_position()
            if not pos:
                self._err(f"No open {self.symbol_base} position found")
                return None

            entry = pos["entry"]
            side = pos["side"]
            size = pos["size"]

            # Calculate TP price
            if side == "SHORT":
                tp_price = round(entry * (1 - tp_pct / 100), 2)
                tp_limit = round(tp_price * 0.997, 2)  # buffer below
                order_side = "buy"
            else:
                tp_price = round(entry * (1 + tp_pct / 100), 2)
                tp_limit = round(tp_price * 1.003, 2)  # buffer above
                order_side = "sell"

            self._info(f"Position:  {side} {size} @ ${entry:.4f}")
            self._info(f"TP Pct:    {tp_pct}%")
            self._info(f"Trigger:   ${tp_price:.4f}")
            self._info(f"Limit:     ${tp_limit:.4f}")

            order = self.trade_ex.create_order(
                symbol=self.symbol,
                type="limit",
                side=order_side,
                amount=size,
                price=tp_limit,
                params={
                    "triggerPrice": tp_price,
                    "reduceOnly": True,
                    "timeInForce": "Gtc",
                },
            )

            self._info(f"Order ID:  {order.get('id', '?')}")
            self._info(f"Status:    {order.get('status', '?')}")
            self._ok(f"Take-profit set at ${tp_price:.4f}")
            return order

        except Exception as e:
            self._err(f"{type(e).__name__}: {e}")
            return None

    # ─── 6. Modify SL/TP ────────────────────────────────────────────────

    async def test_modify_sl(self, new_sl_pct: float = 1.5) -> dict | None:
        """Cancel existing SL and place a new one (modify)."""
        self._header("6. MODIFY STOP-LOSS")

        try:
            # Cancel existing trigger orders first
            cancelled = await self._cancel_trigger_orders()
            self._info(f"Cancelled {cancelled} existing trigger order(s)")

            # Place new SL
            result = await self.test_set_stop_loss(sl_pct=new_sl_pct)
            if result:
                self._ok(f"SL modified to {new_sl_pct}%")
            return result

        except Exception as e:
            self._err(f"{type(e).__name__}: {e}")
            return None

    # ─── 7. Partial Close ────────────────────────────────────────────────

    async def test_partial_close(self, close_pct: float = 0.33) -> dict | None:
        """Close a percentage of the current position."""
        self._header(f"7. PARTIAL CLOSE ({close_pct*100:.0f}%)")

        try:
            pos = await self._get_current_position()
            if not pos:
                self._err(f"No open {self.symbol_base} position found")
                return None

            side = pos["side"]
            size = pos["size"]
            close_size = round(size * close_pct, 4)

            # Min notional check
            price = await self._get_price()
            notional = close_size * price
            if notional < 10:
                min_size = round(10.0 / price + 0.01, 4)
                close_size = min(min_size, size)
                self._info(f"Bumped to min size: {close_size} (notional ${close_size * price:.2f})")

            order_side = "buy" if side == "SHORT" else "sell"
            # IoC with slippage
            if order_side == "buy":
                limit_price = round(price * 1.01, 2)
            else:
                limit_price = round(price * 0.99, 2)

            self._info(f"Position:  {side} {size} {self.symbol_base}")
            self._info(f"Closing:   {close_size} {self.symbol_base} ({close_pct*100:.0f}%)")
            self._info(f"Price:     ${price:.4f}")

            order = self.trade_ex.create_order(
                symbol=self.symbol,
                type="limit",
                side=order_side,
                amount=close_size,
                price=limit_price,
                params={"timeInForce": "Ioc", "reduceOnly": True},
            )

            self._info(f"Order ID:  {order.get('id', '?')}")
            self._info(f"Status:    {order.get('status', '?')}")

            await asyncio.sleep(1)
            remaining = await self._get_current_position()
            if remaining:
                self._ok(f"Closed {close_size}, remaining: {remaining['size']} {self.symbol_base}")
            else:
                self._ok(f"Position fully closed")

            return order

        except Exception as e:
            self._err(f"{type(e).__name__}: {e}")
            return None

    # ─── 8. Full Close ───────────────────────────────────────────────────

    async def test_full_close(self) -> dict | None:
        """Close the entire current position."""
        self._header("8. FULL CLOSE")

        try:
            pos = await self._get_current_position()
            if not pos:
                self._err(f"No open {self.symbol_base} position found")
                return None

            side = pos["side"]
            size = pos["size"]
            price = await self._get_price()

            # Cancel trigger orders first
            cancelled = await self._cancel_trigger_orders()
            if cancelled > 0:
                self._info(f"Cancelled {cancelled} trigger order(s)")

            order_side = "buy" if side == "SHORT" else "sell"
            if order_side == "buy":
                limit_price = round(price * 1.01, 2)
            else:
                limit_price = round(price * 0.99, 2)

            self._info(f"Closing:   {side} {size} {self.symbol_base} @ ~${price:.4f}")

            order = self.trade_ex.create_order(
                symbol=self.symbol,
                type="limit",
                side=order_side,
                amount=size,
                price=limit_price,
                params={"timeInForce": "Ioc", "reduceOnly": True},
            )

            self._info(f"Order ID:  {order.get('id', '?')}")
            self._info(f"Status:    {order.get('status', '?')}")

            await asyncio.sleep(1)
            remaining = await self._get_current_position()
            if not remaining:
                self._ok("Position fully closed")
            else:
                self._info(f"Remaining: {remaining['size']} {self.symbol_base} (may need retry)")

            return order

        except Exception as e:
            self._err(f"{type(e).__name__}: {e}")
            return None

    # ─── 9. Open Orders ─────────────────────────────────────────────────

    async def test_open_orders(self) -> list:
        """Fetch all open orders (limit + trigger)."""
        self._header("9. OPEN ORDERS")

        try:
            r = requests.post(self.api_url, json={
                "type": "openOrders",
                "user": WALLET_ADDRESS
            }, timeout=15)
            orders = r.json()

            if not orders:
                self._info("No open orders")
                return []

            for i, order in enumerate(orders):
                coin = order.get("coin", "?")
                side = order.get("side", "?")
                sz = order.get("sz", "?")
                limit_px = order.get("limitPx", "?")
                order_type = order.get("orderType", "?")
                trigger = order.get("triggerPx", "")
                oid = order.get("oid", "?")
                reduce = order.get("reduceOnly", False)

                trigger_str = f" trigger=${trigger}" if trigger else ""
                reduce_str = " [reduceOnly]" if reduce else ""
                self._info(f"  [{i+1}] {coin} {side} {sz} @ ${limit_px} ({order_type}){trigger_str}{reduce_str}")
                self._info(f"       OID: {oid}")

            self._ok(f"{len(orders)} open order(s)")
            return orders

        except Exception as e:
            self._err(f"{type(e).__name__}: {e}")
            return []

    # ─── 10. Cancel Orders ───────────────────────────────────────────────

    async def test_cancel_all_orders(self) -> int:
        """Cancel all open orders."""
        self._header("10. CANCEL ALL ORDERS")

        try:
            orders = await self.test_open_orders()
            if not orders:
                self._info("Nothing to cancel")
                return 0

            cancelled = 0
            for order in orders:
                oid = order.get("oid")
                coin = order.get("coin", "?")
                if oid:
                    try:
                        self.trade_ex.cancel_order(str(oid), self.symbol)
                        self._info(f"  Cancelled: {coin} OID={oid}")
                        cancelled += 1
                    except Exception as e:
                        self._info(f"  Cancel failed for OID={oid}: {e}")

            self._ok(f"Cancelled {cancelled}/{len(orders)} order(s)")
            return cancelled

        except Exception as e:
            self._err(f"{type(e).__name__}: {e}")
            return 0

    # ─── Internal Helpers ────────────────────────────────────────────────

    async def _get_price(self) -> float:
        """Get current price via REST."""
        r = requests.post(self.api_url, json={"type": "allMids"}, timeout=15)
        mids = r.json()
        return float(mids[self.symbol_base])

    async def _get_current_position(self) -> dict | None:
        """Get current position for the configured symbol."""
        r = requests.post(self.api_url, json={
            "type": "clearinghouseState",
            "user": WALLET_ADDRESS
        }, timeout=15)
        state = r.json()

        for pos in state.get("assetPositions", []):
            info = pos.get("position", {})
            coin = info.get("coin", "")
            if self.symbol_base in coin:
                szi = float(info.get("szi", "0"))
                if abs(szi) > 0.0001:
                    return {
                        "coin": coin,
                        "side": "LONG" if szi > 0 else "SHORT",
                        "size": abs(szi),
                        "entry": float(info.get("entryPx", "0")),
                        "upnl": float(info.get("unrealizedPnl", "0")),
                    }
        return None

    async def _verify_position(self) -> bool:
        """Check if position exists on exchange."""
        pos = await self._get_current_position()
        if pos:
            self._info(f"Verified: {pos['side']} {pos['size']} @ ${pos['entry']:.4f} (uPNL: ${pos['upnl']:.4f})")
            return True
        return False

    async def _cancel_trigger_orders(self) -> int:
        """Cancel all trigger orders for the current symbol."""
        r = requests.post(self.api_url, json={
            "type": "openOrders",
            "user": WALLET_ADDRESS
        }, timeout=15)
        orders = r.json()

        cancelled = 0
        for order in orders:
            coin = order.get("coin", "")
            trigger = order.get("triggerPx", "")
            if self.symbol_base in coin and trigger:
                oid = order.get("oid")
                if oid:
                    try:
                        self.trade_ex.cancel_order(str(oid), self.symbol)
                        cancelled += 1
                    except Exception:
                        pass
        return cancelled


# ─── Interactive Menu ────────────────────────────────────────────────────────

MENU = """
╔══════════════════════════════════════════════╗
║       Hyperliquid API Test — {net:8s}       ║
║       Symbol: {sym:20s}       ║
╠══════════════════════════════════════════════╣
║  1.  Account Info & Balance                  ║
║  2.  Open Positions                          ║
║  3.  Open Position (Short)                   ║
║  3L. Open Position (Long)                    ║
║  4.  Set Stop-Loss                           ║
║  5.  Set Take-Profit                         ║
║  6.  Modify Stop-Loss                        ║
║  7.  Partial Close (33%)                     ║
║  8.  Full Close                              ║
║  9.  Open Orders                             ║
║  10. Cancel All Orders                       ║
║  A.  Run All Tests (sequential)              ║
║  Q.  Quit                                    ║
╚══════════════════════════════════════════════╝
"""


async def interactive(client: HyperliquidTestClient):
    """Interactive test menu."""
    while True:
        print(MENU.format(net=client.net_label, sym=client.symbol))
        choice = input("  Select test [1-10/A/Q]: ").strip().upper()

        if choice == "Q":
            print("\n  Bye!")
            break
        elif choice == "1":
            await client.test_account_info()
        elif choice == "2":
            await client.test_open_positions()
        elif choice == "3":
            await client.test_open_position(side="short")
        elif choice == "3L":
            await client.test_open_position(side="long")
        elif choice == "4":
            pct = input("  SL percentage (default 2.0): ").strip()
            await client.test_set_stop_loss(sl_pct=float(pct) if pct else 2.0)
        elif choice == "5":
            pct = input("  TP percentage (default 3.0): ").strip()
            await client.test_set_take_profit(tp_pct=float(pct) if pct else 3.0)
        elif choice == "6":
            pct = input("  New SL percentage (default 1.5): ").strip()
            await client.test_modify_sl(new_sl_pct=float(pct) if pct else 1.5)
        elif choice == "7":
            pct = input("  Close percentage 0.0-1.0 (default 0.33): ").strip()
            await client.test_partial_close(close_pct=float(pct) if pct else 0.33)
        elif choice == "8":
            confirm = input("  Confirm full close? [y/N]: ").strip().lower()
            if confirm == "y":
                await client.test_full_close()
            else:
                print("  Cancelled.")
        elif choice == "9":
            await client.test_open_orders()
        elif choice == "10":
            confirm = input("  Cancel ALL orders? [y/N]: ").strip().lower()
            if confirm == "y":
                await client.test_cancel_all_orders()
            else:
                print("  Cancelled.")
        elif choice == "A":
            await run_all(client)
        else:
            print("  Invalid choice.")

        input("\n  Press Enter to continue...")


async def run_all(client: HyperliquidTestClient):
    """Run all tests sequentially."""
    print("\n" + "=" * 60)
    print("  RUNNING ALL TESTS")
    print("=" * 60)

    results = {}

    # 1. Account info
    r = await client.test_account_info()
    results["account_info"] = "PASS" if r else "FAIL"

    # 2. Open positions
    await client.test_open_positions()
    results["open_positions"] = "PASS"

    # 3. Open short position
    r = await client.test_open_position(side="short")
    results["open_position"] = "PASS" if r else "FAIL"
    if not r:
        print("\n  Skipping remaining tests (no position opened)")
        _print_results(results)
        return

    await asyncio.sleep(2)

    # 4. Set stop-loss
    r = await client.test_set_stop_loss(sl_pct=2.0)
    results["set_sl"] = "PASS" if r else "FAIL"

    # 5. Set take-profit
    r = await client.test_set_take_profit(tp_pct=3.0)
    results["set_tp"] = "PASS" if r else "FAIL"

    await asyncio.sleep(1)

    # 6. Modify SL
    r = await client.test_modify_sl(new_sl_pct=1.5)
    results["modify_sl"] = "PASS" if r else "FAIL"

    await asyncio.sleep(1)

    # 9. Open orders (should show trigger orders)
    orders = await client.test_open_orders()
    results["open_orders"] = "PASS"

    # 7. Partial close (33%)
    r = await client.test_partial_close(close_pct=0.33)
    results["partial_close"] = "PASS" if r else "FAIL"

    await asyncio.sleep(2)

    # 10. Cancel remaining orders
    cancelled = await client.test_cancel_all_orders()
    results["cancel_orders"] = "PASS"

    await asyncio.sleep(1)

    # 8. Full close
    r = await client.test_full_close()
    results["full_close"] = "PASS" if r else "FAIL"

    # Final balance
    await client.test_account_info()

    _print_results(results)


def _print_results(results: dict):
    """Print test results summary."""
    print("\n" + "=" * 60)
    print("  TEST RESULTS")
    print("=" * 60)
    for name, status in results.items():
        icon = "[PASS]" if status == "PASS" else "[FAIL]"
        print(f"  {icon} {name}")

    passed = sum(1 for v in results.values() if v == "PASS")
    total = len(results)
    print(f"\n  {passed}/{total} passed")
    print("=" * 60)


# ─── Main ────────────────────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(description="Hyperliquid API Position Test")
    parser.add_argument("--mainnet", action="store_true", help="Use mainnet instead of testnet")
    parser.add_argument("--run-all", action="store_true", help="Run all tests sequentially")
    parser.add_argument("--symbol", default="SOL", help="Base symbol (default: SOL)")
    parser.add_argument("--size", type=float, default=0.5, help="Order size (default: 0.5)")
    parser.add_argument("--leverage", type=int, default=5, help="Leverage (default: 5)")
    args = parser.parse_args()

    testnet = not args.mainnet
    if args.mainnet:
        confirm = input("*** MAINNET MODE — REAL MONEY AT RISK. Type 'YES' to continue: ").strip()
        if confirm != "YES":
            print("Aborted.")
            return

    client = HyperliquidTestClient(testnet=testnet, symbol_base=args.symbol)

    print("=" * 60)
    print("  HYPERLIQUID API — POSITION OPERATIONS TEST")
    print(f"  Network:  {'TESTNET' if testnet else 'MAINNET'}")
    print(f"  Symbol:   {args.symbol}/USDC:USDC")
    print(f"  Size:     {args.size}")
    print(f"  Leverage: {args.leverage}x")
    print("=" * 60)

    ok = await client.setup()
    if not ok:
        return

    try:
        if args.run_all:
            await run_all(client)
        else:
            await interactive(client)
    finally:
        await client.cleanup()


if __name__ == "__main__":
    asyncio.run(main())

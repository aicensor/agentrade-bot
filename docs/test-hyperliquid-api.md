# Hyperliquid API — Position Operations Test

Interactive test script for verifying all Hyperliquid position management operations against testnet or mainnet.

## Quick Start

```bash
# Testnet (default, safe)
python test-hyperliquid-api.py

# Run all 10 tests automatically
python test-hyperliquid-api.py --run-all

# Mainnet (requires confirmation)
python test-hyperliquid-api.py --mainnet
```

## Prerequisites

1. **Environment variables** in `.env`:
   ```
   HL_PRIVATE_KEY=0x_your_private_key
   HL_WALLET_ADDRESS=0x_your_wallet_address
   HL_TESTNET=true
   ```

2. **Testnet funds** — if using testnet, ensure your wallet has USDC balance on Hyperliquid testnet. Fund via the [Hyperliquid testnet faucet](https://app.hyperliquid-testnet.xyz/).

3. **Dependencies** installed (`pip install -r requirements.txt`).

## Command-Line Options

| Flag | Default | Description |
|------|---------|-------------|
| `--mainnet` | off | Use mainnet (real money). Requires typing `YES` to confirm. |
| `--run-all` | off | Run all 10 tests sequentially instead of interactive menu. |
| `--symbol` | `SOL` | Base asset to trade (e.g., `BTC`, `ETH`, `SOL`). |
| `--size` | `0.5` | Order size in base asset units. |
| `--leverage` | `5` | Leverage multiplier. |

### Examples

```bash
# Test with BTC, 0.001 BTC, 10x leverage
python test-hyperliquid-api.py --symbol BTC --size 0.001 --leverage 10

# Run all tests on mainnet with ETH
python test-hyperliquid-api.py --mainnet --run-all --symbol ETH --size 0.01

# Quick interactive test with defaults
python test-hyperliquid-api.py
```

## Test Operations

### 1. Account Info & Balance

Fetches account state from Hyperliquid clearinghouse API:
- Account value (total equity)
- Margin used
- Total notional exposure
- Free margin (available for new positions)
- Withdrawable balance

No orders placed. Read-only.

### 2. Open Positions

Lists all open perpetual positions on the account:
- Coin, side (LONG/SHORT), size
- Entry price
- Liquidation price
- Unrealized PNL
- Leverage type and value
- Margin used

No orders placed. Read-only.

### 3. Open Position (Long / Short)

Opens a new position using an IoC (Immediate-or-Cancel) limit order with 1% slippage — effectively a market order.

- Menu option `3` opens a **SHORT**
- Menu option `3L` opens a **LONG**
- Sets leverage before placing the order
- Verifies the position on-chain after a 2s delay
- Checks minimum notional ($10 on Hyperliquid)

**Order flow:**
1. `set_leverage(leverage, symbol)`
2. `create_order(type="limit", side="sell"/"buy", params={"timeInForce": "Ioc"})`
3. Wait 2s → verify via clearinghouse API

### 4. Set Stop-Loss

Places a trigger order that acts as a stop-loss:
- Calculates SL price as `entry ± sl_pct%`
- Uses a limit order with 0.3% buffer for slippage protection
- `reduceOnly=True` ensures it only closes the position
- `timeInForce="Gtc"` (Good-til-Cancelled)

**For SHORT positions:** SL is above entry (buy trigger).
**For LONG positions:** SL is below entry (sell trigger).

Prompts for SL percentage in interactive mode (default: 2%).

### 5. Set Take-Profit

Same mechanism as SL but in the opposite direction:
- **SHORT:** TP is below entry (buy trigger)
- **LONG:** TP is above entry (sell trigger)
- 0.3% buffer for limit fill

Prompts for TP percentage in interactive mode (default: 3%).

### 6. Modify Stop-Loss

Demonstrates SL modification by:
1. Cancelling all existing trigger orders for the symbol
2. Placing a new SL at the updated percentage

This is the pattern used by the engine for trailing stop-loss updates.

Prompts for new SL percentage (default: 1.5%).

### 7. Partial Close

Closes a percentage of the current position:
- Default: 33% (matches TP1/TP2 close in the multi-level trail strategy)
- Uses IoC limit order with 1% slippage
- Enforces minimum notional ($10) — bumps up if partial amount is too small
- Reports remaining position size after close

Prompts for close percentage as decimal 0.0-1.0 (default: 0.33).

### 8. Full Close

Closes the entire position:
1. Cancels all trigger orders (SL/TP) first to avoid orphaned orders
2. Places IoC limit order for full remaining size
3. Verifies position is closed

Requires `y` confirmation in interactive mode.

### 9. Open Orders

Lists all open orders (limit and trigger) on the account:
- Coin, side, size, limit price
- Order type (limit, trigger, etc.)
- Trigger price (if trigger order)
- Reduce-only flag
- Order ID (OID)

No orders placed. Read-only.

### 10. Cancel All Orders

Cancels every open order on the account:
- Fetches all open orders first (displays them)
- Cancels each one individually
- Reports success/failure per order

Requires `y` confirmation in interactive mode.

## Run-All Mode

When using `--run-all`, tests execute in this order:

```
1. Account Info        (read-only)
2. Open Positions      (read-only)
3. Open Short          (places order)
   ↓ wait 2s
4. Set Stop-Loss 2%    (trigger order)
5. Set Take-Profit 3%  (trigger order)
   ↓ wait 1s
6. Modify SL to 1.5%   (cancel + re-place)
   ↓ wait 1s
9. Open Orders         (read-only, shows trigger orders)
7. Partial Close 33%   (IoC order)
   ↓ wait 2s
10. Cancel Orders      (cleans up triggers)
   ↓ wait 1s
8. Full Close          (IoC order)
1. Account Info        (final balance)
```

At the end, a PASS/FAIL summary is printed:

```
══════════════════════════════════════════════════════════════
  TEST RESULTS
══════════════════════════════════════════════════════════════
  [PASS] account_info
  [PASS] open_positions
  [PASS] open_position
  [PASS] set_sl
  [PASS] set_tp
  [PASS] modify_sl
  [PASS] open_orders
  [PASS] partial_close
  [PASS] cancel_orders
  [PASS] full_close

  10/10 passed
══════════════════════════════════════════════════════════════
```

## How Orders Work on Hyperliquid

### Market Orders (via IoC Limit)

Hyperliquid doesn't have true market orders. The script uses **IoC limit orders** with slippage:

```python
trade_ex.create_order(
    symbol="SOL/USDC:USDC",
    type="limit",
    side="sell",
    amount=0.5,
    price=current_price * 0.99,  # 1% slippage
    params={"timeInForce": "Ioc"},
)
```

IoC = fill immediately at limit or better, cancel any unfilled portion.

### Trigger Orders (SL/TP)

Stop-loss and take-profit are placed as trigger orders:

```python
trade_ex.create_order(
    symbol="SOL/USDC:USDC",
    type="limit",
    side="buy",           # buy to close a short
    amount=position_size,
    price=sl_limit,       # limit price (with buffer)
    params={
        "triggerPrice": sl_trigger,  # trigger activation price
        "reduceOnly": True,
        "timeInForce": "Gtc",
    },
)
```

- `triggerPrice`: when the mark price crosses this level, the limit order activates
- `reduceOnly`: prevents the order from opening a new position
- Buffer: 0.3% beyond trigger price to handle slippage on fill

### Minimum Order Size

Hyperliquid requires $10 minimum notional per order. The script handles this:
- If partial close amount < $10, bumps up to minimum (or closes all if remaining < minimum)
- Checked before every order placement

## Troubleshooting

| Issue | Cause | Fix |
|-------|-------|-----|
| `Set HL_PRIVATE_KEY and HL_WALLET_ADDRESS in .env` | Missing credentials | Add keys to `.env` file |
| `Order notional below $10 minimum` | Size too small | Increase `--size` or use a cheaper asset |
| `No open SOL position found` | No position to operate on | Run test 3 first to open a position |
| `Leverage note: ...` | Leverage already set | Safe to ignore — leverage persists |
| IoC order `status: canceled` | Price moved beyond slippage | Retry — price moved >1% between quote and fill |
| `Position may not have filled` | IoC expired unfilled | Check test 2 (positions) — increase slippage if persistent |
| Testnet index mismatch | Asset indices differ on testnet | Script auto-patches — if failing, check testnet meta API |

## Relation to Engine

This test script validates the same operations the engine performs automatically:

| Test | Engine Component | File |
|------|-----------------|------|
| Open position | External (user/signal) | — |
| Set SL | `ExchangeAdapter.set_stop_loss()` | `exchange/base.py` |
| Set TP | `ExchangeAdapter.set_take_profit()` | `exchange/base.py` |
| Modify SL | `OrderExecutor` (cancel + re-place) | `execution/order_executor.py` |
| Partial close | `ExchangeAdapter.close_position(pct=0.33)` | `exchange/base.py` |
| Full close | `ExchangeAdapter.close_position(pct=1.0)` | `exchange/base.py` |
| Fetch positions | `ExchangeAdapter.fetch_positions()` | `exchange/base.py` |

The engine's `MultiLevelTrail` strategy triggers these operations based on price movement:
1. Price hits TP1 → partial close 33% + move SL to breakeven
2. Price hits TP2 → partial close 33% + move SL to TP1
3. Price hits TP3 → activate tight trailing SL
4. Price reverses → SL triggers → full close

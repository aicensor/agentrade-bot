# Backtesting Engine

Replay historical price data through the same multi-level trailing TP/SL strategy used in live trading. Simulates entries, partial closes, stop-loss hits, and trailing — with slippage and fees.

## Quick Start

```bash
# Default: SOL short, 1000 1m candles, single trade
python run_backtest.py

# BTC long, 5m candles, more data
python run_backtest.py --symbol BTC/USDC:USDC --side long --timeframe 5m --candles 3000

# Parameter sweep
python run_backtest.py --sweep --candles 2000
```

## How It Works

### Architecture

The backtest engine is **synchronous** for speed — no event bus, no async. It calls the same `MultiLevelTrailStrategy.evaluate()` function used in live trading, ensuring zero logic divergence.

```
Historical Candles (ccxt OHLCV)
  → Tick Simulator (OHLC expansion)
    → For each tick:
      1. Check for new entries (based on entry_mode)
      2. For each open position:
         a. Check if SL was hit → close trade
         b. Call strategy.evaluate(position, price)
         c. Process returned Action (move SL, partial close, alert)
      3. Record equity curve
  → Generate BacktestResult with metrics
```

### Tick Simulation

Each candle is expanded into 4 simulated ticks to approximate intra-candle price movement:

```
Candle: O=100, H=102, L=98, C=101
  → Tick 1: 100 (open)
  → Tick 2: 102 (high)
  → Tick 3: 98  (low)
  → Tick 4: 101 (close)
```

This captures SL hits that occur at the high/low within a candle — critical for trailing stop accuracy.

Use `--tick-mode close` for faster but less accurate simulation (1 tick per candle).

### Entry Modes

| Mode | Description |
|------|-------------|
| `single` | One trade at the first candle, trail it until SL or end (default) |
| `every_candle` | Open a new trade every N candles (stress test for strategy robustness) |
| `signal` | Programmatic entry at specific candle indices (for custom signal testing) |

### Execution Simulation

- **Slippage**: Applied on every fill (entry, partial close, SL hit). Default 0.05%.
- **Fees**: Charged per side (entry + exit). Default 0.035% (Hyperliquid maker rate).
- **SL execution**: When price crosses the SL level, the trade closes at the SL price (not current price), with slippage applied.
- **Partial closes**: Execute at current price with slippage. Position size is reduced accordingly.

## Command-Line Reference

### Data Options

| Flag | Default | Description |
|------|---------|-------------|
| `--symbol` | `SOL/USDC:USDC` | Trading pair |
| `--exchange` | `hyperliquid` | Exchange for data (any ccxt exchange works) |
| `--timeframe` | `1m` | Candle interval: `1m`, `5m`, `15m`, `1h`, `4h`, `1d` |
| `--candles` | `1000` | Number of candles to fetch |

### Position Options

| Flag | Default | Description |
|------|---------|-------------|
| `--side` | `short` | Trade direction: `long` or `short` |
| `--size` | `1.0` | Position size in base asset units |
| `--leverage` | `5.0` | Leverage multiplier |
| `--capital` | `1000.0` | Initial capital in USD |

### Execution Options

| Flag | Default | Description |
|------|---------|-------------|
| `--slippage` | `0.05` | Slippage % per fill |
| `--fee` | `0.035` | Fee % per side (taker/maker) |
| `--entry-mode` | `single` | `single`, `every_candle`, or `signal` |
| `--tick-mode` | `ohlc` | `ohlc` (4 ticks/candle) or `close` (1 tick/candle) |

### Strategy Overrides

| Flag | Default | Description |
|------|---------|-------------|
| `--tp1` | `3.0` | TP1 percentage from entry |
| `--tp2` | `5.0` | TP2 percentage from entry |
| `--tp3` | `8.0` | TP3 percentage from entry |
| `--trail` | `1.5` | Trail percentage (after TP3) |
| `--tp1-close` | `0.33` | Partial close % at TP1 |
| `--tp2-close` | `0.33` | Partial close % at TP2 |
| `--min-sl-change` | `0.1` | Min SL change % to trigger update |

### Sweep Options

| Flag | Description |
|------|-------------|
| `--sweep` | Run parameter sweep (trail% by default) |
| `--full-sweep` | Full grid search: TP1 × TP3 × trail (use with `--sweep`) |

### Output Options

| Flag | Description |
|------|-------------|
| `--output FILE` | Export results to JSON |
| `--max-trades N` | Max trades to show in table (default: 50) |

## Output Metrics

### Per-Backtest

| Metric | Description |
|--------|-------------|
| Total trades | Number of completed trades |
| Win rate | % of trades with positive PnL |
| Total PnL | Absolute dollar PnL |
| Total PnL % | Cumulative return percentage |
| Avg win / Avg loss | Mean PnL for winners and losers |
| Profit factor | Gross profit / gross loss (>1.0 = profitable) |
| Expectancy | Average expected PnL per trade |
| Sharpe (approx) | Mean return / std dev of returns |
| Max drawdown | Largest peak-to-trough equity decline |
| TP1/TP2/TP3 hit rate | How often each level was reached |
| MFE / MAE | Max favorable / adverse excursion per trade |

### Per-Trade

| Field | Description |
|-------|-------------|
| Entry/Exit price | Fill prices (with slippage) |
| PnL % / PnL $ | Realized profit/loss |
| TP1/TP2/TP3 hit | Which take-profit levels were reached |
| Exit reason | `sl_hit`, `partial_close_complete`, `backtest_end` |
| Partial closes | List of intermediate closes with prices and PnL |
| Duration | Time from entry to exit |

## Examples

### 1. Single Trade Trail Test

Test how the strategy handles a single SOL short over 1000 1-minute candles:

```bash
python run_backtest.py --candles 1000 --side short
```

### 2. Stress Test with Many Entries

Open a trade every ~10 candles to test strategy robustness across different entry points:

```bash
python run_backtest.py --candles 2000 --entry-mode every_candle --timeframe 5m
```

### 3. Tight Scalping Parameters

Test aggressive TP levels for scalping:

```bash
python run_backtest.py --tp1 0.5 --tp2 1.0 --tp3 1.5 --trail 0.3 --timeframe 1m --candles 5000
```

### 4. Wide Swing Parameters

Test wider levels for swing trading on higher timeframes:

```bash
python run_backtest.py --tp1 5 --tp2 10 --tp3 15 --trail 3 --timeframe 4h --candles 1000 --side long
```

### 5. Find Optimal Trail %

Sweep trail percentages while keeping TP levels fixed:

```bash
python run_backtest.py --sweep --candles 3000 --timeframe 5m --entry-mode every_candle
```

Output:
```
  SWEEP RESULTS (sorted by PnL)
   TP1  TP2  TP3 Trail       PnL$    PnL%    WR    PF     DD Trades
  ──────────────────────────────────────────────────────────────────────
   3.0  5.0  8.0   0.5  $ +157.28 +186.1%   92% 126.47   6.1%    100
   3.0  5.0  8.0   1.0  $ +157.28 +186.1%   92% 126.47   6.1%    100
   ...
```

### 6. Full Grid Search

Sweep TP1, TP3, and trail across all combinations:

```bash
python run_backtest.py --sweep --full-sweep --candles 2000 --timeframe 5m --entry-mode every_candle
```

### 7. BTC Long with JSON Export

```bash
python run_backtest.py --symbol BTC/USDC:USDC --side long --timeframe 1h --candles 500 --output btc_long.json
```

### 8. Compare Different Exchanges

```bash
python run_backtest.py --exchange hyperliquid --candles 1000
python run_backtest.py --exchange bybit --symbol SOL/USDT:USDT --candles 1000
```

## JSON Export Format

When using `--output`, the result is exported as JSON:

```json
{
  "symbol": "SOL/USDC:USDC",
  "side": "short",
  "timeframe": "5m",
  "strategy_config": { "tp1_pct": 3.0, ... },
  "metrics": {
    "total_trades": 100,
    "win_rate": 92.0,
    "total_pnl": 157.28,
    "profit_factor": 126.47,
    "max_drawdown_pct": 6.12,
    "sharpe_approx": 1.45,
    ...
  },
  "trades": [
    {
      "id": 1,
      "side": "short",
      "entry_price": 86.54,
      "exit_price": 84.05,
      "realized_pnl": 2.49,
      "realized_pnl_pct": 2.88,
      "tp1_hit": true,
      "tp2_hit": true,
      "tp3_hit": false,
      "partial_closes": [ ... ],
      "exit_reason": "sl_hit",
      ...
    }
  ]
}
```

## Python API

Use the backtest engine programmatically:

```python
from backtest.data import fetch_ohlcv
from backtest.engine import BacktestEngine

# Fetch data
history = fetch_ohlcv(
    symbol="SOL/USDC:USDC",
    exchange_id="hyperliquid",
    timeframe="5m",
    limit=2000,
)

# Configure and run
engine = BacktestEngine(
    strategy_config={
        "tp1_pct": 3.0,
        "tp2_pct": 5.0,
        "tp3_pct": 8.0,
        "trail_pct": 1.5,
    },
    position_size=1.0,
    leverage=5.0,
    initial_capital=1000.0,
    entry_mode="single",
)

result = engine.run(history, side="short")

# Access metrics
print(f"PnL: ${result.total_pnl:.2f}")
print(f"Win rate: {result.win_rate:.1f}%")
print(f"Profit factor: {result.profit_factor:.2f}")

# Iterate trades
for trade in result.trades:
    print(f"#{trade.trade_id}: {trade.realized_pnl_pct:+.2f}% ({trade.exit_reason})")

# Full summary
print(result.summary())
```

## Limitations

- **No order book simulation**: Fills are simulated at candle OHLC prices with fixed slippage, not against actual order book depth.
- **No funding rate**: Funding payments are not simulated. For positions held >8h, actual results will differ.
- **Tick approximation**: OHLC expansion assumes O→H→L→C order. Real intra-candle movement may differ (H before L, or vice versa). Use lower timeframes for better accuracy.
- **No concurrency effects**: The backtest runs trades independently. In live trading, multiple positions share rate limits and execution queues.
- **Wick protection disabled**: The backtest disables wick protection (time-based confirmation) since tick timestamps are simulated, not real-time.

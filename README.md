# Agentrade Engine v2

Real-time trailing TP/SL engine for crypto perpetual futures. Monitors positions via WebSocket price feeds and automatically manages stop-loss, take-profit, and partial closes across multiple exchanges.

## Architecture

```
Price Feed (WebSocket)
  -> Event Bus
    -> Position Manager (evaluates strategy per tick)
      -> Order Queue (dedup + batch every 500ms)
        -> Order Executor (exchange REST API)
          -> Telegram Notifier (alerts)
  -> State Store (Redis snapshots every 5s)
```

## Supported Exchanges

- Hyperliquid
- Bybit
- Binance
- OKX
- MEXC

## Prerequisites

- **Python 3.12+**
- **Redis 7+** — used for state persistence and crash recovery (falls back to JSON file if unavailable)
- **Docker** (recommended for Redis):
  ```bash
  docker run -d --name agentrade-redis --restart unless-stopped -p 6379:6379 redis:7-alpine
  ```

## Installation

```bash
# Clone the repo
git clone <repo-url> && cd agentrade-bot

# Install dependencies
pip install -r requirements.txt

# Copy and configure environment
cp .env.example .env
# Edit .env with your credentials
```

## Configuration

### Environment Variables (`.env`)

| Variable | Description | Required |
|----------|-------------|----------|
| `HL_PRIVATE_KEY` | Hyperliquid private key | For HL trading |
| `HL_WALLET_ADDRESS` | Hyperliquid wallet address | For HL trading |
| `HL_TESTNET` | Use Hyperliquid testnet (`true`/`false`) | No (default: `true`) |
| `TELEGRAM_BOT_TOKEN` | Telegram bot API token | No (dry mode if empty) |
| `TELEGRAM_ADMIN_CHAT_ID` | Telegram chat ID for system alerts | No |
| `TELEGRAM_ALLOWED_USER_IDS` | Comma-separated Telegram user IDs allowed to interact | No (allow all if empty) |
| `REDIS_URL` | Redis connection URL | No (default: `redis://localhost:6379/0`) |

### Engine Config (`config/default.yaml`)

Key settings:
- `engine.snapshot_interval_sec`: State snapshot frequency (default: 5s)
- `engine.reconciliation_interval_sec`: Exchange sync interval (default: 30s)
- `engine.flush_interval_ms`: Order queue batch interval (default: 500ms)
- `risk.max_leverage`: Maximum allowed leverage (default: 10x)
- `risk.sl_type`: Stop-loss type — `"market"` or `"limit"` (default: `"limit"`)
- `telegram.verbose_trailing`: Send message on every SL update (default: false)
- `telegram.allowed_user_ids`: List of Telegram user IDs allowed to interact with the bot

## Usage

```bash
# Run with default config
python main.py

# Run with custom config
python main.py --config config/my_config.yaml

# Run in demo mode (simulated prices, no exchange connection)
python main.py --demo

# Debug logging
python main.py --log-level DEBUG
```

## Backtesting

Replay historical price data through the same strategy pipeline used in live trading.

```bash
# Quick single-trade backtest
python run_backtest.py

# Custom params: BTC long, 1h candles, tight TP levels
python run_backtest.py --symbol BTC/USDC:USDC --side long --timeframe 1h --tp1 2 --tp2 4 --tp3 6

# Parameter sweep (find optimal trail%)
python run_backtest.py --sweep --candles 2000 --entry-mode every_candle

# Full grid search across TP levels × trail%
python run_backtest.py --sweep --full-sweep --candles 5000

# Export results to JSON
python run_backtest.py --output results.json
```

See [docs/backtesting.md](docs/backtesting.md) for full documentation.

## Project Structure

```
agentrade-bot/
  main.py                          # Entry point
  run_backtest.py                  # Backtest CLI runner
  config/
    default.yaml                   # Engine configuration
    strategies/                    # Strategy presets
  core/
    engine.py                      # Main orchestrator
    event_bus.py                   # Async pub/sub event system
    types.py                       # Data classes and enums
  backtest/
    engine.py                      # Backtest engine (sync, fast)
    data.py                        # Historical data provider (ccxt OHLCV)
  exchange/
    base.py                        # Unified exchange adapter (ccxt)
    rate_limiter.py                # Token bucket rate limiter
  execution/
    position_manager.py            # In-memory position tracking
    order_queue.py                 # Batching + deduplication + priority
    order_executor.py              # Exchange API order execution
  feed/
    price_feed.py                  # WebSocket price ingestion
  strategy/
    base.py                        # Abstract strategy interface
    multi_level_trail.py           # Multi-level TP with trailing SL
  persistence/
    state_store.py                 # Redis/file crash recovery
  notification/
    telegram.py                    # Telegram bot alerts
  docs/                            # Documentation
```

## Strategy: Multi-Level Trailing TP/SL

The default strategy manages positions through progressive take-profit levels:

1. **TP1** (default 3%) — Partial close 33%, move SL to breakeven
2. **TP2** (default 5%) — Partial close 33%, move SL to TP1 price
3. **TP3** (default 8%) — Activate tight trailing stop

Protections:
- **Wick protection**: 3s confirmation delay prevents false triggers on single-tick spikes
- **Mark price divergence**: Pauses trailing if mark/last price diverge >0.5%
- **Smart threshold**: Skips API calls if SL change is <0.1%
- **Anti-thundering-herd**: Staggered jitter prevents all users hitting exchange API simultaneously

## Dependencies

| Package | Purpose |
|---------|---------|
| `ccxt` | Exchange connectivity (REST + WebSocket) |
| `redis[hiredis]` | State persistence with C parser |
| `python-telegram-bot` | Telegram notifications |
| `structlog` | Structured logging |
| `pyyaml` | Config parsing |
| `numpy` | Numerical operations |
| `ta` | Technical analysis indicators |
| `python-dotenv` | Environment variable loading |
| `watchdog` | Config file hot-reload |
| `motor` / `pymongo` | MongoDB driver (future use) |

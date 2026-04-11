"""
Agentrade Engine v2 — Backtest Runner (Rust Core)

Run backtests from the command line against historical exchange data.
Uses the Rust backtest_core for 30-300M ticks/sec performance.

Usage:
    python run_backtest.py                                     # Defaults: SOL short, 1000 candles
    python run_backtest.py --symbol BTC/USDC:USDC --side long  # BTC long
    python run_backtest.py --timeframe 5m --candles 5000       # 5m candles, more data
    python run_backtest.py --tp1 2 --tp2 4 --tp3 6 --trail 1  # Custom strategy params
    python run_backtest.py --sweep                             # Parameter sweep mode
    python run_backtest.py --sweep --full-sweep                # Full grid search
"""

import sys
import argparse
import json
import time
from pathlib import Path
from datetime import datetime, timezone

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

import backtest_core
from backtest.data import fetch_ohlcv, candles_to_ticks


def build_config(args, strategy_overrides=None):
    """Build Rust-compatible config dict."""
    strategy = {
        "tp1_pct": args.tp1 or 3.0,
        "tp2_pct": args.tp2 or 5.0,
        "tp3_pct": args.tp3 or 8.0,
        "tp1_close_pct": args.tp1_close or 0.33,
        "tp2_close_pct": args.tp2_close or 0.33,
        "trail_pct": args.trail or 1.5,
        "min_sl_change_pct": args.min_sl_change or 0.1,
    }
    if strategy_overrides:
        strategy.update(strategy_overrides)

    entry_mode = args.entry_mode
    if entry_mode == "every_candle":
        entry_mode = "every_n"

    return {
        "strategy": strategy,
        "side": args.side,
        "position_size": args.size,
        "leverage": args.leverage,
        "initial_capital": args.capital,
        "slippage_pct": args.slippage,
        "fee_pct": args.fee,
        "entry_mode": entry_mode,
        "entry_interval": 4,  # every 4 ticks = every candle in OHLC mode
        "funding_rate_pct": args.funding_rate,
        "funding_interval_sec": 28800.0,
        "enable_liquidation": True,
        "equity_sample_interval": max(1, args.candles * 4 // 500),
    }


def print_results(result, symbol, side, timeframe, num_candles):
    """Print formatted backtest results."""
    m = result["metrics"]

    print()
    print("=" * 60)
    print("  BACKTEST RESULTS")
    print("=" * 60)
    print(f"  Symbol:       {symbol}")
    print(f"  Side:         {side}")
    print(f"  Timeframe:    {timeframe}")
    print(f"  Ticks:        {result['tick_count']:,}")
    print(f"  Engine:       Rust (backtest_core)")
    print(f"  Exec time:    {result['execution_time_ms']:.1f}ms")
    tps = result["tick_count"] / (result["execution_time_ms"] / 1000) if result["execution_time_ms"] > 0 else 0
    print(f"  Speed:        {tps/1e6:.1f}M ticks/sec")
    print()

    print("  PERFORMANCE")
    print("  " + "-" * 40)
    print(f"  Total trades:   {m['total_trades']}")
    print(f"  Winners:        {m['winners']}")
    print(f"  Losers:         {m['losers']}")
    print(f"  Win rate:       {m['win_rate']:.1f}%")
    print()
    print(f"  Total PnL:      ${m['total_pnl']:+.2f} ({m['total_pnl_pct']:+.2f}%)")
    print(f"  Avg win:        {m['avg_win_pct']:+.2f}%")
    print(f"  Avg loss:       {m['avg_loss_pct']:+.2f}%")
    print(f"  Profit factor:  {m['profit_factor']:.2f}")
    print(f"  Expectancy:     ${m['expectancy']:+.2f}")
    print()
    print(f"  Max drawdown:   {m['max_drawdown_pct']:.2f}%")
    print(f"  Sharpe:         {m['sharpe']:.2f}")
    print(f"  Sortino:        {m['sortino']:.2f}")
    print()

    if "tp1_hit_rate" in m:
        print(f"  TP1 hit rate:   {m['tp1_hit_rate']:.1f}%")
        print(f"  TP2 hit rate:   {m['tp2_hit_rate']:.1f}%")
        print(f"  TP3 hit rate:   {m['tp3_hit_rate']:.1f}%")
        print()

    print(f"  Funding paid:   ${m['total_funding_paid']:.2f}")
    print(f"  Fees paid:      ${m['total_fees_paid']:.2f}")

    # Trade log
    trades = result.get("trades", [])
    if trades:
        print()
        print("  TRADE LOG")
        print("  " + "-" * 80)
        print(f"  {'#':>3s}  {'Side':>5s}  {'Entry':>10s}  {'Exit':>10s}  {'PnL$':>9s}  {'PnL%':>7s}  {'Exit Reason':>14s}  {'TP1':>3s} {'TP2':>3s} {'TP3':>3s}")
        print("  " + "-" * 80)
        for t in trades[:50]:
            side_str = t["side"]
            tp1 = "Y" if t["tp1_hit"] else "-"
            tp2 = "Y" if t["tp2_hit"] else "-"
            tp3 = "Y" if t["tp3_hit"] else "-"
            print(
                f"  {t['trade_id']:>3d}  {side_str:>5s}  "
                f"${t['entry_price']:>9.2f}  ${t['exit_price']:>9.2f}  "
                f"${t['realized_pnl']:>+8.2f}  {t['realized_pnl_pct']:>+6.2f}%  "
                f"{t['exit_reason']:>14s}  "
                f" {tp1:>2s}  {tp2:>2s}  {tp3:>2s}"
            )
        if len(trades) > 50:
            print(f"  ... and {len(trades) - 50} more trades")

    print()
    print("=" * 60)


def run_single(args, timestamps=None, prices=None, history=None):
    """Run a single backtest."""
    config = build_config(args)

    print("=" * 60)
    print("  AGENTRADE BACKTEST ENGINE (Rust Core)")
    print("=" * 60)
    print(f"  Symbol:     {args.symbol}")
    print(f"  Side:       {args.side}")
    print(f"  Exchange:   {args.exchange}")
    print(f"  Timeframe:  {args.timeframe}")
    print(f"  Candles:    {args.candles}")
    print(f"  Entry mode: {args.entry_mode}")
    print(f"  Size:       {args.size}")
    print(f"  Leverage:   {args.leverage}x")
    print(f"  Capital:    ${args.capital}")
    print(f"  Slippage:   {args.slippage}%")
    print(f"  Fee:        {args.fee}%")
    print(f"  Funding:    {args.funding_rate}%/8h")
    print(f"  Strategy:   TP {config['strategy']['tp1_pct']}/{config['strategy']['tp2_pct']}/{config['strategy']['tp3_pct']}%  Trail {config['strategy']['trail_pct']}%")
    print("=" * 60)

    # Fetch data if not provided
    if timestamps is None or prices is None:
        print("\n  Fetching historical data...")
        history = fetch_ohlcv(
            symbol=args.symbol,
            exchange_id=args.exchange,
            timeframe=args.timeframe,
            limit=args.candles,
        )

        if not history.candles:
            print("  ERROR: No candle data returned")
            return None

        low, high = history.price_range
        print(f"  Data: {len(history.candles)} candles, {history.duration_hours:.1f}h")
        print(f"  Price range: ${low:.2f} - ${high:.2f}")

        ticks = candles_to_ticks(history.candles)
        timestamps = [t[0] for t in ticks]
        prices = [t[1] for t in ticks]

    print(f"  Ticks: {len(timestamps):,}")
    print("\n  Running Rust backtest engine...")

    config_json = json.dumps(config)
    result = backtest_core.run_backtest(timestamps, prices, config_json)

    print_results(result, args.symbol, args.side, args.timeframe, args.candles)

    # Export if requested
    if args.output:
        export_data = {
            "symbol": args.symbol,
            "side": args.side,
            "timeframe": args.timeframe,
            "config": config,
            "metrics": result["metrics"],
            "trades": result.get("trades", []),
            "tick_count": result["tick_count"],
            "execution_time_ms": result["execution_time_ms"],
        }
        with open(args.output, "w") as f:
            json.dump(export_data, f, indent=2, default=str)
        print(f"  Results exported to: {args.output}")

    return result


def run_sweep(args):
    """Run parameter sweep using Rust batch mode."""
    print("=" * 60)
    print("  PARAMETER SWEEP (Rust Core)")
    print("=" * 60)

    # Fetch data once
    print("\n  Fetching historical data...")
    history = fetch_ohlcv(
        symbol=args.symbol,
        exchange_id=args.exchange,
        timeframe=args.timeframe,
        limit=args.candles,
    )

    if not history.candles:
        print("  ERROR: No candle data returned")
        return

    low, high = history.price_range
    print(f"  Data: {len(history.candles)} candles, {history.duration_hours:.1f}h")
    print(f"  Price range: ${low:.2f} - ${high:.2f}")

    ticks = candles_to_ticks(history.candles)
    timestamps = [t[0] for t in ticks]
    prices = [t[1] for t in ticks]
    print(f"  Ticks: {len(timestamps):,}")

    # Build config grid
    if args.full_sweep:
        tp1_values = [1.5, 2.0, 3.0, 4.0]
        tp3_values = [5.0, 8.0, 10.0, 12.0]
        trail_values = [0.5, 1.0, 1.5, 2.0, 3.0]

        configs = []
        config_params = []
        for tp1 in tp1_values:
            for tp3 in tp3_values:
                if tp3 <= tp1:
                    continue
                tp2 = round((tp1 + tp3) / 2, 1)
                for trail in trail_values:
                    strategy = {
                        "tp1_pct": tp1, "tp2_pct": tp2, "tp3_pct": tp3,
                        "trail_pct": trail,
                        "tp1_close_pct": args.tp1_close or 0.33,
                        "tp2_close_pct": args.tp2_close or 0.33,
                        "min_sl_change_pct": args.min_sl_change or 0.1,
                    }
                    cfg = {
                        "strategy": strategy,
                        "side": args.side,
                        "position_size": args.size,
                        "leverage": args.leverage,
                        "initial_capital": args.capital,
                        "slippage_pct": args.slippage,
                        "fee_pct": args.fee,
                        "entry_mode": "every_n" if args.entry_mode == "every_candle" else args.entry_mode,
                        "entry_interval": 4,
                        "funding_rate_pct": args.funding_rate,
                        "funding_interval_sec": 28800.0,
                        "enable_liquidation": True,
                        "equity_sample_interval": max(1, args.candles * 4 // 100),
                    }
                    configs.append(json.dumps(cfg))
                    config_params.append(strategy)
    else:
        trail_values = [0.5, 1.0, 1.5, 2.0, 3.0]
        configs = []
        config_params = []
        for trail in trail_values:
            strategy = {
                "tp1_pct": args.tp1 or 3.0,
                "tp2_pct": args.tp2 or 5.0,
                "tp3_pct": args.tp3 or 8.0,
                "trail_pct": trail,
                "tp1_close_pct": args.tp1_close or 0.33,
                "tp2_close_pct": args.tp2_close or 0.33,
                "min_sl_change_pct": args.min_sl_change or 0.1,
            }
            cfg = {
                "strategy": strategy,
                "side": args.side,
                "position_size": args.size,
                "leverage": args.leverage,
                "initial_capital": args.capital,
                "slippage_pct": args.slippage,
                "fee_pct": args.fee,
                "entry_mode": "every_n" if args.entry_mode == "every_candle" else args.entry_mode,
                "entry_interval": 4,
                "funding_rate_pct": args.funding_rate,
                "funding_interval_sec": 28800.0,
                "enable_liquidation": True,
                "equity_sample_interval": max(1, args.candles * 4 // 100),
            }
            configs.append(json.dumps(cfg))
            config_params.append(strategy)

    print(f"\n  Running {len(configs)} configurations via Rust batch...")

    start = time.perf_counter()
    results = backtest_core.run_batch(timestamps, prices, configs)
    elapsed = time.perf_counter() - start

    print(f"  Completed in {elapsed*1000:.0f}ms ({elapsed/len(configs)*1000:.1f}ms/config)")

    # Combine and sort
    paired = list(zip(config_params, results))
    paired.sort(key=lambda x: x[1]["metrics"]["total_pnl"], reverse=True)

    # Print results table
    print()
    print("=" * 60)
    print("  SWEEP RESULTS (sorted by PnL)")
    print("=" * 60)
    print(f"  {'TP1':>4s} {'TP2':>4s} {'TP3':>4s} {'Trail':>5s}  {'PnL$':>9s} {'PnL%':>7s} {'WR':>5s} {'PF':>5s} {'DD':>6s} {'Sharpe':>6s} {'Trades':>6s}")
    print("  " + "-" * 72)

    for strategy, result in paired[:30]:
        m = result["metrics"]
        pf = m["profit_factor"]
        pf_str = f"{pf:5.2f}" if pf < 1000 else "  inf"
        print(
            f"  {strategy['tp1_pct']:>4.1f} {strategy['tp2_pct']:>4.1f} {strategy['tp3_pct']:>4.1f} {strategy['trail_pct']:>5.1f}  "
            f"${m['total_pnl']:>+8.2f} {m['total_pnl_pct']:>+6.1f}% "
            f"{m['win_rate']:>4.0f}% {pf_str} "
            f"{m['max_drawdown_pct']:>5.1f}% {m['sharpe']:>+5.2f} {m['total_trades']:>6d}"
        )

    if paired:
        best_strategy, best_result = paired[0]
        bm = best_result["metrics"]
        print()
        print("  BEST CONFIG:")
        for k, v in best_strategy.items():
            print(f"    {k}: {v}")
        print(f"  PnL: ${bm['total_pnl']:+.2f} ({bm['total_pnl_pct']:+.2f}%)")
        print(f"  Win Rate: {bm['win_rate']:.1f}% | PF: {bm['profit_factor']:.2f} | Sharpe: {bm['sharpe']:.2f}")
        print(f"  Max DD: {bm['max_drawdown_pct']:.2f}% | Expectancy: ${bm['expectancy']:+.2f}")


def main():
    parser = argparse.ArgumentParser(
        description="Agentrade Backtest Engine (Rust Core)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python run_backtest.py                                        # Quick default backtest
  python run_backtest.py --symbol BTC/USDC:USDC --side long     # BTC long
  python run_backtest.py --timeframe 5m --candles 5000          # More data
  python run_backtest.py --tp1 2 --tp2 4 --tp3 6 --trail 1     # Custom params
  python run_backtest.py --entry-mode every_candle              # Re-enter after each close
  python run_backtest.py --sweep                                # Trail sweep
  python run_backtest.py --sweep --full-sweep                   # Full grid search
  python run_backtest.py --output results.json                  # Export to JSON
        """,
    )

    # Data
    parser.add_argument("--symbol", default="SOL/USDC:USDC", help="Trading pair")
    parser.add_argument("--exchange", default="hyperliquid", help="Exchange")
    parser.add_argument("--timeframe", default="1m", help="Candle timeframe (1m, 5m, 15m, 1h, 4h, 1d)")
    parser.add_argument("--candles", type=int, default=1000, help="Number of candles to fetch")

    # Position
    parser.add_argument("--side", default="short", choices=["long", "short"], help="Trade direction")
    parser.add_argument("--size", type=float, default=1.0, help="Position size in base asset")
    parser.add_argument("--leverage", type=float, default=5.0, help="Leverage")
    parser.add_argument("--capital", type=float, default=1000.0, help="Initial capital USD")

    # Execution
    parser.add_argument("--slippage", type=float, default=0.05, help="Slippage %% per fill")
    parser.add_argument("--fee", type=float, default=0.035, help="Fee %% per side")
    parser.add_argument("--funding-rate", type=float, default=0.01, help="Funding rate %% per 8h")
    parser.add_argument("--entry-mode", default="every_candle", choices=["single", "every_candle"], help="Entry mode")
    parser.add_argument("--tick-mode", default="ohlc", choices=["ohlc", "close"], help="Tick simulation mode")

    # Strategy overrides
    parser.add_argument("--tp1", type=float, default=None, help="TP1 %% (default: 3.0)")
    parser.add_argument("--tp2", type=float, default=None, help="TP2 %% (default: 5.0)")
    parser.add_argument("--tp3", type=float, default=None, help="TP3 %% (default: 8.0)")
    parser.add_argument("--trail", type=float, default=None, help="Trail %% (default: 1.5)")
    parser.add_argument("--tp1-close", type=float, default=None, help="TP1 close %% (default: 0.33)")
    parser.add_argument("--tp2-close", type=float, default=None, help="TP2 close %% (default: 0.33)")
    parser.add_argument("--min-sl-change", type=float, default=None, help="Min SL change %% (default: 0.1)")

    # Sweep
    parser.add_argument("--sweep", action="store_true", help="Run parameter sweep")
    parser.add_argument("--full-sweep", action="store_true", help="Full grid search (with --sweep)")

    # Output
    parser.add_argument("--output", "-o", default=None, help="Export results to JSON file")
    parser.add_argument("--max-trades", type=int, default=50, help="Max trades to show in table")

    args = parser.parse_args()

    if args.sweep:
        run_sweep(args)
    else:
        run_single(args)


if __name__ == "__main__":
    main()

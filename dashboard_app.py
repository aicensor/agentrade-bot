"""
Agentrade — Live Backtest Dashboard (Flask)

Interactive web app for backtesting with real-time parameter tuning.
Includes simulation replay mode with play/pause/speed controls.

Usage:
    python dashboard_app.py                    # Start on port 8899
    python dashboard_app.py --port 9000        # Custom port
"""

import sys
import json
import time
import argparse
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent))

from flask import Flask, jsonify, request, Response

import asyncio

import backtest_core
import lfest_core

# ── Data cache ────────────────────────────────────────────────────────────────
_cache = {}


TIMEFRAME_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240}
# Hyperliquid API limit: 5000 candles per request
# Max days per timeframe: 1m=3.5d, 5m=17d, 15m=52d, 1h=208d, 4h=833d

def get_data(symbol, exchange, days, timeframe="5m"):
    """Fetch and cache tick data at the specified timeframe."""
    key = f"{symbol}|{exchange}|{days}|{timeframe}"
    if key in _cache:
        ts_data, px_data, candles, meta = _cache[key]
        if time.time() - meta["fetched_at"] < 300:
            return ts_data, px_data, candles, meta

    import ccxt
    ex = getattr(ccxt, exchange)({"options": {"defaultType": "swap"}})
    ex.load_markets()

    now_ms = int(time.time() * 1000)
    start_ms = now_ms - (days * 24 * 60 * 60 * 1000)

    tf_min = TIMEFRAME_MINUTES.get(timeframe, 5)
    max_candles_needed = int(days * 24 * 60 / tf_min)

    all_candles = []
    if max_candles_needed <= 5000:
        # Single fetch covers the whole range
        all_candles = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=5000, since=start_ms)
    else:
        # Need multiple fetches (paginate) or fall back to larger TF for older data
        # Strategy: fetch the target TF (capped at 5000), then backfill with next-larger TF
        backfill_tf = {"1m": "5m", "5m": "15m", "15m": "1h", "1h": "4h"}.get(timeframe)

        primary = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=5000, since=start_ms)
        time.sleep(0.3)

        if backfill_tf and primary:
            # Fetch larger TF for the period before primary data starts
            primary_start = primary[0][0]
            if primary_start > start_ms:
                backfill = ex.fetch_ohlcv(symbol, timeframe=backfill_tf, limit=5000, since=start_ms)
                older = [c for c in backfill if c[0] < primary_start]
                all_candles = older + primary
            else:
                all_candles = primary
        else:
            all_candles = primary

    all_candles.sort(key=lambda c: c[0])

    # Remove duplicates by timestamp
    seen = set()
    deduped = []
    for c in all_candles:
        if c[0] not in seen:
            seen.add(c[0])
            deduped.append(c)
    all_candles = deduped

    if not all_candles:
        raise ValueError(f"No candle data returned for {symbol} {timeframe}")

    # Synthesize 4 ticks per candle: O, H, L, C
    timestamps = []
    prices = []
    for c in all_candles:
        ts = c[0] / 1000
        timestamps.extend([ts, ts + 1, ts + 2, ts + 3])
        prices.extend([c[1], c[2], c[3], c[4]])

    hours = (all_candles[-1][0] - all_candles[0][0]) / (1000 * 3600)
    meta = {
        "candles": len(all_candles),
        "ticks": len(timestamps),
        "days": round(hours / 24, 1),
        "timeframe": timeframe,
        "price_low": min(c[3] for c in all_candles),
        "price_high": max(c[2] for c in all_candles),
        "fetched_at": time.time(),
    }

    _cache[key] = (timestamps, prices, all_candles, meta)
    return timestamps, prices, all_candles, meta


# ── Flask app ─────────────────────────────────────────────────────────────────
app = Flask(__name__)


@app.route("/")
def index():
    return Response(DASHBOARD_HTML, mimetype="text/html")


@app.route("/api/backtest", methods=["POST"])
def api_backtest():
    """Run backtest with given parameters, return JSON results."""
    params = request.json
    t0 = time.perf_counter()

    symbol = params.get("symbol", "SOL/USDC:USDC")
    exchange = params.get("exchange", "hyperliquid")
    days = params.get("days", 30)
    side = params.get("side", "long")
    timeframe = params.get("timeframe", "5m")

    try:
        ts_data, px_data, candles, meta = get_data(symbol, exchange, days, timeframe)
    except Exception as e:
        return jsonify({"error": f"Data fetch failed: {e}"}), 400

    config = {
        "strategy": {
            "tp1_pct": params.get("tp1", 3.0),
            "tp2_pct": params.get("tp2", 5.0),
            "tp3_pct": params.get("tp3", 8.0),
            "tp1_close_pct": params.get("tp1_close", 0.33),
            "tp2_close_pct": params.get("tp2_close", 0.33),
            "trail_pct": params.get("exit_trail", 1.5),
            "min_sl_change_pct": params.get("min_sl_change", 0.1),
        },
        "side": side,
        "position_size": params.get("size", 1.0),
        "leverage": params.get("leverage", 5.0),
        "initial_capital": params.get("capital", 1000.0),
        "slippage_pct": params.get("slippage", 0.05),
        "fee_pct": params.get("fee", 0.035),
        "entry_mode": "every_n",
        "entry_interval": 1,
        "funding_rate_pct": params.get("funding_rate", 0.01),
        "funding_interval_sec": 28800.0,
        "enable_liquidation": True,
        "equity_sample_interval": max(1, len(ts_data) // 500),
    }

    te = params.get("trailing_entry", {})
    config["trailing_entry"] = {
        "enabled": te.get("enabled", False),
        "trail_pct": te.get("trail_pct", 2.0),
        "atr_enabled": te.get("atr_enabled", False),
        "atr_period": te.get("atr_period", 14),
        "atr_multiplier": te.get("atr_multiplier", 1.5),
        "timeout_sec": te.get("timeout_sec", 0),
        "max_adverse_pct": te.get("max_adverse_pct", 0),
    }

    sides_to_run = ["long", "short"] if side == "both" else [side]
    all_results = {}
    for s in sides_to_run:
        cfg = {**config, "side": s}
        all_results[s] = backtest_core.run_backtest(ts_data, px_data, json.dumps(cfg))

    primary_side = sides_to_run[0]
    result = all_results[primary_side]

    # OHLCV candle data for TradingView chart
    step = max(1, len(candles) // 800)
    sampled_candles = candles[::step]
    price_chart = {
        "candles": [
            {
                "time": int(c[0] / 1000),
                "open": round(c[1], 4),
                "high": round(c[2], 4),
                "low": round(c[3], 4),
                "close": round(c[4], 4),
                "volume": round(c[5], 2) if len(c) > 5 and c[5] else 0,
            }
            for c in sampled_candles
        ],
    }

    # Merge trades from all sides
    trades = []
    all_raw_trades = []
    for s in sides_to_run:
        r = all_results[s]
        for t in r.get("trades", []):
            all_raw_trades.append(t)
            if len(trades) < 400:
                trades.append({
                    "id": t["trade_id"],
                    "side": t.get("side", s),
                    "entry": round(t["entry_price"], 2),
                    "exit": round(t["exit_price"], 2),
                    "entry_ts": t["entry_time"],
                    "exit_ts": t["exit_time"],
                    "entry_time": datetime.fromtimestamp(t["entry_time"], tz=timezone.utc).strftime("%m/%d %H:%M"),
                    "exit_time": datetime.fromtimestamp(t["exit_time"], tz=timezone.utc).strftime("%m/%d %H:%M"),
                    "pnl": round(t["realized_pnl"], 2),
                    "pnl_pct": round(t["realized_pnl_pct"], 2),
                    "reason": t["exit_reason"],
                    "tp1": t["tp1_hit"], "tp2": t["tp2_hit"], "tp3": t["tp3_hit"],
                    "signal": round(t.get("signal_price", 0), 2),
                    "improvement": round(t.get("entry_improvement_pct", 0), 2),
                })
    trades.sort(key=lambda t: t["entry_ts"])

    if side == "both":
        m_long = all_results["long"]["metrics"]
        m_short = all_results["short"]["metrics"]
        total_trades = m_long["total_trades"] + m_short["total_trades"]
        total_winners = m_long["winners"] + m_short["winners"]
        total_losers = m_long["losers"] + m_short["losers"]
        m = {
            "total_trades": total_trades,
            "winners": total_winners,
            "losers": total_losers,
            "win_rate": round(total_winners / total_trades * 100, 2) if total_trades > 0 else 0,
            "total_pnl": m_long["total_pnl"] + m_short["total_pnl"],
            "total_pnl_pct": m_long["total_pnl_pct"] + m_short["total_pnl_pct"],
            "profit_factor": (m_long["profit_factor"] + m_short["profit_factor"]) / 2,
            "max_drawdown_pct": max(m_long["max_drawdown_pct"], m_short["max_drawdown_pct"]),
            "sharpe": (m_long["sharpe"] + m_short["sharpe"]) / 2,
            "sortino": (m_long["sortino"] + m_short["sortino"]) / 2,
            "expectancy": (m_long["expectancy"] + m_short["expectancy"]) / 2,
            "avg_win_pct": (m_long["avg_win_pct"] + m_short["avg_win_pct"]) / 2,
            "avg_loss_pct": (m_long["avg_loss_pct"] + m_short["avg_loss_pct"]) / 2,
            "tp1_hit_rate": (m_long["tp1_hit_rate"] + m_short["tp1_hit_rate"]) / 2,
            "tp2_hit_rate": (m_long["tp2_hit_rate"] + m_short["tp2_hit_rate"]) / 2,
            "tp3_hit_rate": (m_long["tp3_hit_rate"] + m_short["tp3_hit_rate"]) / 2,
            "total_funding_paid": m_long["total_funding_paid"] + m_short["total_funding_paid"],
            "total_fees_paid": m_long["total_fees_paid"] + m_short["total_fees_paid"],
            "avg_entry_improvement_pct": (m_long.get("avg_entry_improvement_pct", 0) + m_short.get("avg_entry_improvement_pct", 0)) / 2,
            "signals_expired": m_long.get("signals_expired", 0) + m_short.get("signals_expired", 0),
            "signals_cancelled": m_long.get("signals_cancelled", 0) + m_short.get("signals_cancelled", 0),
        }
        eq = all_results["long"]["equity_curve"]
    else:
        m = result["metrics"]
        eq = result["equity_curve"]

    eq_step = max(1, len(eq) // 300)
    equity_chart = {
        "labels": [datetime.fromtimestamp(e[0], tz=timezone.utc).strftime("%m/%d %H:%M") for e in eq[::eq_step]],
        "data": [round(e[1], 2) for e in eq[::eq_step]],
    }

    equity_short = None
    if side == "both":
        eq_s = all_results["short"]["equity_curve"]
        eq_s_step = max(1, len(eq_s) // 300)
        equity_short = [round(e[1], 2) for e in eq_s[::eq_s_step]]

    elapsed = time.perf_counter() - t0
    engine_ms = sum(r["execution_time_ms"] for r in all_results.values())

    resp = {
        "meta": meta,
        "metrics": m,
        "price_chart": price_chart,
        "equity_chart": equity_chart,
        "trades": trades,
        "pnl_dist": [round(t["realized_pnl_pct"], 2) for t in all_raw_trades],
        "elapsed_ms": round(elapsed * 1000, 1),
        "engine_ms": round(engine_ms, 2),
        "mode": side,
        "config": {
            "tp1_pct": config["strategy"]["tp1_pct"],
            "tp2_pct": config["strategy"]["tp2_pct"],
            "tp3_pct": config["strategy"]["tp3_pct"],
            "trail_pct": config["strategy"]["trail_pct"],
            "leverage": config["leverage"],
        },
    }
    if equity_short is not None:
        resp["equity_short"] = equity_short
    return jsonify(resp)


@app.route("/api/compare", methods=["POST"])
def api_compare():
    params = request.json
    symbol = params.get("symbol", "SOL/USDC:USDC")
    exchange = params.get("exchange", "hyperliquid")
    days = params.get("days", 30)
    side = params.get("side", "long")
    timeframe = params.get("timeframe", "5m")

    try:
        ts_data, px_data, candles, meta = get_data(symbol, exchange, days, timeframe)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    compare_side = side if side != "both" else "long"
    base = {
        "strategy": {
            "tp1_pct": params.get("tp1", 3.0), "tp2_pct": params.get("tp2", 5.0),
            "tp3_pct": params.get("tp3", 8.0), "tp1_close_pct": params.get("tp1_close", 0.33),
            "tp2_close_pct": params.get("tp2_close", 0.33), "trail_pct": params.get("exit_trail", 1.5),
            "min_sl_change_pct": 0.1,
        },
        "side": compare_side, "position_size": params.get("size", 1.0),
        "leverage": params.get("leverage", 5.0), "initial_capital": params.get("capital", 1000.0),
        "slippage_pct": params.get("slippage", 0.05), "fee_pct": params.get("fee", 0.035),
        "entry_mode": "every_n", "entry_interval": 1,
        "funding_rate_pct": params.get("funding_rate", 0.01), "funding_interval_sec": 28800.0,
        "enable_liquidation": True, "equity_sample_interval": max(1, len(ts_data) // 300),
    }

    modes = [
        ("Immediate", {"enabled": False}),
        ("Trail 1%", {"enabled": True, "trail_pct": 1.0}),
        ("Trail 2%", {"enabled": True, "trail_pct": 2.0}),
        ("Trail 3%", {"enabled": True, "trail_pct": 3.0}),
        ("ATR x1.0", {"enabled": True, "atr_enabled": True, "atr_period": 14, "atr_multiplier": 1.0}),
        ("ATR x1.5", {"enabled": True, "atr_enabled": True, "atr_period": 14, "atr_multiplier": 1.5}),
    ]

    configs, labels = [], []
    for label, te in modes:
        configs.append(json.dumps({**base, "trailing_entry": te}))
        labels.append(label)

    t0 = time.perf_counter()
    results = backtest_core.run_batch(ts_data, px_data, configs)
    elapsed = time.perf_counter() - t0

    comparison, equity_curves = [], []
    for label, r in zip(labels, results):
        rm = r["metrics"]
        comparison.append({
            "label": label, "trades": rm["total_trades"], "wr": round(rm["win_rate"], 1),
            "pnl": round(rm["total_pnl"], 2), "pnl_pct": round(rm["total_pnl_pct"], 2),
            "pf": round(rm["profit_factor"], 2) if rm["profit_factor"] < 1000 else 999,
            "sharpe": round(rm["sharpe"], 2), "sortino": round(rm["sortino"], 2),
            "dd": round(rm["max_drawdown_pct"], 2), "expectancy": round(rm["expectancy"], 2),
            "avg_imp": round(rm.get("avg_entry_improvement_pct", 0), 2),
            "tp1": round(rm.get("tp1_hit_rate", 0), 1), "tp2": round(rm.get("tp2_hit_rate", 0), 1),
            "tp3": round(rm.get("tp3_hit_rate", 0), 1),
        })
        eq = r["equity_curve"]
        eq_step = max(1, len(eq) // 300)
        equity_curves.append({"label": label, "data": [round(e[1], 2) for e in eq[::eq_step]]})

    return jsonify({"comparison": comparison, "equity_curves": equity_curves, "elapsed_ms": round(elapsed * 1000, 1)})


@app.route("/api/sweep", methods=["POST"])
def api_sweep():
    params = request.json
    symbol = params.get("symbol", "SOL/USDC:USDC")
    exchange = params.get("exchange", "hyperliquid")
    days = params.get("days", 30)
    side = params.get("side", "long")
    timeframe = params.get("timeframe", "5m")

    try:
        ts_data, px_data, _, meta = get_data(symbol, exchange, days, timeframe)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    sweep_side = side if side != "both" else "long"
    base = {
        "side": sweep_side, "position_size": params.get("size", 1.0),
        "leverage": params.get("leverage", 5.0), "initial_capital": params.get("capital", 1000.0),
        "slippage_pct": params.get("slippage", 0.05), "fee_pct": params.get("fee", 0.035),
        "entry_mode": "every_n", "entry_interval": 1,
        "funding_rate_pct": params.get("funding_rate", 0.01), "funding_interval_sec": 28800.0,
        "enable_liquidation": True, "equity_sample_interval": max(1, len(ts_data) // 100),
    }
    te_cfg = params.get("trailing_entry", {"enabled": False})

    configs, sweep_params = [], []
    for tp1 in [1.5, 2.0, 3.0, 4.0]:
        for tp3 in [5.0, 8.0, 10.0, 12.0]:
            if tp3 <= tp1: continue
            tp2 = round((tp1 + tp3) / 2, 1)
            for trail in [0.5, 1.0, 1.5, 2.0, 3.0]:
                cfg = {**base, "strategy": {"tp1_pct": tp1, "tp2_pct": tp2, "tp3_pct": tp3, "tp1_close_pct": 0.33, "tp2_close_pct": 0.33, "trail_pct": trail, "min_sl_change_pct": 0.1}, "trailing_entry": te_cfg}
                configs.append(json.dumps(cfg))
                sweep_params.append({"tp1": tp1, "tp2": tp2, "tp3": tp3, "trail": trail})

    t0 = time.perf_counter()
    results = backtest_core.run_batch(ts_data, px_data, configs)
    elapsed = time.perf_counter() - t0

    paired = sorted(zip(sweep_params, results), key=lambda x: x[1]["metrics"]["total_pnl"], reverse=True)
    sweep_data = []
    for p, r in paired[:30]:
        rm = r["metrics"]
        sweep_data.append({**p, "pnl": round(rm["total_pnl"], 2), "wr": round(rm["win_rate"], 1),
            "pf": round(rm["profit_factor"], 2) if rm["profit_factor"] < 1000 else 999,
            "sharpe": round(rm["sharpe"], 2), "dd": round(rm["max_drawdown_pct"], 2), "trades": rm["total_trades"]})

    return jsonify({"sweep": sweep_data, "total_configs": len(configs), "elapsed_ms": round(elapsed * 1000, 1)})


@app.route("/api/sim", methods=["POST"])
def api_sim():
    """Run Mode 2 event-driven simulation using real tick data."""
    from backtest.tick_data import fetch_trades_ccxt, Tick, TickStream, candles_to_tick_stream
    from backtest.sim_engine import run_simulation

    params = request.json
    t0 = time.perf_counter()

    symbol = params.get("symbol", "SOL/USDC:USDC")
    exchange = params.get("exchange", "hyperliquid")
    days = params.get("days", 3)
    side = params.get("side", "short")
    timeframe = params.get("timeframe", "5m")
    use_real_ticks = params.get("use_real_ticks", False)

    strategy_config = {
        "tp1_pct": params.get("tp1", 3.0),
        "tp2_pct": params.get("tp2", 5.0),
        "tp3_pct": params.get("tp3", 8.0),
        "tp1_close_pct": params.get("tp1_close", 0.33),
        "tp2_close_pct": params.get("tp2_close", 0.33),
        "tp3_close_pct": 0.0,
        "trail_pct": params.get("exit_trail", 1.5),
        "min_sl_change_pct": 0.1,
        "sl_type": "limit",
        "sl_buffer_pct": 0.3,
        "wick_protection": False,
    }

    try:
        if use_real_ticks:
            # Fetch real trades from exchange
            now_ms = int(time.time() * 1000)
            since_ms = now_ms - (days * 24 * 60 * 60 * 1000)
            tick_stream = fetch_trades_ccxt(
                symbol=symbol,
                exchange_id=exchange,
                limit=1000,
                since_ms=since_ms,
                max_pages=min(days * 5, 50),
            )
        else:
            # Use OHLCV candles → synthetic ticks (faster fallback)
            ts_data, px_data, candles, meta = get_data(symbol, exchange, days, timeframe)
            ticks = []
            for c in candles:
                ts_sec = c[0] / 1000
                ticks.append(Tick(timestamp=ts_sec, price=c[1], quantity=1.0))       # open
                ticks.append(Tick(timestamp=ts_sec + 1, price=c[2], quantity=1.0))    # high
                ticks.append(Tick(timestamp=ts_sec + 2, price=c[3], quantity=1.0))    # low
                ticks.append(Tick(timestamp=ts_sec + 3, price=c[4], quantity=1.0))    # close
            tick_stream = TickStream(symbol=symbol, exchange=exchange, ticks=ticks)

        if tick_stream.tick_count == 0:
            return jsonify({"error": "No tick data available"}), 400

        sim_side = side if side != "both" else "short"

        result = asyncio.run(run_simulation(
            tick_stream,
            symbol=symbol,
            exchange=exchange,
            side=sim_side,
            strategy_config=strategy_config,
            position_size=params.get("size", 1.0),
            leverage=params.get("leverage", 5.0),
            initial_capital=params.get("capital", 1000.0),
            entry_mode=params.get("entry_mode", "every_n"),
            entry_interval=params.get("entry_interval", 100),
            slippage_pct=params.get("slippage", 0.05),
            fee_pct=params.get("fee", 0.035),
        ))

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"Simulation failed: {e}"}), 400

    # Build OHLCV candles for chart from tick data (group into 5m candles)
    if use_real_ticks:
        # Group ticks into candles for display
        tf_seconds = TIMEFRAME_MINUTES.get(timeframe, 5) * 60
        candle_buckets = {}
        for tick in tick_stream.ticks:
            bucket = int(tick.timestamp // tf_seconds) * tf_seconds
            if bucket not in candle_buckets:
                candle_buckets[bucket] = {"o": tick.price, "h": tick.price, "l": tick.price, "c": tick.price, "v": tick.quantity}
            else:
                b = candle_buckets[bucket]
                b["h"] = max(b["h"], tick.price)
                b["l"] = min(b["l"], tick.price)
                b["c"] = tick.price
                b["v"] += tick.quantity
        sorted_times = sorted(candle_buckets.keys())
        chart_candles = [{"time": int(t), "open": round(candle_buckets[t]["o"], 4), "high": round(candle_buckets[t]["h"], 4), "low": round(candle_buckets[t]["l"], 4), "close": round(candle_buckets[t]["c"], 4), "volume": round(candle_buckets[t]["v"], 2)} for t in sorted_times]
    else:
        # Use existing candles
        ts_data, px_data, candles, meta = get_data(symbol, exchange, days, timeframe)
        step = max(1, len(candles) // 800)
        sampled = candles[::step]
        chart_candles = [{"time": int(c[0] / 1000), "open": round(c[1], 4), "high": round(c[2], 4), "low": round(c[3], 4), "close": round(c[4], 4), "volume": round(c[5], 2) if len(c) > 5 and c[5] else 0} for c in sampled]

    # Format trades
    trades = []
    for t in result.trades[:400]:
        trades.append({
            "id": t.trade_id,
            "side": t.side,
            "entry": round(t.entry_price, 2),
            "exit": round(t.exit_price, 2),
            "entry_ts": t.entry_time,
            "exit_ts": t.exit_time,
            "entry_time": datetime.fromtimestamp(t.entry_time, tz=timezone.utc).strftime("%m/%d %H:%M"),
            "exit_time": datetime.fromtimestamp(t.exit_time, tz=timezone.utc).strftime("%m/%d %H:%M"),
            "pnl": round(t.realized_pnl, 2),
            "pnl_pct": round(t.realized_pnl_pct, 2),
            "reason": t.exit_reason,
            "tp1": t.tp1_hit, "tp2": t.tp2_hit, "tp3": t.tp3_hit,
            "signal": 0, "improvement": 0,
        })

    # Equity curve
    eq = result.equity_curve
    eq_step = max(1, len(eq) // 300)
    equity_chart = {
        "labels": [datetime.fromtimestamp(e[0], tz=timezone.utc).strftime("%m/%d %H:%M") for e in eq[::eq_step]],
        "data": [round(e[1], 2) for e in eq[::eq_step]],
    }

    # Metrics
    m = {
        "total_trades": result.total_trades,
        "winners": len(result.winners),
        "losers": len(result.losers),
        "win_rate": round(result.win_rate, 2),
        "total_pnl": round(result.total_pnl, 2),
        "total_pnl_pct": round(result.total_pnl_pct, 2),
        "profit_factor": round(result.profit_factor, 2) if result.profit_factor < 1000 else 999,
        "max_drawdown_pct": round(result.max_drawdown_pct, 2),
        "sharpe": 0, "sortino": 0,
        "expectancy": round(result.total_pnl / max(result.total_trades, 1), 2),
        "avg_win_pct": round(sum(t.realized_pnl_pct for t in result.winners) / max(len(result.winners), 1), 2),
        "avg_loss_pct": round(sum(t.realized_pnl_pct for t in result.losers) / max(len(result.losers), 1), 2),
        "tp1_hit_rate": round(sum(1 for t in result.trades if t.tp1_hit) / max(result.total_trades, 1) * 100, 1),
        "tp2_hit_rate": round(sum(1 for t in result.trades if t.tp2_hit) / max(result.total_trades, 1) * 100, 1),
        "tp3_hit_rate": round(sum(1 for t in result.trades if t.tp3_hit) / max(result.total_trades, 1) * 100, 1),
        "total_funding_paid": 0,
        "total_fees_paid": round(sum(t.fees_paid for t in result.trades), 2),
        "avg_entry_improvement_pct": 0,
        "signals_expired": 0, "signals_cancelled": 0,
    }

    elapsed = time.perf_counter() - t0
    resp = {
        "meta": {
            "candles": len(chart_candles),
            "ticks": tick_stream.tick_count,
            "days": round(tick_stream.duration_hours / 24, 1) if tick_stream.duration_hours else days,
            "timeframe": timeframe,
            "mode": "Mode 2 (Event-Driven)",
            "tick_source": "real" if use_real_ticks else "synthetic",
            "ticks_per_sec": round(result.tick_count / max(result.execution_time_sec, 0.001)),
            "fetched_at": time.time(),
        },
        "metrics": m,
        "price_chart": {"candles": chart_candles},
        "equity_chart": equity_chart,
        "trades": trades,
        "pnl_dist": [round(t.realized_pnl_pct, 2) for t in result.trades],
        "elapsed_ms": round(elapsed * 1000, 1),
        "engine_ms": round(result.execution_time_sec * 1000, 2),
        "mode": sim_side,
        "sim_mode": True,
        "config": {
            "tp1_pct": strategy_config["tp1_pct"],
            "tp2_pct": strategy_config["tp2_pct"],
            "tp3_pct": strategy_config["tp3_pct"],
            "trail_pct": strategy_config["trail_pct"],
            "leverage": params.get("leverage", 5.0),
        },
        "pipeline_stats": result.summary().get("pipeline_stats", {}),
    }

    return jsonify(resp)


@app.route("/api/tick_files", methods=["GET"])
def api_tick_files():
    """List available real tick data files."""
    from backtest.tick_collector import list_tick_files
    coin = request.args.get("coin")
    files = list_tick_files(coin)
    return jsonify(files)


@app.route("/api/lfest", methods=["POST"])
def api_lfest():
    """Run Mode 3 backtest using LFEST-rs exchange simulator.

    Data sources (in priority order):
      1. tick_file: path to pre-collected tick CSV (sub-1s real data)
      2. use_real_ticks=true: fetch from exchange API (limited by exchange)
      3. OHLCV candles → synthetic ticks (fallback)
    """
    from backtest.tick_collector import load_ticks as load_tick_file, list_tick_files

    params = request.json
    t0 = time.perf_counter()

    symbol = params.get("symbol", "SOL/USDC:USDC")
    exchange_id = params.get("exchange", "hyperliquid")
    days = params.get("days", 30)
    side = params.get("side", "long")
    timeframe = params.get("timeframe", "5m")
    tick_file = params.get("tick_file")  # Path to pre-collected tick data

    tick_source = "synthetic"
    chart_candles = []

    try:
        if tick_file:
            # Load pre-collected real tick data (sub-1s resolution)
            ts_data, px_data = load_tick_file(Path(tick_file))
            if not ts_data:
                return jsonify({"error": f"No data in tick file: {tick_file}"}), 400
            tick_source = "real (file)"
            days = (ts_data[-1] - ts_data[0]) / 86400

            # Build candles from ticks for chart — auto-select timeframe
            data_hours = (ts_data[-1] - ts_data[0]) / 3600 if len(ts_data) > 1 else 0
            if data_hours < 2:
                tf_seconds = 60
            elif data_hours < 8:
                tf_seconds = 60
            else:
                tf_seconds = TIMEFRAME_MINUTES.get(timeframe, 5) * 60
            candle_buckets = {}
            for ts_val, px_val in zip(ts_data, px_data):
                bucket = int(ts_val // tf_seconds) * tf_seconds
                if bucket not in candle_buckets:
                    candle_buckets[bucket] = {"o": px_val, "h": px_val, "l": px_val, "c": px_val, "v": 1}
                else:
                    b = candle_buckets[bucket]
                    b["h"] = max(b["h"], px_val)
                    b["l"] = min(b["l"], px_val)
                    b["c"] = px_val
                    b["v"] += 1
            sorted_times = sorted(candle_buckets.keys())
            chart_candles = [{"time": int(t), "open": round(candle_buckets[t]["o"], 4), "high": round(candle_buckets[t]["h"], 4), "low": round(candle_buckets[t]["l"], 4), "close": round(candle_buckets[t]["c"], 4), "volume": candle_buckets[t]["v"]} for t in sorted_times]
        else:
            # Use 1m candles (highest resolution from Hyperliquid) for maximum ticks
            # If tick files exist with enough data, prefer those
            coin = symbol.split("/")[0]
            tick_files = [f for f in list_tick_files(coin) if f["ticks"] >= 5000]
            if tick_files:
                best = max(tick_files, key=lambda f: f["ticks"])
                ts_data, px_data = load_tick_file(Path(best["path"]))
                tick_source = f"real ({best['name']})"
                days = (ts_data[-1] - ts_data[0]) / 86400 if ts_data else days
                # Build chart candles from ticks
                tf_seconds = max(60, int((ts_data[-1] - ts_data[0]) / 200)) if len(ts_data) > 1 else 60
                candle_buckets = {}
                for ts_val, px_val in zip(ts_data, px_data):
                    bucket = int(ts_val // tf_seconds) * tf_seconds
                    if bucket not in candle_buckets:
                        candle_buckets[bucket] = {"o": px_val, "h": px_val, "l": px_val, "c": px_val, "v": 1}
                    else:
                        b = candle_buckets[bucket]
                        b["h"] = max(b["h"], px_val)
                        b["l"] = min(b["l"], px_val)
                        b["c"] = px_val
                        b["v"] += 1
                sorted_times = sorted(candle_buckets.keys())
                chart_candles = [{"time": int(t), "open": round(candle_buckets[t]["o"], 4), "high": round(candle_buckets[t]["h"], 4), "low": round(candle_buckets[t]["l"], 4), "close": round(candle_buckets[t]["c"], 4), "volume": candle_buckets[t]["v"]} for t in sorted_times]
            else:
                # Default: fetch 1m OHLCV candles → 4 ticks each (best historical resolution)
                ts_data, px_data, candles, meta = get_data(symbol, exchange_id, days, "1m")
                tick_source = f"1m candles ({len(candles):,} candles → {len(ts_data):,} ticks)"
                # Aggregate 1m candles into user-selected timeframe for chart display
                tf_seconds = TIMEFRAME_MINUTES.get(timeframe, 5) * 60
                candle_buckets = {}
                for c in candles:
                    ts_sec = int(c[0] / 1000)
                    bucket = (ts_sec // tf_seconds) * tf_seconds
                    vol = round(c[5], 2) if len(c) > 5 and c[5] else 0
                    if bucket not in candle_buckets:
                        candle_buckets[bucket] = {"o": c[1], "h": c[2], "l": c[3], "c": c[4], "v": vol}
                    else:
                        b = candle_buckets[bucket]
                        b["h"] = max(b["h"], c[2])
                        b["l"] = min(b["l"], c[3])
                        b["c"] = c[4]
                        b["v"] += vol
                sorted_times = sorted(candle_buckets.keys())
                chart_candles = [
                    {"time": t, "open": round(candle_buckets[t]["o"], 4), "high": round(candle_buckets[t]["h"], 4),
                     "low": round(candle_buckets[t]["l"], 4), "close": round(candle_buckets[t]["c"], 4),
                     "volume": round(candle_buckets[t]["v"], 2)}
                    for t in sorted_times
                ]
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"Data fetch failed: {e}"}), 400

    config = {
        "strategy": {
            "tp1_pct": params.get("tp1", 3.0),
            "tp2_pct": params.get("tp2", 5.0),
            "tp3_pct": params.get("tp3", 8.0),
            "tp1_close_pct": params.get("tp1_close", 0.33),
            "tp2_close_pct": params.get("tp2_close", 0.33),
            "trail_pct": params.get("exit_trail", 1.5),
            "min_sl_change_pct": params.get("min_sl_change", 0.1),
        },
        "side": side if side != "both" else "short",
        "position_size": params.get("size", 1.0),
        "leverage": int(params.get("leverage", 5)),
        "initial_capital": params.get("capital", 1000.0),
        "spread_pct": params.get("spread_pct", 0.01),
        "fee_maker_bps": int(params.get("fee_maker_bps", 2)),
        "fee_taker_bps": int(params.get("fee_taker_bps", 6)),
        "entry_mode": "every_n",
        "entry_interval": 1,
        "equity_sample_interval": max(1, len(ts_data) // 500),
    }

    te = params.get("trailing_entry", {})
    config["trailing_entry"] = {
        "enabled": te.get("enabled", False),
        "trail_pct": te.get("trail_pct", 2.0),
        "atr_enabled": te.get("atr_enabled", False),
        "atr_period": te.get("atr_period", 14),
        "atr_multiplier": te.get("atr_multiplier", 1.5),
        "timeout_sec": te.get("timeout_sec", 0),
        "max_adverse_pct": te.get("max_adverse_pct", 0),
    }

    try:
        result = lfest_core.run_backtest(ts_data, px_data, json.dumps(config))
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"LFEST backtest failed: {e}"}), 400

    price_chart = {"candles": chart_candles}

    # Trades
    trades = []
    for t in result.get("trades", [])[:400]:
        trades.append({
            "id": t["trade_id"],
            "side": t.get("side", config["side"]),
            "entry": round(t["entry_price"], 2),
            "exit": round(t["exit_price"], 2),
            "entry_ts": t["entry_time"],
            "exit_ts": t["exit_time"],
            "entry_time": datetime.fromtimestamp(t["entry_time"], tz=timezone.utc).strftime("%m/%d %H:%M"),
            "exit_time": datetime.fromtimestamp(t["exit_time"], tz=timezone.utc).strftime("%m/%d %H:%M"),
            "pnl": round(t["realized_pnl"], 2),
            "pnl_pct": round(t["realized_pnl_pct"], 2),
            "reason": t["exit_reason"],
            "tp1": t["tp1_hit"], "tp2": t["tp2_hit"], "tp3": t["tp3_hit"],
            "signal": round(t.get("signal_price", 0), 2),
            "improvement": round(t.get("entry_improvement_pct", 0), 2),
        })

    # Equity curve
    eq = result.get("equity_curve", [])
    eq_step = max(1, len(eq) // 300)
    sampled_eq = eq[::eq_step]
    equity_chart = {
        "labels": [datetime.fromtimestamp(e[0], tz=timezone.utc).strftime("%m/%d %H:%M") for e in sampled_eq],
        "data": [round(e[1], 2) for e in sampled_eq],
    }

    m = result.get("metrics", {})
    elapsed = time.perf_counter() - t0

    resp = {
        "meta": {
            "candles": len(price_chart["candles"]),
            "ticks": result.get("tick_count", 0),
            "days": days,
            "timeframe": timeframe,
            "mode": "Mode 3 (LFEST-rs Exchange Sim)",
            "tick_source": tick_source,
            "ticks_per_sec": round(result.get("tick_count", 0) / max(result.get("execution_time_ms", 1) / 1000, 0.001)),
            "fetched_at": time.time(),
        },
        "metrics": m,
        "price_chart": price_chart,
        "equity_chart": equity_chart,
        "trades": trades,
        "pnl_dist": [round(t["realized_pnl_pct"], 2) for t in result.get("trades", [])],
        "elapsed_ms": round(elapsed * 1000, 1),
        "engine_ms": round(result.get("execution_time_ms", 0), 2),
        "mode": config["side"],
    }

    return jsonify(resp)


# ── HTML Template ─────────────────────────────────────────────────────────────
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Agentrade Backtest Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
<script src="https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"></script>
<style>
:root {
  --bg: #0f1117; --card: #1a1d2e; --border: #2d3148; --input-bg: #232640;
  --text: #e2e8f0; --dim: #94a3b8; --accent: #3b82f6;
  --green: #26a69a; --red: #ef5350; --yellow: #f59e0b; --purple: #8b5cf6;
}
* { margin:0; padding:0; box-sizing:border-box; }
body { background:var(--bg); color:var(--text); font-family:'SF Mono','Fira Code','Cascadia Code',monospace; font-size:13px; }
.layout { display:grid; grid-template-columns:280px 1fr; min-height:100vh; }

.sidebar { background:var(--card); border-right:1px solid var(--border); padding:20px; overflow-y:auto; }
.sidebar h1 { font-size:16px; color:var(--accent); margin-bottom:4px; }
.sidebar .sub { color:var(--dim); font-size:11px; margin-bottom:20px; }

.control-group { margin-bottom:16px; }
.control-group label { display:block; color:var(--dim); font-size:10px; text-transform:uppercase; letter-spacing:0.5px; margin-bottom:4px; }
.control-group select, .control-group input {
  width:100%; background:var(--input-bg); border:1px solid var(--border); color:var(--text);
  padding:6px 8px; border-radius:4px; font-family:inherit; font-size:12px;
}
.control-group input[type=range] { padding:0; }
.range-row { display:flex; align-items:center; gap:8px; }
.range-row input[type=range] { flex:1; }
.range-row .val { min-width:36px; text-align:right; color:var(--accent); font-size:12px; }
.control-row { display:grid; grid-template-columns:1fr 1fr; gap:8px; }
.section-title { color:var(--yellow); font-size:11px; text-transform:uppercase; letter-spacing:1px; margin:16px 0 8px; border-bottom:1px solid var(--border); padding-bottom:4px; }

.btn { width:100%; padding:10px; border:none; border-radius:6px; cursor:pointer; font-family:inherit; font-size:13px; font-weight:bold; margin-top:8px; transition: opacity 0.2s; }
.btn:hover { opacity:0.85; }
.btn-primary { background:var(--accent); color:#fff; }
.btn-green { background:var(--green); color:#fff; }
.btn-purple { background:var(--purple); color:#fff; }
.btn:disabled { opacity:0.4; cursor:not-allowed; }

.toggle { display:flex; align-items:center; gap:8px; margin:4px 0; }
.toggle input[type=checkbox] { width:16px; height:16px; accent-color:var(--accent); }
.status { color:var(--dim); font-size:11px; margin-top:8px; text-align:center; min-height:16px; }

.main { padding:20px; overflow-y:auto; }
.grid { display:grid; gap:12px; margin-bottom:12px; }
.g4 { grid-template-columns:repeat(4,1fr); }
.g2 { grid-template-columns:repeat(2,1fr); }
.g1 { grid-template-columns:1fr; }

.card { background:var(--card); border:1px solid var(--border); border-radius:8px; padding:14px; }
.card h3 { color:var(--accent); font-size:11px; text-transform:uppercase; letter-spacing:0.8px; margin-bottom:10px; }

.kpi .val { font-size:22px; font-weight:bold; }
.kpi .lbl { color:var(--dim); font-size:11px; margin-top:2px; }
.kpi .val.green { color:var(--green); }
.kpi .val.red { color:var(--red); }
.kpi .val.yellow { color:var(--yellow); }

.chart-box { position:relative; height:260px; }
#tvChartWrap { width:100%; height:520px; border-radius:6px; overflow:hidden; }

/* ── Simulation Controls ──────────────────────────────────── */
.sim-bar {
  display:flex; align-items:center; gap:10px; padding:10px 14px;
  background:#131722; border:1px solid var(--border); border-radius:8px; margin-bottom:8px;
}
.sim-btn {
  width:36px; height:36px; border-radius:50%; border:2px solid var(--border); background:transparent;
  color:var(--text); font-size:16px; cursor:pointer; display:flex; align-items:center; justify-content:center;
  transition: all 0.15s;
}
.sim-btn:hover { border-color:var(--accent); color:var(--accent); }
.sim-btn.active { background:var(--accent); border-color:var(--accent); color:#fff; }
.sim-btn.reset { font-size:14px; }

.sim-progress { flex:1; display:flex; align-items:center; gap:8px; }
.sim-progress input[type=range] {
  flex:1; height:6px; -webkit-appearance:none; appearance:none;
  background:var(--border); border-radius:3px; outline:none;
}
.sim-progress input[type=range]::-webkit-slider-thumb {
  -webkit-appearance:none; width:14px; height:14px; border-radius:50%;
  background:var(--accent); cursor:pointer; border:2px solid #131722;
}
.sim-time { color:var(--dim); font-size:11px; min-width:80px; text-align:center; }
.sim-candle-count { color:var(--dim); font-size:10px; min-width:60px; text-align:right; }

.speed-group { display:flex; align-items:center; gap:4px; }
.speed-btn {
  padding:3px 8px; border-radius:4px; border:1px solid var(--border); background:transparent;
  color:var(--dim); font-size:10px; cursor:pointer; font-family:inherit;
}
.speed-btn.active { background:var(--accent); border-color:var(--accent); color:#fff; }
.speed-btn:hover { border-color:var(--accent); }

/* ── Position Panel ───────────────────────────────────────── */
.pos-panel {
  display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-top:8px;
}
.pos-card {
  background:#131722; border:1px solid var(--border); border-radius:6px; padding:10px 12px;
  font-size:11px; min-height:70px;
}
.pos-card.active-long { border-color:var(--green); }
.pos-card.active-short { border-color:var(--red); }
.pos-card .pos-title { color:var(--dim); font-size:9px; text-transform:uppercase; letter-spacing:0.5px; margin-bottom:6px; }
.pos-card .pos-empty { color:var(--dim); font-style:italic; }
.pos-row { display:flex; justify-content:space-between; margin-bottom:2px; }
.pos-row .pos-label { color:var(--dim); }
.pos-row .pos-val { font-weight:bold; }

.order-log {
  background:#131722; border:1px solid var(--border); border-radius:6px; padding:10px 12px;
  font-size:10px; max-height:160px; overflow-y:auto; margin-top:8px;
}
.order-log .order-title { color:var(--dim); font-size:9px; text-transform:uppercase; letter-spacing:0.5px; margin-bottom:6px; }
.order-entry { padding:3px 0; border-bottom:1px solid rgba(45,49,72,0.4); display:flex; gap:8px; align-items:center; }
.order-entry:last-child { border-bottom:none; }
.order-entry .order-time { color:var(--dim); min-width:70px; }
.order-entry .order-action { font-weight:bold; }
.order-entry.buy .order-action { color:var(--green); }
.order-entry.sell .order-action { color:var(--red); }
.order-entry.tp .order-action { color:var(--yellow); }

.sim-stats {
  display:flex; gap:16px; font-size:11px; margin-top:8px; padding:6px 0;
}
.sim-stat { display:flex; gap:4px; }
.sim-stat .ss-label { color:var(--dim); }
.sim-stat .ss-val { font-weight:bold; }

table { width:100%; border-collapse:collapse; font-size:11px; }
th { color:var(--dim); text-align:right; padding:5px 6px; border-bottom:1px solid var(--border); font-weight:normal; text-transform:uppercase; font-size:9px; letter-spacing:0.3px; }
th:first-child { text-align:left; }
td { padding:5px 6px; text-align:right; border-bottom:1px solid rgba(45,49,72,0.5); }
td:first-child { text-align:left; }
.pos { color:var(--green); } .neg { color:var(--red); }
.best-row { background:rgba(38,166,154,0.06); }

.tab-bar { display:flex; gap:4px; margin-bottom:12px; }
.tab { padding:6px 14px; background:var(--card); border:1px solid var(--border); border-radius:6px; cursor:pointer; color:var(--dim); font-size:11px; }
.tab.active { background:var(--accent); color:#fff; border-color:var(--accent); }
.hidden { display:none; }

@media (max-width:1100px) {
  .layout { grid-template-columns:1fr; }
  .sidebar { border-right:none; border-bottom:1px solid var(--border); }
  .g4 { grid-template-columns:repeat(2,1fr); }
}
</style>
</head>
<body>
<div class="layout">

<!-- SIDEBAR -->
<div class="sidebar">
  <h1>AGENTRADE</h1>
  <div class="sub">Backtest Dashboard</div>

  <div class="section-title">Market</div>
  <div class="control-group"><label>Symbol</label>
    <select id="symbol">
      <option value="SOL/USDC:USDC" selected>SOL/USDC</option>
      <option value="BTC/USDC:USDC">BTC/USDC</option>
      <option value="ETH/USDC:USDC">ETH/USDC</option>
      <option value="DOGE/USDC:USDC">DOGE/USDC</option>
      <option value="WIF/USDC:USDC">WIF/USDC</option>
      <option value="HYPE/USDC:USDC">HYPE/USDC</option>
    </select>
  </div>
  <div class="control-row">
    <div class="control-group"><label>Side</label>
      <select id="side"><option value="both" selected>Both</option><option value="long">Long</option><option value="short">Short</option></select>
    </div>
    <div class="control-group"><label>Days</label>
      <select id="days"><option value="3">3</option><option value="7">7</option><option value="14">14</option><option value="30" selected>30</option></select>
    </div>
  </div>
  <div class="control-group"><label>Timeframe (candle resolution)</label>
    <select id="timeframe">
      <option value="1m">1m (max ~3.5 days)</option>
      <option value="5m" selected>5m (max ~17 days)</option>
      <option value="15m">15m (max ~52 days)</option>
      <option value="1h">1h (max ~208 days)</option>
      <option value="4h">4h (max ~833 days)</option>
    </select>
  </div>

  <div class="section-title">Exit Strategy (Trailing TP/SL)</div>
  <div class="control-group"><label>TP1 %</label><div class="range-row"><input type="range" id="tp1" min="0.5" max="10" step="0.5" value="3"><span class="val" id="tp1v">3.0</span></div></div>
  <div class="control-group"><label>TP2 %</label><div class="range-row"><input type="range" id="tp2" min="1" max="15" step="0.5" value="5"><span class="val" id="tp2v">5.0</span></div></div>
  <div class="control-group"><label>TP3 %</label><div class="range-row"><input type="range" id="tp3" min="2" max="20" step="0.5" value="8"><span class="val" id="tp3v">8.0</span></div></div>
  <div class="control-group"><label>Exit Trail %</label><div class="range-row"><input type="range" id="exit_trail" min="0.5" max="5" step="0.25" value="1.5"><span class="val" id="exit_trailv">1.5</span></div></div>

  <div class="section-title">Trailing Entry</div>
  <div class="toggle"><input type="checkbox" id="te_enabled"><label for="te_enabled" style="color:var(--text)">Enable Trailing Entry</label></div>
  <div id="te_controls">
    <div class="control-group"><label>Entry Trail %</label><div class="range-row"><input type="range" id="te_trail" min="0.5" max="5" step="0.25" value="2"><span class="val" id="te_trailv">2.0</span></div></div>
    <div class="toggle"><input type="checkbox" id="te_atr"><label for="te_atr" style="color:var(--text)">ATR-Based</label></div>
    <div class="control-group"><label>ATR Multiplier</label><div class="range-row"><input type="range" id="te_atr_mult" min="0.5" max="3" step="0.25" value="1.5"><span class="val" id="te_atr_multv">1.5</span></div></div>
  </div>

  <div class="section-title">Execution</div>
  <div class="control-row">
    <div class="control-group"><label>Leverage</label><input type="number" id="leverage" value="5" min="1" max="50"></div>
    <div class="control-group"><label>Capital $</label><input type="number" id="capital" value="1000" min="100"></div>
  </div>
  <div class="control-row">
    <div class="control-group"><label>Size</label><input type="number" id="size" value="1" min="0.01" step="0.1"></div>
    <div class="control-group"><label>Fee %</label><input type="number" id="fee" value="0.035" min="0" step="0.005"></div>
  </div>

  <button class="btn btn-primary" id="btnRun" onclick="runBacktest()">RUN BACKTEST (Mode 1)</button>
  <button class="btn btn-green" id="btnSim" onclick="runSim()" style="background:#f59e0b">RUN SIMULATION (Mode 2)</button>
  <button class="btn btn-green" id="btnLfest" onclick="runLfest()" style="background:#ef4444">RUN LFEST (Mode 3)</button>
  <button class="btn btn-green" id="btnCompare" onclick="runCompare()">COMPARE ENTRY MODES</button>
  <button class="btn btn-purple" id="btnSweep" onclick="runSweep()">PARAMETER SWEEP</button>
  <div class="status" id="status"></div>
</div>

<!-- MAIN -->
<div class="main">
  <div class="grid g4">
    <div class="card"><div class="kpi"><div class="val" id="k_pnl">--</div><div class="lbl">Total PnL</div></div></div>
    <div class="card"><div class="kpi"><div class="val" id="k_wr">--</div><div class="lbl">Win Rate</div></div></div>
    <div class="card"><div class="kpi"><div class="val" id="k_pf">--</div><div class="lbl">Profit Factor</div></div></div>
    <div class="card"><div class="kpi"><div class="val" id="k_dd">--</div><div class="lbl">Max Drawdown</div></div></div>
  </div>
  <div class="grid g4">
    <div class="card"><div class="kpi"><div class="val" id="k_trades">--</div><div class="lbl">Trades</div></div></div>
    <div class="card"><div class="kpi"><div class="val" id="k_sharpe">--</div><div class="lbl">Sharpe Ratio</div></div></div>
    <div class="card"><div class="kpi"><div class="val" id="k_imp">--</div><div class="lbl">Avg Entry Improvement</div></div></div>
    <div class="card"><div class="kpi"><div class="val" id="k_exp">--</div><div class="lbl">Expectancy / Trade</div></div></div>
  </div>

  <!-- Simulation Controls -->
  <div class="grid g1">
    <div class="sim-bar" id="simBar" style="display:none">
      <button class="sim-btn" id="simPlayBtn" onclick="simToggle()" title="Play/Pause">&#9654;</button>
      <button class="sim-btn reset" id="simResetBtn" onclick="simReset()" title="Reset">&#8634;</button>
      <div class="sim-progress">
        <input type="range" id="simSlider" min="0" max="100" value="0" oninput="simSeek(this.value)">
      </div>
      <div class="sim-time" id="simTime">--:--</div>
      <div class="sim-candle-count" id="simCount">0/0</div>
      <div class="speed-group">
        <button class="speed-btn" onclick="simSetSpeed(1,this)">1x</button>
        <button class="speed-btn" onclick="simSetSpeed(2,this)">2x</button>
        <button class="speed-btn active" onclick="simSetSpeed(5,this)">5x</button>
        <button class="speed-btn" onclick="simSetSpeed(10,this)">10x</button>
        <button class="speed-btn" onclick="simSetSpeed(25,this)">25x</button>
        <button class="speed-btn" onclick="simSetSpeed(50,this)">50x</button>
      </div>
    </div>
  </div>

  <!-- TradingView Chart -->
  <div class="grid g1">
    <div class="card" style="padding-bottom:8px">
      <h3>Price Chart with Trade Positions</h3>
      <div id="tvChartWrap"></div>
      <!-- Position Panel -->
      <div class="pos-panel" id="posPanel" style="display:none">
        <div class="pos-card" id="posLong">
          <div class="pos-title">Long Position</div>
          <div class="pos-empty" id="posLongEmpty">No position</div>
          <div id="posLongData" style="display:none"></div>
        </div>
        <div class="pos-card" id="posShort">
          <div class="pos-title">Short Position</div>
          <div class="pos-empty" id="posShortEmpty">No position</div>
          <div id="posShortData" style="display:none"></div>
        </div>
      </div>
      <!-- Simulation Stats -->
      <div class="sim-stats" id="simStats" style="display:none">
        <div class="sim-stat"><span class="ss-label">Realized PnL:</span><span class="ss-val" id="ssRpnl">$0.00</span></div>
        <div class="sim-stat"><span class="ss-label">Trades:</span><span class="ss-val" id="ssTrades">0</span></div>
        <div class="sim-stat"><span class="ss-label">Win/Loss:</span><span class="ss-val" id="ssWL">0/0</span></div>
        <div class="sim-stat"><span class="ss-label">Current Price:</span><span class="ss-val" id="ssPrice">--</span></div>
      </div>
      <!-- Order Log -->
      <div class="order-log" id="orderLog" style="display:none">
        <div class="order-title">Order Log</div>
        <div id="orderLogEntries"></div>
      </div>
    </div>
  </div>

  <div class="grid g2">
    <div class="card"><h3>Equity Curve</h3><div class="chart-box"><canvas id="chartEquity"></canvas></div></div>
    <div class="card"><h3>PnL Distribution</h3><div class="chart-box"><canvas id="chartPnl"></canvas></div></div>
  </div>
  <div class="grid g1">
    <div class="card"><h3>Entry Mode Comparison</h3><div class="chart-box"><canvas id="chartCompare"></canvas></div></div>
  </div>

  <div class="tab-bar">
    <div class="tab active" onclick="showTab('trades',this)">Trade Log</div>
    <div class="tab" onclick="showTab('comparison',this)">Entry Comparison</div>
    <div class="tab" onclick="showTab('sweep',this)">Sweep Results</div>
  </div>
  <div id="tab-trades" class="card"></div>
  <div id="tab-comparison" class="card hidden"></div>
  <div id="tab-sweep" class="card hidden"></div>
</div>
</div>

<script>
// ── Globals ──────────────────────────────────────────────────
document.querySelectorAll('input[type=range]').forEach(el => {
  const vEl = document.getElementById(el.id + 'v');
  if (vEl) { el.oninput = () => vEl.textContent = parseFloat(el.value).toFixed(el.step < 1 ? (el.step < 0.1 ? 2 : 1) : 0); }
});

function showTab(name, el) {
  document.querySelectorAll('[id^=tab-]').forEach(t => t.classList.add('hidden'));
  document.getElementById('tab-' + name).classList.remove('hidden');
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  if (el) el.classList.add('active');
}

let charts = {};
const COLORS = ['#3b82f6','#f59e0b','#26a69a','#ef5350','#8b5cf6','#ec4899'];
Chart.defaults.color = '#94a3b8';
Chart.defaults.borderColor = '#2d3148';
Chart.defaults.font.family = "'SF Mono',monospace";
Chart.defaults.font.size = 10;

function makeChart(id, type, data, opts) {
  if (charts[id]) charts[id].destroy();
  charts[id] = new Chart(document.getElementById(id), { type, data, options: opts });
}

// ── TradingView Chart ────────────────────────────────────────
let tvChart = null, tvCandleSeries = null, tvVolumeSeries = null;
let tvPriceLines = [];

function initTVChart() {
  const container = document.getElementById('tvChartWrap');
  container.innerHTML = '';
  tvPriceLines = [];

  tvChart = LightweightCharts.createChart(container, {
    width: container.clientWidth, height: 520,
    layout: { background: { type: 'solid', color: '#131722' }, textColor: '#94a3b8', fontFamily: "'SF Mono','Fira Code',monospace", fontSize: 11 },
    grid: { vertLines: { color: 'rgba(42,46,57,0.8)' }, horzLines: { color: 'rgba(42,46,57,0.8)' } },
    crosshair: {
      mode: LightweightCharts.CrosshairMode.Normal,
      vertLine: { color: 'rgba(59,130,246,0.5)', width: 1, style: 2, labelBackgroundColor: '#3b82f6' },
      horzLine: { color: 'rgba(59,130,246,0.5)', width: 1, style: 2, labelBackgroundColor: '#3b82f6' },
    },
    rightPriceScale: { borderColor: '#2a2e39', scaleMargins: { top: 0.02, bottom: 0.18 } },
    timeScale: { borderColor: '#2a2e39', timeVisible: true, secondsVisible: false, rightOffset: 5, barSpacing: 4, minBarSpacing: 1 },
    handleScroll: { vertTouchDrag: false }, handleScale: { axisPressedMouseMove: true },
  });

  tvCandleSeries = tvChart.addCandlestickSeries({
    upColor: '#26a69a', downColor: '#ef5350', borderDownColor: '#ef5350', borderUpColor: '#26a69a', wickDownColor: '#ef5350', wickUpColor: '#26a69a',
  });

  tvVolumeSeries = tvChart.addHistogramSeries({ color: '#26a69a', priceFormat: { type: 'volume' }, priceScaleId: '' });
  tvVolumeSeries.priceScale().applyOptions({ scaleMargins: { top: 0.85, bottom: 0 } });

  new ResizeObserver(entries => { for (const e of entries) tvChart.applyOptions({ width: e.contentRect.width }); }).observe(container);
}

function clearPriceLines() {
  tvPriceLines.forEach(pl => { try { tvCandleSeries.removePriceLine(pl); } catch(e) {} });
  tvPriceLines = [];
}

function addPriceLine(price, color, title, style) {
  const pl = tvCandleSeries.createPriceLine({
    price: price, color: color, lineWidth: 1,
    lineStyle: style || LightweightCharts.LineStyle.Dashed,
    axisLabelVisible: true, title: title,
  });
  tvPriceLines.push(pl);
}

function renderTVChart(candles, trades) {
  if (!tvChart) initTVChart();
  tvCandleSeries.setData(candles);
  tvVolumeSeries.setData(candles.map(c => ({ time: c.time, value: c.volume || 0, color: c.close >= c.open ? 'rgba(38,166,154,0.25)' : 'rgba(239,83,80,0.25)' })));

  const markers = [];
  trades.forEach(t => {
    const isLong = t.side === 'long', isWin = t.pnl >= 0;
    markers.push({ time: Math.round(t.entry_ts), position: isLong ? 'belowBar' : 'aboveBar', color: isLong ? '#26a69a' : '#ef5350', shape: isLong ? 'arrowUp' : 'arrowDown', text: (isLong?'L':'S') + ' $' + t.entry });
    markers.push({ time: Math.round(t.exit_ts), position: isWin ? 'aboveBar' : 'belowBar', color: isWin ? '#66bb6a' : '#ff5252', shape: 'circle', text: t.reason + ' ' + (t.pnl>=0?'+':'') + '$' + t.pnl.toFixed(1) });
  });
  markers.sort((a,b) => a.time - b.time);
  for (let i = 1; i < markers.length; i++) { if (markers[i].time <= markers[i-1].time) markers[i].time = markers[i-1].time + 1; }
  tvCandleSeries.setMarkers(markers);
  tvChart.timeScale().fitContent();
}

// ══════════════════════════════════════════════════════════════
// ══  SIMULATION REPLAY ENGINE  ═══════════════════════════════
// ══════════════════════════════════════════════════════════════
let sim = {
  playing: false,
  speed: 5,           // candles per tick
  interval: null,
  idx: 0,             // current candle index
  allCandles: [],
  allTrades: [],
  config: {},
  // State
  activeMarkers: [],
  openPositions: {},   // {long: {...}, short: {...}}
  closedTrades: [],
  nextEntryIdx: {},    // {long: 0, short: 0} — index into trades array for next entry
  nextExitIdx: {},
  realizedPnl: 0,
  wins: 0, losses: 0,
  orderLogEntries: [],
};

function simInit(candles, trades, config) {
  simStop();
  sim.allCandles = candles;
  sim.allTrades = trades;
  sim.config = config || {};
  sim.idx = 0;
  sim.activeMarkers = [];
  sim.openPositions = {};
  sim.closedTrades = [];
  sim.realizedPnl = 0;
  sim.wins = 0;
  sim.losses = 0;
  sim.orderLogEntries = [];

  // Pre-sort trades by entry and exit times
  sim.entryEvents = [];
  sim.exitEvents = [];
  trades.forEach(function(t, i) {
    sim.entryEvents.push({ time: Math.round(t.entry_ts), trade: t, idx: i });
    sim.exitEvents.push({ time: Math.round(t.exit_ts), trade: t, idx: i });
  });
  sim.entryEvents.sort(function(a,b) { return a.time - b.time; });
  sim.exitEvents.sort(function(a,b) { return a.time - b.time; });
  sim.nextEntry = 0;
  sim.nextExit = 0;

  // Show sim UI
  document.getElementById('simBar').style.display = 'flex';
  document.getElementById('posPanel').style.display = 'grid';
  document.getElementById('simStats').style.display = 'flex';
  document.getElementById('orderLog').style.display = 'block';

  var slider = document.getElementById('simSlider');
  slider.max = candles.length - 1;
  slider.value = 0;

  // Init chart with all candles (user can replay via play button)
  if (!tvChart) initTVChart();
  tvCandleSeries.setData(candles);
  tvVolumeSeries.setData(candles.map(c => ({ time: c.time, value: c.volume || 0, color: c.close >= c.open ? 'rgba(38,166,154,0.25)' : 'rgba(239,83,80,0.25)' })));
  tvCandleSeries.setMarkers([]);
  clearPriceLines();

  simUpdateUI();
}

function simToggle() {
  if (sim.playing) { simStop(); } else { simPlay(); }
}

function simPlay() {
  if (sim.idx >= sim.allCandles.length - 1) simReset();
  sim.playing = true;
  document.getElementById('simPlayBtn').innerHTML = '&#9646;&#9646;';
  document.getElementById('simPlayBtn').classList.add('active');

  var baseDelay = 100;  // ms per tick
  sim.interval = setInterval(function() {
    for (var i = 0; i < sim.speed && sim.idx < sim.allCandles.length; i++) {
      simStep();
    }
    simUpdateUI();
    if (sim.idx >= sim.allCandles.length - 1) simStop();
  }, baseDelay);
}

function simStop() {
  sim.playing = false;
  if (sim.interval) { clearInterval(sim.interval); sim.interval = null; }
  var btn = document.getElementById('simPlayBtn');
  if (btn) { btn.innerHTML = '&#9654;'; btn.classList.remove('active'); }
}

function simReset() {
  simStop();
  simInit(sim.allCandles, sim.allTrades, sim.config);
}

function simSeek(val) {
  simStop();
  var targetIdx = parseInt(val);
  // Rebuild from scratch up to targetIdx
  sim.idx = 0;
  sim.activeMarkers = [];
  sim.openPositions = {};
  sim.closedTrades = [];
  sim.realizedPnl = 0;
  sim.wins = 0;
  sim.losses = 0;
  sim.orderLogEntries = [];
  sim.nextEntry = 0;
  sim.nextExit = 0;

  // Fast-forward processing all events up to targetIdx
  var candlesUpTo = sim.allCandles.slice(0, targetIdx + 1);
  if (candlesUpTo.length > 0) {
    var lastTime = candlesUpTo[candlesUpTo.length - 1].time;
    // Process entries
    while (sim.nextEntry < sim.entryEvents.length && sim.entryEvents[sim.nextEntry].time <= lastTime) {
      var ev = sim.entryEvents[sim.nextEntry];
      simProcessEntry(ev.trade, ev.time);
      sim.nextEntry++;
    }
    // Process exits
    while (sim.nextExit < sim.exitEvents.length && sim.exitEvents[sim.nextExit].time <= lastTime) {
      var ev2 = sim.exitEvents[sim.nextExit];
      simProcessExit(ev2.trade, ev2.time);
      sim.nextExit++;
    }
  }

  sim.idx = targetIdx;

  // Render chart up to this point
  tvCandleSeries.setData(candlesUpTo);
  tvVolumeSeries.setData(candlesUpTo.map(function(c) { return { time: c.time, value: c.volume || 0, color: c.close >= c.open ? 'rgba(38,166,154,0.25)' : 'rgba(239,83,80,0.25)' }; }));

  // Rebuild markers
  var mkrs = [];
  sim.activeMarkers.forEach(function(m) { mkrs.push(m); });
  mkrs.sort(function(a,b) { return a.time - b.time; });
  for (var j = 1; j < mkrs.length; j++) { if (mkrs[j].time <= mkrs[j-1].time) mkrs[j].time = mkrs[j-1].time + 1; }
  tvCandleSeries.setMarkers(mkrs);

  simUpdatePriceLines();
  simUpdateUI();
  tvChart.timeScale().scrollToPosition(5, false);
}

function simSetSpeed(spd, btn) {
  sim.speed = spd;
  document.querySelectorAll('.speed-btn').forEach(function(b) { b.classList.remove('active'); });
  if (btn) btn.classList.add('active');
  // If playing, restart with new speed
  if (sim.playing) { simStop(); simPlay(); }
}

function simStep() {
  if (sim.idx >= sim.allCandles.length) return;

  var candle = sim.allCandles[sim.idx];
  var ct = candle.time;

  // Add candle to chart
  tvCandleSeries.update(candle);
  tvVolumeSeries.update({ time: ct, value: candle.volume || 0, color: candle.close >= candle.open ? 'rgba(38,166,154,0.25)' : 'rgba(239,83,80,0.25)' });

  // Check for entries at this candle
  while (sim.nextEntry < sim.entryEvents.length && sim.entryEvents[sim.nextEntry].time <= ct) {
    var ev = sim.entryEvents[sim.nextEntry];
    simProcessEntry(ev.trade, ct);
    sim.nextEntry++;
  }

  // Check for exits at this candle
  while (sim.nextExit < sim.exitEvents.length && sim.exitEvents[sim.nextExit].time <= ct) {
    var ev2 = sim.exitEvents[sim.nextExit];
    simProcessExit(ev2.trade, ct);
    sim.nextExit++;
  }

  // Update open position PnL with current price
  var price = candle.close;
  Object.keys(sim.openPositions).forEach(function(key) {
    var pos = sim.openPositions[key];
    if (pos) {
      if (pos.side === 'long') { pos.unrealizedPnl = (price - pos.entry) * sim.config.leverage; pos.unrealizedPct = (price - pos.entry) / pos.entry * 100; }
      else { pos.unrealizedPnl = (pos.entry - price) * sim.config.leverage; pos.unrealizedPct = (pos.entry - price) / pos.entry * 100; }
    }
  });

  sim.idx++;
}

function simProcessEntry(trade, time) {
  var isLong = trade.side === 'long';
  var key = trade.side;

  sim.openPositions[key] = {
    id: trade.id, side: trade.side, entry: trade.entry, entryTime: time,
    tp1: trade.entry * (1 + (isLong ? 1 : -1) * (sim.config.tp1_pct || 3) / 100),
    tp2: trade.entry * (1 + (isLong ? 1 : -1) * (sim.config.tp2_pct || 5) / 100),
    tp3: trade.entry * (1 + (isLong ? 1 : -1) * (sim.config.tp3_pct || 8) / 100),
    sl: 0,
    unrealizedPnl: 0, unrealizedPct: 0,
  };

  // Add entry marker
  sim.activeMarkers.push({
    time: Math.round(trade.entry_ts),
    position: isLong ? 'belowBar' : 'aboveBar',
    color: isLong ? '#26a69a' : '#ef5350',
    shape: isLong ? 'arrowUp' : 'arrowDown',
    text: (isLong ? 'LONG' : 'SHORT') + ' $' + trade.entry,
  });

  // Refresh markers on chart
  simRefreshMarkers();

  // Order log
  simAddOrder(time, isLong ? 'buy' : 'sell', (isLong ? 'OPEN LONG' : 'OPEN SHORT') + ' #' + trade.id + ' @ $' + trade.entry);
}

function simProcessExit(trade, time) {
  var key = trade.side;
  var isWin = trade.pnl >= 0;

  // Remove from open positions
  delete sim.openPositions[key];

  // Add exit marker
  sim.activeMarkers.push({
    time: Math.round(trade.exit_ts),
    position: isWin ? 'aboveBar' : 'belowBar',
    color: isWin ? '#66bb6a' : '#ff5252',
    shape: 'circle',
    text: trade.reason + ' ' + (trade.pnl >= 0 ? '+' : '') + '$' + trade.pnl.toFixed(1),
  });

  simRefreshMarkers();

  sim.closedTrades.push(trade);
  sim.realizedPnl += trade.pnl;
  if (isWin) sim.wins++; else sim.losses++;

  var action = isWin ? 'tp' : 'sell';
  simAddOrder(time, action, 'CLOSE ' + trade.side.toUpperCase() + ' #' + trade.id + ' @ $' + trade.exit + ' | ' + trade.reason + ' ' + (trade.pnl>=0?'+':'') + '$' + trade.pnl.toFixed(2));

  simUpdatePriceLines();
}

function simRefreshMarkers() {
  var mkrs = sim.activeMarkers.slice();
  mkrs.sort(function(a,b) { return a.time - b.time; });
  for (var j = 1; j < mkrs.length; j++) { if (mkrs[j].time <= mkrs[j-1].time) mkrs[j].time = mkrs[j-1].time + 1; }
  tvCandleSeries.setMarkers(mkrs);
}

function simUpdatePriceLines() {
  clearPriceLines();
  Object.keys(sim.openPositions).forEach(function(key) {
    var pos = sim.openPositions[key];
    if (!pos) return;
    var isLong = pos.side === 'long';
    addPriceLine(pos.entry, '#3b82f6', 'Entry ' + pos.side.toUpperCase(), LightweightCharts.LineStyle.Solid);
    addPriceLine(pos.tp1, '#26a69a', 'TP1', LightweightCharts.LineStyle.Dashed);
    addPriceLine(pos.tp2, '#26a69a', 'TP2', LightweightCharts.LineStyle.Dashed);
    addPriceLine(pos.tp3, '#f59e0b', 'TP3', LightweightCharts.LineStyle.Dashed);
    if (pos.sl > 0) addPriceLine(pos.sl, '#ef5350', 'SL', LightweightCharts.LineStyle.Dashed);
  });
}

function simAddOrder(time, cls, text) {
  var d = new Date(time * 1000);
  var ts = (d.getUTCMonth()+1) + '/' + d.getUTCDate() + ' ' + String(d.getUTCHours()).padStart(2,'0') + ':' + String(d.getUTCMinutes()).padStart(2,'0');
  sim.orderLogEntries.unshift({ time: ts, cls: cls, text: text });
  if (sim.orderLogEntries.length > 50) sim.orderLogEntries.pop();
}

function simUpdateUI() {
  // Slider
  var slider = document.getElementById('simSlider');
  slider.value = sim.idx;

  // Time display
  var currentCandle = sim.allCandles[Math.min(sim.idx, sim.allCandles.length - 1)];
  if (currentCandle) {
    var d = new Date(currentCandle.time * 1000);
    document.getElementById('simTime').textContent = (d.getUTCMonth()+1) + '/' + d.getUTCDate() + ' ' + String(d.getUTCHours()).padStart(2,'0') + ':' + String(d.getUTCMinutes()).padStart(2,'0');
    document.getElementById('ssPrice').textContent = '$' + currentCandle.close;
  }
  document.getElementById('simCount').textContent = sim.idx + '/' + sim.allCandles.length;

  // Stats
  var rpnlEl = document.getElementById('ssRpnl');
  rpnlEl.textContent = '$' + (sim.realizedPnl >= 0 ? '+' : '') + sim.realizedPnl.toFixed(2);
  rpnlEl.style.color = sim.realizedPnl >= 0 ? '#26a69a' : '#ef5350';
  document.getElementById('ssTrades').textContent = sim.closedTrades.length;
  document.getElementById('ssWL').textContent = sim.wins + '/' + sim.losses;

  // Position panels
  updatePosCard('long');
  updatePosCard('short');

  // Order log
  var logHtml = '';
  sim.orderLogEntries.forEach(function(o) {
    logHtml += '<div class="order-entry ' + o.cls + '"><span class="order-time">' + o.time + '</span><span class="order-action">' + o.text + '</span></div>';
  });
  document.getElementById('orderLogEntries').innerHTML = logHtml;
}

function updatePosCard(side) {
  var pos = sim.openPositions[side];
  var card = document.getElementById('pos' + side.charAt(0).toUpperCase() + side.slice(1));
  var empty = document.getElementById('pos' + side.charAt(0).toUpperCase() + side.slice(1) + 'Empty');
  var data = document.getElementById('pos' + side.charAt(0).toUpperCase() + side.slice(1) + 'Data');

  if (!pos) {
    card.className = 'pos-card';
    empty.style.display = 'block';
    data.style.display = 'none';
    data.innerHTML = '';
    return;
  }

  card.className = 'pos-card active-' + side;
  empty.style.display = 'none';
  data.style.display = 'block';

  var pnlColor = pos.unrealizedPnl >= 0 ? '#26a69a' : '#ef5350';
  data.innerHTML =
    '<div class="pos-row"><span class="pos-label">Trade #' + pos.id + '</span><span class="pos-val" style="color:' + (side==='long'?'#26a69a':'#ef5350') + '">' + side.toUpperCase() + '</span></div>' +
    '<div class="pos-row"><span class="pos-label">Entry</span><span class="pos-val">$' + pos.entry + '</span></div>' +
    '<div class="pos-row"><span class="pos-label">Unrealized</span><span class="pos-val" style="color:' + pnlColor + '">' + (pos.unrealizedPnl>=0?'+':'') + pos.unrealizedPnl.toFixed(2) + ' (' + (pos.unrealizedPct>=0?'+':'') + pos.unrealizedPct.toFixed(2) + '%)</span></div>' +
    '<div class="pos-row"><span class="pos-label">TP1</span><span class="pos-val" style="color:#26a69a">$' + pos.tp1.toFixed(2) + '</span></div>' +
    '<div class="pos-row"><span class="pos-label">TP2</span><span class="pos-val" style="color:#26a69a">$' + pos.tp2.toFixed(2) + '</span></div>' +
    '<div class="pos-row"><span class="pos-label">TP3</span><span class="pos-val" style="color:#f59e0b">$' + pos.tp3.toFixed(2) + '</span></div>';
}

// ── Params ───────────────────────────────────────────────────
function getParams() {
  return {
    symbol: document.getElementById('symbol').value,
    side: document.getElementById('side').value,
    days: parseInt(document.getElementById('days').value),
    timeframe: document.getElementById('timeframe').value,
    tp1: parseFloat(document.getElementById('tp1').value),
    tp2: parseFloat(document.getElementById('tp2').value),
    tp3: parseFloat(document.getElementById('tp3').value),
    exit_trail: parseFloat(document.getElementById('exit_trail').value),
    leverage: parseFloat(document.getElementById('leverage').value),
    capital: parseFloat(document.getElementById('capital').value),
    size: parseFloat(document.getElementById('size').value),
    fee: parseFloat(document.getElementById('fee').value),
    trailing_entry: {
      enabled: document.getElementById('te_enabled').checked,
      trail_pct: parseFloat(document.getElementById('te_trail').value),
      atr_enabled: document.getElementById('te_atr').checked,
      atr_period: 14,
      atr_multiplier: parseFloat(document.getElementById('te_atr_mult').value),
    }
  };
}

function setStatus(msg) { document.getElementById('status').textContent = msg; }
function setLoading(btn, loading) { document.getElementById(btn).disabled = loading; document.getElementById(btn).style.opacity = loading ? '0.5' : '1'; }

// ── Run Backtest ─────────────────────────────────────────────
var lastBacktestData = null;

async function runBacktest() {
  setLoading('btnRun', true);
  setStatus('Running backtest...');
  try {
    var resp = await fetch('/api/backtest', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(getParams()) });
    var d = await resp.json();
    if (d.error) { setStatus('Error: ' + d.error); return; }

    lastBacktestData = d;
    var m = d.metrics;
    setKPI('k_pnl', '$' + (m.total_pnl >= 0 ? '+' : '') + m.total_pnl.toFixed(2), m.total_pnl >= 0 ? 'green' : 'red', m.total_pnl_pct.toFixed(2) + '% return');
    setKPI('k_wr', m.win_rate.toFixed(1) + '%', m.win_rate >= 50 ? 'green' : 'red', m.total_trades + ' trades (' + m.winners + 'W/' + m.losers + 'L)');
    var pfStr = m.profit_factor < 1000 ? m.profit_factor.toFixed(2) : 'INF';
    setKPI('k_pf', pfStr, m.profit_factor >= 1.5 ? 'green' : m.profit_factor >= 1 ? 'yellow' : 'red', 'Sharpe ' + m.sharpe.toFixed(2) + ' | Sortino ' + m.sortino.toFixed(2));
    setKPI('k_dd', m.max_drawdown_pct.toFixed(2) + '%', m.max_drawdown_pct < 5 ? 'green' : m.max_drawdown_pct < 15 ? 'yellow' : 'red', 'TP1 ' + m.tp1_hit_rate.toFixed(0) + '% | TP2 ' + m.tp2_hit_rate.toFixed(0) + '% | TP3 ' + m.tp3_hit_rate.toFixed(0) + '%');
    setKPI('k_trades', m.total_trades, '', 'Fees $' + m.total_fees_paid.toFixed(2) + ' | Fund $' + m.total_funding_paid.toFixed(2));
    setKPI('k_sharpe', m.sharpe.toFixed(2), m.sharpe > 0 ? 'green' : 'red', 'Sortino: ' + m.sortino.toFixed(2));
    var imp = m.avg_entry_improvement_pct || 0;
    setKPI('k_imp', (imp >= 0 ? '+' : '') + imp.toFixed(2) + '%', imp > 0 ? 'green' : imp < 0 ? 'red' : '', 'Signals expired: ' + (m.signals_expired || 0));
    setKPI('k_exp', '$' + (m.expectancy >= 0 ? '+' : '') + m.expectancy.toFixed(2), m.expectancy >= 0 ? 'green' : 'red', 'per trade');

    // Render full chart first
    renderTVChart(d.price_chart.candles, d.trades);

    // Initialize simulation (ready to play)
    simInit(d.price_chart.candles, d.trades, d.config || {});

    // Equity
    var eqDs = [{ label: 'Equity (Long)', data: d.equity_chart.data, borderColor: '#26a69a', backgroundColor: 'rgba(38,166,154,0.08)', fill: true, pointRadius: 0, borderWidth: 1.5, tension: 0.1 }];
    if (d.equity_short) { eqDs[0].fill = false; eqDs.push({ label: 'Equity (Short)', data: d.equity_short, borderColor: '#ef5350', fill: false, pointRadius: 0, borderWidth: 1.5, tension: 0.1 }); }
    makeChart('chartEquity', 'line', { labels: d.equity_chart.labels, datasets: eqDs }, {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: !!d.equity_short, labels: { boxWidth: 10, padding: 6 } } },
      scales: { x: { ticks: { maxTicksLimit: 6, maxRotation: 0 } }, y: { ticks: { callback: function(v) { return '$' + v; } } } }
    });

    // PnL dist
    var bins = {};
    d.pnl_dist.forEach(function(p) { var b = Math.round(p); bins[b] = (bins[b]||0)+1; });
    var sorted = Object.keys(bins).map(Number).sort(function(a,b) { return a-b; });
    makeChart('chartPnl', 'bar', {
      labels: sorted.map(function(b) { return b + '%'; }),
      datasets: [{ data: sorted.map(function(b) { return bins[b]; }), backgroundColor: sorted.map(function(b) { return b >= 0 ? 'rgba(38,166,154,0.6)' : 'rgba(239,83,80,0.6)'; }), borderWidth: 0 }]
    }, { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { y: { title: { display: true, text: 'Count' } } } });

    // Trade log table
    var html = '<h3>Trade Log</h3><table><thead><tr><th>#</th><th>Side</th><th>Entry</th><th>Exit</th><th>PnL $</th><th>PnL %</th><th>Reason</th><th>TP</th></tr></thead><tbody>';
    d.trades.forEach(function(t) {
      var cls = t.pnl >= 0 ? 'pos' : 'neg';
      var sc = t.side === 'long' ? 'pos' : 'neg';
      var tp = (t.tp1?'1':'') + (t.tp2?' 2':'') + (t.tp3?' 3':'') || '-';
      html += '<tr><td>' + t.id + '</td><td class="' + sc + '">' + t.side.toUpperCase() + '</td><td>$' + t.entry + '</td><td>$' + t.exit + '</td><td class="' + cls + '">$' + (t.pnl>0?'+':'') + t.pnl.toFixed(2) + '</td><td class="' + cls + '">' + (t.pnl_pct>0?'+':'') + t.pnl_pct.toFixed(2) + '%</td><td>' + t.reason + '</td><td>' + tp + '</td></tr>';
    });
    html += '</tbody></table>';
    document.getElementById('tab-trades').innerHTML = html;

    setStatus('Done in ' + d.elapsed_ms + 'ms  |  Press PLAY to simulate');
  } catch(e) { setStatus('Error: ' + e.message); }
  finally { setLoading('btnRun', false); }
}

// ── Compare ──────────────────────────────────────────────────
async function runCompare() {
  setLoading('btnCompare', true); setStatus('Comparing...');
  try {
    var resp = await fetch('/api/compare', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(getParams()) });
    var d = await resp.json();
    if (d.error) { setStatus('Error: ' + d.error); return; }
    var labels = d.equity_curves[0]?.data.map(function(_,i) { return i; }) || [];
    makeChart('chartCompare', 'line', { labels: labels, datasets: d.equity_curves.map(function(ec, i) { return { label: ec.label, data: ec.data, borderColor: COLORS[i%COLORS.length], pointRadius: 0, borderWidth: 1.5, tension: 0.1, fill: false }; }) },
      { responsive: true, maintainAspectRatio: false, plugins: { legend: { position: 'top', labels: { boxWidth: 10, padding: 6 } } }, scales: { x: { display: false }, y: { ticks: { callback: function(v) { return '$'+v; } } } } });
    var html = '<h3>Entry Mode Comparison</h3><table><thead><tr><th>Mode</th><th>Trades</th><th>WR</th><th>PnL $</th><th>PF</th><th>Sharpe</th><th>DD</th></tr></thead><tbody>';
    d.comparison.forEach(function(c) { var cls = c.pnl >= 0 ? 'pos' : 'neg'; html += '<tr><td>' + c.label + '</td><td>' + c.trades + '</td><td>' + c.wr + '%</td><td class="' + cls + '">$' + (c.pnl>0?'+':'') + c.pnl.toFixed(2) + '</td><td>' + (c.pf<999?c.pf.toFixed(2):'INF') + '</td><td>' + c.sharpe + '</td><td>' + c.dd + '%</td></tr>'; });
    html += '</tbody></table>';
    document.getElementById('tab-comparison').innerHTML = html;
    showTab('comparison', document.querySelectorAll('.tab')[1]);
    setStatus('Compared 6 modes in ' + d.elapsed_ms + 'ms');
  } catch(e) { setStatus('Error: ' + e.message); }
  finally { setLoading('btnCompare', false); }
}

// ── Sweep ────────────────────────────────────────────────────
async function runSweep() {
  setLoading('btnSweep', true); setStatus('Sweeping...');
  try {
    var resp = await fetch('/api/sweep', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(getParams()) });
    var d = await resp.json();
    if (d.error) { setStatus('Error: ' + d.error); return; }
    var html = '<h3>Sweep Top 30/' + d.total_configs + '</h3><table><thead><tr><th>TP1</th><th>TP2</th><th>TP3</th><th>Trail</th><th>PnL</th><th>WR</th><th>PF</th><th>Sharpe</th><th>DD</th></tr></thead><tbody>';
    d.sweep.forEach(function(s,i) { var cls = s.pnl >= 0 ? 'pos' : 'neg'; html += '<tr' + (i===0?' class="best-row"':'') + '><td>' + s.tp1 + '</td><td>' + s.tp2 + '</td><td>' + s.tp3 + '</td><td>' + s.trail + '</td><td class="' + cls + '">$' + (s.pnl>0?'+':'') + s.pnl.toFixed(2) + '</td><td>' + s.wr + '%</td><td>' + (s.pf<999?s.pf.toFixed(2):'INF') + '</td><td>' + s.sharpe + '</td><td>' + s.dd + '%</td></tr>'; });
    html += '</tbody></table>';
    document.getElementById('tab-sweep').innerHTML = html;
    showTab('sweep', document.querySelectorAll('.tab')[2]);
    setStatus('Swept ' + d.total_configs + ' in ' + d.elapsed_ms + 'ms');
  } catch(e) { setStatus('Error: ' + e.message); }
  finally { setLoading('btnSweep', false); }
}

function setKPI(id, val, cls, sub) {
  var el = document.getElementById(id);
  el.textContent = val;
  el.className = 'val' + (cls ? ' ' + cls : '');
  var lbl = el.parentElement.querySelector('.lbl');
  if (lbl && sub) lbl.textContent = sub;
}

// ── Mode 2 Simulation ────────────────────────────────────────
async function runSim() {
  setLoading('btnSim', true);
  setStatus('Running Mode 2 simulation (event-driven pipeline)...');
  try {
    var p = getParams();
    p.entry_mode = 'every_n';
    p.entry_interval = 100;
    p.use_real_ticks = false;  // Use synthetic ticks from OHLCV (faster)
    var resp = await fetch('/api/sim', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(p) });
    var d = await resp.json();
    if (d.error) { setStatus('Error: ' + d.error); return; }

    lastBacktestData = d;
    var m = d.metrics;
    setKPI('k_pnl', '$' + (m.total_pnl >= 0 ? '+' : '') + m.total_pnl.toFixed(2), m.total_pnl >= 0 ? 'green' : 'red', m.total_pnl_pct.toFixed(2) + '% return');
    setKPI('k_wr', m.win_rate.toFixed(1) + '%', m.win_rate >= 50 ? 'green' : 'red', m.total_trades + ' trades (' + m.winners + 'W/' + m.losers + 'L)');
    var pfStr = m.profit_factor < 1000 ? m.profit_factor.toFixed(2) : 'INF';
    setKPI('k_pf', pfStr, m.profit_factor >= 1.5 ? 'green' : m.profit_factor >= 1 ? 'yellow' : 'red', 'Mode 2: Event-Driven Pipeline');
    setKPI('k_dd', m.max_drawdown_pct.toFixed(2) + '%', m.max_drawdown_pct < 5 ? 'green' : m.max_drawdown_pct < 15 ? 'yellow' : 'red', 'TP1 ' + m.tp1_hit_rate.toFixed(0) + '% | TP2 ' + m.tp2_hit_rate.toFixed(0) + '% | TP3 ' + m.tp3_hit_rate.toFixed(0) + '%');
    setKPI('k_trades', m.total_trades, '', 'Fees $' + m.total_fees_paid.toFixed(2));
    setKPI('k_sharpe', '--', '', 'Mode 2 uses live pipeline');
    setKPI('k_imp', d.meta.tick_source === 'real' ? 'Real Ticks' : 'Synthetic', 'yellow', d.meta.ticks + ' ticks @ ' + d.meta.ticks_per_sec + '/s');
    setKPI('k_exp', '$' + (m.expectancy >= 0 ? '+' : '') + m.expectancy.toFixed(2), m.expectancy >= 0 ? 'green' : 'red', 'per trade');

    renderTVChart(d.price_chart.candles, d.trades);
    simInit(d.price_chart.candles, d.trades, d.config || {});

    var eqDs = [{ label: 'Equity', data: d.equity_chart.data, borderColor: '#f59e0b', backgroundColor: 'rgba(245,158,11,0.08)', fill: true, pointRadius: 0, borderWidth: 1.5, tension: 0.1 }];
    makeChart('chartEquity', 'line', { labels: d.equity_chart.labels, datasets: eqDs }, {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: true, labels: { boxWidth: 10, padding: 6 } } },
      scales: { x: { ticks: { maxTicksLimit: 6, maxRotation: 0 } }, y: { ticks: { callback: function(v) { return '$' + v; } } } }
    });

    var bins = {};
    d.pnl_dist.forEach(function(p) { var b = Math.round(p); bins[b] = (bins[b]||0)+1; });
    var sorted = Object.keys(bins).map(Number).sort(function(a,b) { return a-b; });
    makeChart('chartPnl', 'bar', {
      labels: sorted.map(function(b) { return b + '%'; }),
      datasets: [{ data: sorted.map(function(b) { return bins[b]; }), backgroundColor: sorted.map(function(b) { return b >= 0 ? 'rgba(245,158,11,0.6)' : 'rgba(239,83,80,0.6)'; }), borderWidth: 0 }]
    }, { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { y: { title: { display: true, text: 'Count' } } } });

    var html = '<h3>Mode 2 Trade Log (Event-Driven)</h3><table><thead><tr><th>#</th><th>Side</th><th>Entry</th><th>Exit</th><th>PnL $</th><th>PnL %</th><th>Reason</th><th>TP</th></tr></thead><tbody>';
    d.trades.forEach(function(t) {
      var cls = t.pnl >= 0 ? 'pos' : 'neg';
      var sc = t.side === 'long' ? 'pos' : 'neg';
      var tp = (t.tp1?'1':'') + (t.tp2?' 2':'') + (t.tp3?' 3':'') || '-';
      html += '<tr><td>' + t.id + '</td><td class="' + sc + '">' + t.side.toUpperCase() + '</td><td>$' + t.entry + '</td><td>$' + t.exit + '</td><td class="' + cls + '">$' + (t.pnl>0?'+':'') + t.pnl.toFixed(2) + '</td><td class="' + cls + '">' + (t.pnl_pct>0?'+':'') + t.pnl_pct.toFixed(2) + '%</td><td>' + t.reason + '</td><td>' + tp + '</td></tr>';
    });
    html += '</tbody></table>';
    document.getElementById('tab-trades').innerHTML = html;

    var modeLabel = d.meta.mode + ' | ' + d.meta.tick_source + ' ticks';
    setStatus('Mode 2 done in ' + d.elapsed_ms + 'ms (' + d.engine_ms + 'ms engine) | ' + modeLabel + ' | Press PLAY to simulate');
  } catch(e) { setStatus('Error: ' + e.message); console.error(e); }
  finally { setLoading('btnSim', false); }
}

// ── Mode 3 LFEST-rs ──────────────────────────────────────────
async function runLfest() {
  setLoading('btnLfest', true);
  setStatus('Running Mode 3 (LFEST-rs exchange simulator)...');
  try {
    var p = getParams();
    var resp = await fetch('/api/lfest', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(p) });
    var d = await resp.json();
    if (d.error) { setStatus('Error: ' + d.error); return; }

    lastBacktestData = d;
    var m = d.metrics;
    setKPI('k_pnl', '$' + (m.total_pnl >= 0 ? '+' : '') + m.total_pnl.toFixed(2), m.total_pnl >= 0 ? 'green' : 'red', m.total_pnl_pct.toFixed(2) + '% return');
    setKPI('k_wr', m.win_rate.toFixed(1) + '%', m.win_rate >= 50 ? 'green' : 'red', m.total_trades + ' trades (' + m.winners + 'W/' + m.losers + 'L)');
    var pfStr = m.profit_factor < 1000 ? m.profit_factor.toFixed(2) : 'INF';
    setKPI('k_pf', pfStr, m.profit_factor >= 1.5 ? 'green' : m.profit_factor >= 1 ? 'yellow' : 'red', 'Mode 3: LFEST-rs Exchange Sim');
    var liqStr = (m.liquidations || 0) > 0 ? ' | ' + m.liquidations + ' liquidations' : '';
    setKPI('k_dd', m.max_drawdown_pct.toFixed(2) + '%', m.max_drawdown_pct < 5 ? 'green' : m.max_drawdown_pct < 15 ? 'yellow' : 'red', 'TP1 ' + (m.tp1_hit_rate||0).toFixed(0) + '% | TP2 ' + (m.tp2_hit_rate||0).toFixed(0) + '% | TP3 ' + (m.tp3_hit_rate||0).toFixed(0) + '%' + liqStr);
    setKPI('k_trades', m.total_trades, '', 'Fees $' + m.total_fees_paid.toFixed(2) + liqStr);
    setKPI('k_sharpe', (m.sharpe||0).toFixed(2), (m.sharpe||0) > 0 ? 'green' : 'red', 'Sortino: ' + (m.sortino||0).toFixed(2));
    var imp = m.avg_entry_improvement_pct || 0;
    setKPI('k_imp', (imp >= 0 ? '+' : '') + imp.toFixed(2) + '%', imp > 0 ? 'green' : imp < 0 ? 'red' : '', 'Signals expired: ' + (m.signals_expired || 0));
    setKPI('k_exp', '$' + (m.expectancy >= 0 ? '+' : '') + m.expectancy.toFixed(2), m.expectancy >= 0 ? 'green' : 'red', 'per trade');

    renderTVChart(d.price_chart.candles, d.trades);
    simInit(d.price_chart.candles, d.trades, d.config || {});

    var eqDs = [{ label: 'Equity (LFEST)', data: d.equity_chart.data, borderColor: '#ef4444', backgroundColor: 'rgba(239,68,68,0.08)', fill: true, pointRadius: 0, borderWidth: 1.5, tension: 0.1 }];
    makeChart('chartEquity', 'line', { labels: d.equity_chart.labels, datasets: eqDs }, {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: true, labels: { boxWidth: 10, padding: 6 } } },
      scales: { x: { ticks: { maxTicksLimit: 6, maxRotation: 0 } }, y: { ticks: { callback: function(v) { return '$' + v; } } } }
    });

    var bins = {};
    d.pnl_dist.forEach(function(p) { var b = Math.round(p); bins[b] = (bins[b]||0)+1; });
    var sorted = Object.keys(bins).map(Number).sort(function(a,b) { return a-b; });
    makeChart('chartPnl', 'bar', {
      labels: sorted.map(function(b) { return b + '%'; }),
      datasets: [{ data: sorted.map(function(b) { return bins[b]; }), backgroundColor: sorted.map(function(b) { return b >= 0 ? 'rgba(239,68,68,0.6)' : 'rgba(239,83,80,0.6)'; }), borderWidth: 0 }]
    }, { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { y: { title: { display: true, text: 'Count' } } } });

    var html = '<h3>Mode 3 Trade Log (LFEST-rs Exchange Sim)</h3><table><thead><tr><th>#</th><th>Side</th><th>Entry</th><th>Exit</th><th>PnL $</th><th>PnL %</th><th>Reason</th><th>TP</th></tr></thead><tbody>';
    d.trades.forEach(function(t) {
      var cls = t.pnl >= 0 ? 'pos' : 'neg';
      var sc = t.side === 'long' ? 'pos' : 'neg';
      var tp = (t.tp1?'1':'') + (t.tp2?' 2':'') + (t.tp3?' 3':'') || '-';
      html += '<tr><td>' + t.id + '</td><td class="' + sc + '">' + t.side.toUpperCase() + '</td><td>$' + t.entry + '</td><td>$' + t.exit + '</td><td class="' + cls + '">$' + (t.pnl>0?'+':'') + t.pnl.toFixed(2) + '</td><td class="' + cls + '">' + (t.pnl_pct>0?'+':'') + t.pnl_pct.toFixed(2) + '%</td><td>' + t.reason + '</td><td>' + tp + '</td></tr>';
    });
    html += '</tbody></table>';
    document.getElementById('tab-trades').innerHTML = html;

    setStatus('Mode 3 (LFEST-rs) done in ' + d.elapsed_ms + 'ms (' + d.engine_ms + 'ms engine) | ' + d.meta.ticks + ' ' + (d.meta.tick_source||'') + ' ticks @ ' + d.meta.ticks_per_sec + '/s');
  } catch(e) { setStatus('Error: ' + e.message); console.error(e); }
  finally { setLoading('btnLfest', false); }
}

window.addEventListener('load', function() { setTimeout(runBacktest, 100); });
</script>
</body>
</html>"""


def main():
    parser = argparse.ArgumentParser(description="Agentrade Backtest Dashboard")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8899)
    args = parser.parse_args()

    print("=" * 60)
    print("  AGENTRADE BACKTEST DASHBOARD")
    print("=" * 60)
    print(f"  http://localhost:{args.port}")
    print("=" * 60)

    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()

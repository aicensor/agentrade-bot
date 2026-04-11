"""
Agentrade — Backtest Dashboard

Generates an interactive HTML dashboard from Rust backtest results.
Opens automatically in browser or serves via Python HTTP.

Usage:
    python backtest_dashboard.py                          # SOL/USDC defaults
    python backtest_dashboard.py --symbol BTC/USDC:USDC   # BTC
    python backtest_dashboard.py --side long               # Long only
    python backtest_dashboard.py --serve                   # Start HTTP server
"""

import sys
import json
import time
import argparse
import webbrowser
import http.server
import threading
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
from backtest.data import fetch_ohlcv_range, candles_to_ticks


def fetch_data(symbol, exchange, days):
    """Fetch historical data and convert to ticks."""
    import ccxt
    ex = getattr(ccxt, exchange)({"options": {"defaultType": "swap"}})
    ex.load_markets()

    now_ms = int(time.time() * 1000)
    start_ms = now_ms - (days * 24 * 60 * 60 * 1000)

    # Use 15m for full coverage, 5m for recent detail
    print(f"  Fetching {symbol} data from {exchange} ({days} days)...")

    raw_15m = ex.fetch_ohlcv(symbol, timeframe="15m", limit=5000, since=start_ms)
    time.sleep(0.3)
    raw_5m = ex.fetch_ohlcv(symbol, timeframe="5m", limit=5000, since=start_ms)

    first_5m_ts = raw_5m[0][0] if raw_5m else now_ms
    all_candles = [c for c in raw_15m if c[0] < first_5m_ts] + raw_5m
    all_candles.sort(key=lambda c: c[0])

    timestamps = []
    prices = []
    for c in all_candles:
        ts = c[0] / 1000
        timestamps.extend([ts, ts + 1, ts + 2, ts + 3])
        prices.extend([c[1], c[2], c[3], c[4]])

    hours = (all_candles[-1][0] - all_candles[0][0]) / (1000 * 3600)
    print(f"  Loaded {len(all_candles)} candles ({hours / 24:.1f} days), {len(timestamps):,} ticks")

    return timestamps, prices, all_candles


def run_configs(timestamps, prices, side):
    """Run multiple strategy configs and return results."""
    base = {
        "strategy": {
            "tp1_pct": 3.0, "tp2_pct": 5.0, "tp3_pct": 8.0,
            "tp1_close_pct": 0.33, "tp2_close_pct": 0.33,
            "trail_pct": 1.5, "min_sl_change_pct": 0.1,
        },
        "side": side,
        "position_size": 1.0,
        "leverage": 5.0,
        "initial_capital": 1000.0,
        "slippage_pct": 0.05,
        "fee_pct": 0.035,
        "entry_mode": "every_n",
        "entry_interval": 1,
        "funding_rate_pct": 0.01,
        "funding_interval_sec": 28800.0,
        "enable_liquidation": True,
        "equity_sample_interval": max(1, len(timestamps) // 500),
    }

    configs = [
        ("Immediate Entry", {**base, "trailing_entry": {"enabled": False}}),
        ("Trail 1%", {**base, "trailing_entry": {"enabled": True, "trail_pct": 1.0}}),
        ("Trail 2%", {**base, "trailing_entry": {"enabled": True, "trail_pct": 2.0}}),
        ("Trail 3%", {**base, "trailing_entry": {"enabled": True, "trail_pct": 3.0}}),
        ("ATR(14) x1.0", {**base, "trailing_entry": {"enabled": True, "atr_enabled": True, "atr_period": 14, "atr_multiplier": 1.0}}),
        ("ATR(14) x1.5", {**base, "trailing_entry": {"enabled": True, "atr_enabled": True, "atr_period": 14, "atr_multiplier": 1.5}}),
    ]

    results = []
    for label, cfg in configs:
        r = backtest_core.run_backtest(timestamps, prices, json.dumps(cfg))
        results.append({"label": label, "config": cfg, "result": r})

    # Parameter sweep
    sweep_configs = []
    sweep_labels = []
    tp1_vals = [1.5, 2.0, 3.0, 4.0]
    tp3_vals = [5.0, 8.0, 10.0, 12.0]
    trail_vals = [0.5, 1.0, 1.5, 2.0, 3.0]
    for tp1 in tp1_vals:
        for tp3 in tp3_vals:
            if tp3 <= tp1:
                continue
            tp2 = round((tp1 + tp3) / 2, 1)
            for trail in trail_vals:
                cfg = {**base}
                cfg["strategy"] = {
                    "tp1_pct": tp1, "tp2_pct": tp2, "tp3_pct": tp3,
                    "tp1_close_pct": 0.33, "tp2_close_pct": 0.33,
                    "trail_pct": trail, "min_sl_change_pct": 0.1,
                }
                sweep_configs.append(json.dumps(cfg))
                sweep_labels.append({"tp1": tp1, "tp2": tp2, "tp3": tp3, "trail": trail})

    print(f"  Running {len(sweep_configs)} sweep configs...")
    sweep_results = backtest_core.run_batch(timestamps, prices, sweep_configs)
    sweep = list(zip(sweep_labels, sweep_results))
    sweep.sort(key=lambda x: x[1]["metrics"]["total_pnl"], reverse=True)

    return results, sweep


def generate_html(symbol, side, days, results, sweep, candles, timestamps, prices):
    """Generate the HTML dashboard."""
    primary = results[0]["result"]
    pm = primary["metrics"]

    # Find best trailing config
    best_trail = max(results[1:], key=lambda r: r["result"]["metrics"]["total_pnl"]) if len(results) > 1 else results[0]

    # Price data for chart (downsample candles)
    price_labels = []
    price_data = []
    for c in candles[::max(1, len(candles) // 300)]:
        dt = datetime.fromtimestamp(c[0] / 1000, tz=timezone.utc)
        price_labels.append(dt.strftime("%m/%d %H:%M"))
        price_data.append(round(c[4], 2))  # close

    # Equity curves
    equity_datasets = []
    colors = ["#3b82f6", "#f59e0b", "#10b981", "#ef4444", "#8b5cf6", "#ec4899"]
    for i, r in enumerate(results):
        eq = r["result"]["equity_curve"]
        sampled = eq[::max(1, len(eq) // 300)]
        equity_datasets.append({
            "label": r["label"],
            "data": [round(e[1], 2) for e in sampled],
            "color": colors[i % len(colors)],
        })

    # Equity labels from first result
    eq0 = results[0]["result"]["equity_curve"]
    eq_sampled = eq0[::max(1, len(eq0) // 300)]
    eq_labels = []
    for e in eq_sampled:
        dt = datetime.fromtimestamp(e[0], tz=timezone.utc)
        eq_labels.append(dt.strftime("%m/%d %H:%M"))

    # Trade markers for primary result
    trade_markers = []
    for t in primary["trades"][:100]:
        trade_markers.append({
            "id": t["trade_id"],
            "side": t.get("side", side),
            "entry": round(t["entry_price"], 2),
            "exit": round(t["exit_price"], 2),
            "pnl": round(t["realized_pnl"], 2),
            "pnl_pct": round(t["realized_pnl_pct"], 2),
            "reason": t["exit_reason"],
            "tp1": t["tp1_hit"],
            "tp2": t["tp2_hit"],
            "tp3": t["tp3_hit"],
            "signal": round(t.get("signal_price", 0), 2),
            "improvement": round(t.get("entry_improvement_pct", 0), 2),
        })

    # Comparison table data
    comparison = []
    for r in results:
        m = r["result"]["metrics"]
        comparison.append({
            "label": r["label"],
            "trades": m["total_trades"],
            "wr": round(m["win_rate"], 1),
            "pnl": round(m["total_pnl"], 2),
            "pnl_pct": round(m["total_pnl_pct"], 2),
            "pf": round(m["profit_factor"], 2) if m["profit_factor"] < 1000 else 999,
            "sharpe": round(m["sharpe"], 2),
            "sortino": round(m["sortino"], 2),
            "dd": round(m["max_drawdown_pct"], 2),
            "expectancy": round(m["expectancy"], 2),
            "avg_imp": round(m.get("avg_entry_improvement_pct", 0), 2),
            "expired": m.get("signals_expired", 0),
            "tp1": round(m.get("tp1_hit_rate", 0), 1),
            "tp2": round(m.get("tp2_hit_rate", 0), 1),
            "tp3": round(m.get("tp3_hit_rate", 0), 1),
            "fees": round(m.get("total_fees_paid", 0), 2),
            "funding": round(m.get("total_funding_paid", 0), 2),
        })

    # Sweep top 20
    sweep_top = []
    for params, r in sweep[:20]:
        m = r["metrics"]
        sweep_top.append({
            "tp1": params["tp1"], "tp2": params["tp2"],
            "tp3": params["tp3"], "trail": params["trail"],
            "pnl": round(m["total_pnl"], 2),
            "wr": round(m["win_rate"], 1),
            "pf": round(m["profit_factor"], 2) if m["profit_factor"] < 1000 else 999,
            "sharpe": round(m["sharpe"], 2),
            "dd": round(m["max_drawdown_pct"], 2),
            "trades": m["total_trades"],
        })

    # PnL distribution for primary
    pnl_dist = [round(t["realized_pnl_pct"], 2) for t in primary["trades"]]

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Agentrade Backtest Dashboard — {symbol} {side.upper()}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
<style>
  :root {{
    --bg: #0f1117; --card: #1a1d2e; --border: #2d3148;
    --text: #e2e8f0; --dim: #94a3b8; --accent: #3b82f6;
    --green: #10b981; --red: #ef4444; --yellow: #f59e0b;
  }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ background: var(--bg); color: var(--text); font-family: 'SF Mono', 'Fira Code', monospace; font-size: 13px; padding: 20px; }}
  .header {{ text-align: center; margin-bottom: 24px; }}
  .header h1 {{ font-size: 20px; color: var(--accent); margin-bottom: 4px; }}
  .header .sub {{ color: var(--dim); font-size: 12px; }}
  .grid {{ display: grid; gap: 16px; margin-bottom: 16px; }}
  .grid-4 {{ grid-template-columns: repeat(4, 1fr); }}
  .grid-2 {{ grid-template-columns: repeat(2, 1fr); }}
  .grid-1 {{ grid-template-columns: 1fr; }}
  .card {{ background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }}
  .card h3 {{ color: var(--accent); font-size: 12px; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 12px; }}
  .metric {{ margin-bottom: 8px; }}
  .metric .label {{ color: var(--dim); font-size: 11px; }}
  .metric .value {{ font-size: 18px; font-weight: bold; }}
  .metric .value.green {{ color: var(--green); }}
  .metric .value.red {{ color: var(--red); }}
  .metric .value.yellow {{ color: var(--yellow); }}
  table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
  th {{ color: var(--dim); text-align: right; padding: 6px 8px; border-bottom: 1px solid var(--border); font-weight: normal; text-transform: uppercase; font-size: 10px; letter-spacing: 0.5px; }}
  th:first-child {{ text-align: left; }}
  td {{ padding: 6px 8px; text-align: right; border-bottom: 1px solid var(--border); }}
  td:first-child {{ text-align: left; }}
  .pos {{ color: var(--green); }}
  .neg {{ color: var(--red); }}
  .chart-container {{ position: relative; height: 280px; }}
  .tag {{ display: inline-block; padding: 2px 6px; border-radius: 3px; font-size: 10px; }}
  .tag-green {{ background: rgba(16,185,129,0.2); color: var(--green); }}
  .tag-red {{ background: rgba(239,68,68,0.2); color: var(--red); }}
  .tag-yellow {{ background: rgba(245,158,11,0.2); color: var(--yellow); }}
  .best-row {{ background: rgba(16,185,129,0.08); }}
  @media (max-width: 900px) {{
    .grid-4 {{ grid-template-columns: repeat(2, 1fr); }}
    .grid-2 {{ grid-template-columns: 1fr; }}
  }}
</style>
</head>
<body>

<div class="header">
  <h1>AGENTRADE BACKTEST DASHBOARD</h1>
  <div class="sub">{symbol} | {side.upper()} | {days} days | Rust Engine | {now_str}</div>
</div>

<!-- Key Metrics -->
<div class="grid grid-4">
  <div class="card">
    <h3>Total PnL</h3>
    <div class="metric">
      <div class="value {'green' if pm['total_pnl'] >= 0 else 'red'}">${pm['total_pnl']:+.2f}</div>
      <div class="label">{pm['total_pnl_pct']:+.2f}% return</div>
    </div>
  </div>
  <div class="card">
    <h3>Win Rate</h3>
    <div class="metric">
      <div class="value {'green' if pm['win_rate'] >= 50 else 'red'}">{pm['win_rate']:.1f}%</div>
      <div class="label">{pm['total_trades']} trades ({pm['winners']}W / {pm['losers']}L)</div>
    </div>
  </div>
  <div class="card">
    <h3>Profit Factor</h3>
    <div class="metric">
      <div class="value {'green' if pm['profit_factor'] >= 1.5 else 'yellow' if pm['profit_factor'] >= 1 else 'red'}">{'%.2f' % pm['profit_factor'] if pm['profit_factor'] < 1000 else 'INF'}</div>
      <div class="label">Sharpe: {pm['sharpe']:.2f} | Sortino: {pm['sortino']:.2f}</div>
    </div>
  </div>
  <div class="card">
    <h3>Max Drawdown</h3>
    <div class="metric">
      <div class="value {'green' if pm['max_drawdown_pct'] < 5 else 'yellow' if pm['max_drawdown_pct'] < 15 else 'red'}">{pm['max_drawdown_pct']:.2f}%</div>
      <div class="label">Expectancy: ${pm['expectancy']:+.2f}/trade</div>
    </div>
  </div>
</div>

<!-- Charts Row -->
<div class="grid grid-2">
  <div class="card">
    <h3>Price Chart — {symbol}</h3>
    <div class="chart-container"><canvas id="priceChart"></canvas></div>
  </div>
  <div class="card">
    <h3>Equity Curves — Entry Mode Comparison</h3>
    <div class="chart-container"><canvas id="equityChart"></canvas></div>
  </div>
</div>

<!-- PnL Distribution + TP Hit Rates -->
<div class="grid grid-2">
  <div class="card">
    <h3>PnL Distribution (%)</h3>
    <div class="chart-container"><canvas id="pnlChart"></canvas></div>
  </div>
  <div class="card">
    <h3>Entry Mode Comparison</h3>
    <div class="chart-container"><canvas id="compChart"></canvas></div>
  </div>
</div>

<!-- Comparison Table -->
<div class="grid grid-1">
  <div class="card">
    <h3>Trailing Entry Comparison — Immediate vs Trailing</h3>
    <table>
      <thead>
        <tr>
          <th>Mode</th><th>Trades</th><th>Win Rate</th><th>PnL $</th><th>PnL %</th>
          <th>PF</th><th>Sharpe</th><th>Sortino</th><th>Max DD</th><th>Expectancy</th>
          <th>Entry Imp.</th><th>TP1%</th><th>TP2%</th><th>TP3%</th><th>Fees</th>
        </tr>
      </thead>
      <tbody>
"""
    for i, c in enumerate(comparison):
        is_best = c["pnl"] == max(x["pnl"] for x in comparison)
        row_class = ' class="best-row"' if is_best else ""
        pnl_class = "pos" if c["pnl"] >= 0 else "neg"
        imp_class = "pos" if c["avg_imp"] > 0 else ("neg" if c["avg_imp"] < 0 else "")
        pf_str = f'{c["pf"]:.2f}' if c["pf"] < 999 else "INF"
        html += f"""        <tr{row_class}>
          <td>{c['label']}</td><td>{c['trades']}</td><td>{c['wr']}%</td>
          <td class="{pnl_class}">${c['pnl']:+.2f}</td><td class="{pnl_class}">{c['pnl_pct']:+.2f}%</td>
          <td>{pf_str}</td><td>{c['sharpe']}</td><td>{c['sortino']}</td>
          <td>{c['dd']}%</td><td>${c['expectancy']:+.2f}</td>
          <td class="{imp_class}">{c['avg_imp']:+.2f}%</td>
          <td>{c['tp1']}%</td><td>{c['tp2']}%</td><td>{c['tp3']}%</td>
          <td>${c['fees']:.2f}</td>
        </tr>
"""

    html += """      </tbody>
    </table>
  </div>
</div>

<!-- Trade Log -->
<div class="grid grid-1">
  <div class="card">
    <h3>Trade Log (Immediate Entry)</h3>
    <table>
      <thead>
        <tr>
          <th>#</th><th>Side</th><th>Signal</th><th>Entry</th><th>Exit</th>
          <th>PnL $</th><th>PnL %</th><th>Exit Reason</th>
          <th>TP1</th><th>TP2</th><th>TP3</th><th>Improvement</th>
        </tr>
      </thead>
      <tbody>
"""
    for t in trade_markers:
        pnl_class = "pos" if t["pnl"] >= 0 else "neg"
        sig_str = f'${t["signal"]:.2f}' if t["signal"] > 0 else "-"
        imp_str = f'{t["improvement"]:+.2f}%' if t["signal"] > 0 else "-"
        imp_class = "pos" if t["improvement"] > 0 else ("neg" if t["improvement"] < 0 else "")
        html += f"""        <tr>
          <td>{t['id']}</td><td>{t['side']}</td><td>{sig_str}</td>
          <td>${t['entry']:.2f}</td><td>${t['exit']:.2f}</td>
          <td class="{pnl_class}">${t['pnl']:+.2f}</td>
          <td class="{pnl_class}">{t['pnl_pct']:+.2f}%</td>
          <td>{t['reason']}</td>
          <td>{'Y' if t['tp1'] else '-'}</td>
          <td>{'Y' if t['tp2'] else '-'}</td>
          <td>{'Y' if t['tp3'] else '-'}</td>
          <td class="{imp_class}">{imp_str}</td>
        </tr>
"""

    html += """      </tbody>
    </table>
  </div>
</div>

<!-- Parameter Sweep -->
<div class="grid grid-1">
  <div class="card">
    <h3>Parameter Sweep — Top 20 Configs (sorted by PnL)</h3>
    <table>
      <thead>
        <tr><th>TP1%</th><th>TP2%</th><th>TP3%</th><th>Trail%</th><th>PnL $</th><th>Win Rate</th><th>PF</th><th>Sharpe</th><th>Max DD</th><th>Trades</th></tr>
      </thead>
      <tbody>
"""
    for i, s in enumerate(sweep_top):
        is_best = i == 0
        row_class = ' class="best-row"' if is_best else ""
        pnl_class = "pos" if s["pnl"] >= 0 else "neg"
        pf_str = f'{s["pf"]:.2f}' if s["pf"] < 999 else "INF"
        html += f"""        <tr{row_class}>
          <td>{s['tp1']}</td><td>{s['tp2']}</td><td>{s['tp3']}</td><td>{s['trail']}</td>
          <td class="{pnl_class}">${s['pnl']:+.2f}</td><td>{s['wr']}%</td>
          <td>{pf_str}</td><td>{s['sharpe']}</td><td>{s['dd']}%</td><td>{s['trades']}</td>
        </tr>
"""

    html += f"""      </tbody>
    </table>
  </div>
</div>

<div style="text-align: center; color: var(--dim); margin-top: 16px; font-size: 11px;">
  Powered by Agentrade Rust Backtest Engine | Generated {now_str}
</div>

<script>
const priceLabels = {json.dumps(price_labels)};
const priceData = {json.dumps(price_data)};
const eqLabels = {json.dumps(eq_labels)};
const eqDatasets = {json.dumps(equity_datasets)};
const pnlDist = {json.dumps(pnl_dist)};
const comparison = {json.dumps(comparison)};

// Chart defaults
Chart.defaults.color = '#94a3b8';
Chart.defaults.borderColor = '#2d3148';
Chart.defaults.font.family = "'SF Mono', 'Fira Code', monospace";
Chart.defaults.font.size = 11;

// Price chart
new Chart(document.getElementById('priceChart'), {{
  type: 'line',
  data: {{
    labels: priceLabels,
    datasets: [{{
      label: '{symbol}',
      data: priceData,
      borderColor: '#3b82f6',
      backgroundColor: 'rgba(59,130,246,0.1)',
      fill: true,
      pointRadius: 0,
      borderWidth: 1.5,
      tension: 0.1,
    }}]
  }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    plugins: {{ legend: {{ display: false }} }},
    scales: {{
      x: {{ display: true, ticks: {{ maxTicksLimit: 8, maxRotation: 0 }} }},
      y: {{ display: true, ticks: {{ callback: v => '$' + v }} }}
    }}
  }}
}});

// Equity curves
new Chart(document.getElementById('equityChart'), {{
  type: 'line',
  data: {{
    labels: eqLabels,
    datasets: eqDatasets.map(d => ({{
      label: d.label,
      data: d.data,
      borderColor: d.color,
      pointRadius: 0,
      borderWidth: 1.5,
      tension: 0.1,
      fill: false,
    }}))
  }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    plugins: {{ legend: {{ position: 'top', labels: {{ boxWidth: 12, padding: 8 }} }} }},
    scales: {{
      x: {{ display: true, ticks: {{ maxTicksLimit: 6, maxRotation: 0 }} }},
      y: {{ display: true, ticks: {{ callback: v => '$' + v }} }}
    }}
  }}
}});

// PnL distribution
const pnlBins = {{}};
pnlDist.forEach(p => {{
  const bin = Math.round(p);
  pnlBins[bin] = (pnlBins[bin] || 0) + 1;
}});
const sortedBins = Object.keys(pnlBins).map(Number).sort((a,b) => a - b);
new Chart(document.getElementById('pnlChart'), {{
  type: 'bar',
  data: {{
    labels: sortedBins.map(b => b + '%'),
    datasets: [{{
      data: sortedBins.map(b => pnlBins[b]),
      backgroundColor: sortedBins.map(b => b >= 0 ? 'rgba(16,185,129,0.6)' : 'rgba(239,68,68,0.6)'),
      borderColor: sortedBins.map(b => b >= 0 ? '#10b981' : '#ef4444'),
      borderWidth: 1,
    }}]
  }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    plugins: {{ legend: {{ display: false }} }},
    scales: {{
      x: {{ display: true }},
      y: {{ display: true, title: {{ display: true, text: 'Count' }} }}
    }}
  }}
}});

// Comparison bar chart
new Chart(document.getElementById('compChart'), {{
  type: 'bar',
  data: {{
    labels: comparison.map(c => c.label),
    datasets: [
      {{
        label: 'PnL $',
        data: comparison.map(c => c.pnl),
        backgroundColor: comparison.map(c => c.pnl >= 0 ? 'rgba(16,185,129,0.7)' : 'rgba(239,68,68,0.7)'),
        borderWidth: 0,
        yAxisID: 'y',
      }},
      {{
        label: 'Win Rate %',
        data: comparison.map(c => c.wr),
        type: 'line',
        borderColor: '#f59e0b',
        pointBackgroundColor: '#f59e0b',
        pointRadius: 4,
        borderWidth: 2,
        fill: false,
        yAxisID: 'y1',
      }}
    ]
  }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    plugins: {{ legend: {{ position: 'top', labels: {{ boxWidth: 12, padding: 8 }} }} }},
    scales: {{
      x: {{ ticks: {{ maxRotation: 45 }} }},
      y: {{ position: 'left', title: {{ display: true, text: 'PnL ($)' }} }},
      y1: {{ position: 'right', min: 0, max: 100, title: {{ display: true, text: 'Win Rate (%)' }}, grid: {{ display: false }} }}
    }}
  }}
}});
</script>

</body>
</html>"""

    return html


def main():
    parser = argparse.ArgumentParser(description="Agentrade Backtest Dashboard")
    parser.add_argument("--symbol", default="SOL/USDC:USDC", help="Trading pair")
    parser.add_argument("--exchange", default="hyperliquid", help="Exchange")
    parser.add_argument("--side", default="long", choices=["long", "short"])
    parser.add_argument("--days", type=int, default=30, help="Days of history")
    parser.add_argument("--output", default="/tmp/agentrade_dashboard.html")
    parser.add_argument("--serve", action="store_true", help="Start HTTP server")
    parser.add_argument("--port", type=int, default=8899)
    args = parser.parse_args()

    print("=" * 60)
    print("  AGENTRADE BACKTEST DASHBOARD")
    print("=" * 60)

    timestamps, prices, candles = fetch_data(args.symbol, args.exchange, args.days)
    results, sweep = run_configs(timestamps, prices, args.side)

    print("  Generating dashboard...")
    html = generate_html(
        args.symbol, args.side, args.days,
        results, sweep, candles, timestamps, prices,
    )

    out_path = Path(args.output)
    out_path.write_text(html)
    print(f"  Dashboard saved to: {out_path}")

    if args.serve:
        import os
        os.chdir(str(out_path.parent))

        class Handler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, format, *a):
                pass

        server = http.server.HTTPServer(("0.0.0.0", args.port), Handler)
        print(f"  Serving at http://localhost:{args.port}/{out_path.name}")
        print("  Press Ctrl+C to stop")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n  Server stopped")
    else:
        print(f"  Open in browser: file://{out_path.resolve()}")
        try:
            webbrowser.open(f"file://{out_path.resolve()}")
        except Exception:
            pass


if __name__ == "__main__":
    main()

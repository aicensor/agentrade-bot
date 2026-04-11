"""
Real-time tick collector via Hyperliquid WebSocket.

Streams actual trades at sub-1s resolution and saves to compressed CSV.
Run this to collect data, then use Mode 3 on the saved ticks.

Usage:
    python -m backtest.tick_collector --coin SOL --minutes 60
    python -m backtest.tick_collector --coin SOL --hours 4
    python -m backtest.tick_collector --coin BTC --minutes 10
"""

import argparse
import csv
import gzip
import json
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

TICK_DIR = Path(__file__).parent.parent / "data" / "ticks"
TICK_DIR.mkdir(parents=True, exist_ok=True)

WS_URL = "wss://api.hyperliquid.xyz/ws"


def collect_ticks(coin: str, duration_sec: float, output: Path | None = None):
    """Stream trades from Hyperliquid WS and save to gzipped CSV."""
    import websocket

    if output is None:
        ts_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
        dur_label = f"{int(duration_sec // 60)}m" if duration_sec < 3600 else f"{duration_sec / 3600:.1f}h"
        output = TICK_DIR / f"{coin}_{ts_str}_{dur_label}.csv.gz"

    ticks = []
    start_time = time.time()
    tick_count = 0
    stop = False

    def on_signal(sig, frame):
        nonlocal stop
        stop = True
        print(f"\nStopping... collected {tick_count} ticks")

    signal.signal(signal.SIGINT, on_signal)

    def on_message(ws, message):
        nonlocal tick_count, stop
        data = json.loads(message)

        if data.get("channel") == "trades":
            for trade in data.get("data", []):
                tick_count += 1
                ticks.append({
                    "timestamp": trade["time"] / 1000.0,  # ms -> sec
                    "price": float(trade["px"]),
                    "quantity": float(trade["sz"]),
                    "side": trade["side"],  # B or A
                })

                if tick_count % 500 == 0:
                    elapsed = time.time() - start_time
                    rate = tick_count / elapsed
                    remaining = duration_sec - elapsed
                    print(f"  {tick_count:,} ticks | {elapsed:.0f}s elapsed | {rate:.1f} ticks/s | {remaining:.0f}s remaining", end="\r")

        elapsed = time.time() - start_time
        if elapsed >= duration_sec or stop:
            ws.close()

    def on_open(ws):
        sub = {
            "method": "subscribe",
            "subscription": {"type": "trades", "coin": coin},
        }
        ws.send(json.dumps(sub))
        print(f"Subscribed to {coin} trades. Collecting for {duration_sec:.0f}s...")

    def on_error(ws, error):
        print(f"WS error: {error}")

    def on_close(ws, status, msg):
        pass

    ws = websocket.WebSocketApp(
        WS_URL,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )
    ws.run_forever()

    # Save to gzipped CSV
    if ticks:
        ticks.sort(key=lambda t: t["timestamp"])
        with gzip.open(output, "wt", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["timestamp", "price", "quantity", "side"])
            writer.writeheader()
            writer.writerows(ticks)

        duration_actual = ticks[-1]["timestamp"] - ticks[0]["timestamp"]
        tps = len(ticks) / duration_actual if duration_actual > 0 else 0

        # Show gaps
        gaps = [ticks[i + 1]["timestamp"] - ticks[i]["timestamp"] for i in range(min(len(ticks) - 1, 1000))]
        gaps.sort()
        median_gap = gaps[len(gaps) // 2] if gaps else 0

        print(f"\nSaved {len(ticks):,} ticks to {output}")
        print(f"  Duration: {duration_actual:.1f}s ({duration_actual / 60:.1f}m)")
        print(f"  Rate: {tps:.1f} ticks/sec")
        print(f"  Median gap: {median_gap * 1000:.0f}ms")
        print(f"  Min gap: {gaps[0] * 1000:.0f}ms | Max gap: {gaps[-1] * 1000:.0f}ms")
        print(f"  Price range: {min(t['price'] for t in ticks):.4f} - {max(t['price'] for t in ticks):.4f}")
    else:
        print("No ticks collected!")

    return output


def load_ticks(path: Path) -> tuple[list[float], list[float]]:
    """Load ticks from gzipped CSV, return (timestamps, prices)."""
    timestamps = []
    prices = []
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt") as f:
        reader = csv.DictReader(f)
        for row in reader:
            timestamps.append(float(row["timestamp"]))
            prices.append(float(row["price"]))
    return timestamps, prices


def list_tick_files(coin: str | None = None) -> list[dict]:
    """List available tick data files."""
    files = []
    for f in sorted(TICK_DIR.glob("*.csv*")):
        name = f.name
        parts = name.replace(".csv.gz", "").replace(".csv", "").split("_")
        file_coin = parts[0] if parts else "?"

        if coin and file_coin.upper() != coin.upper():
            continue

        # Get file info
        size_mb = f.stat().st_size / (1024 * 1024)

        # Quick peek at tick count
        opener = gzip.open if str(f).endswith(".gz") else open
        tick_count = 0
        first_ts = last_ts = 0
        try:
            with opener(f, "rt") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    if tick_count == 0:
                        first_ts = float(row["timestamp"])
                    last_ts = float(row["timestamp"])
                    tick_count += 1
        except Exception:
            pass

        duration_min = (last_ts - first_ts) / 60 if last_ts > first_ts else 0

        files.append({
            "path": str(f),
            "coin": file_coin,
            "ticks": tick_count,
            "duration_min": round(duration_min, 1),
            "size_mb": round(size_mb, 2),
            "name": name,
        })
    return files


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Collect real tick data from Hyperliquid WS")
    parser.add_argument("--coin", default="SOL", help="Coin to track (default: SOL)")
    parser.add_argument("--minutes", type=float, default=0, help="Duration in minutes")
    parser.add_argument("--hours", type=float, default=0, help="Duration in hours")
    parser.add_argument("--list", action="store_true", help="List available tick files")
    args = parser.parse_args()

    if args.list:
        files = list_tick_files()
        if not files:
            print("No tick files found.")
        else:
            print(f"{'File':<40} {'Coin':>5} {'Ticks':>10} {'Duration':>10} {'Size':>8}")
            print("-" * 80)
            for f in files:
                print(f"{f['name']:<40} {f['coin']:>5} {f['ticks']:>10,} {f['duration_min']:>8.1f}m {f['size_mb']:>7.2f}MB")
        sys.exit(0)

    duration = args.minutes * 60 + args.hours * 3600
    if duration <= 0:
        duration = 60  # Default 1 minute
        print("No duration specified, defaulting to 1 minute")

    # Check websocket-client is installed
    try:
        import websocket
    except ImportError:
        print("Installing websocket-client...")
        import subprocess
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--user", "--break-system-packages", "websocket-client"])

    collect_ticks(args.coin.upper(), duration)

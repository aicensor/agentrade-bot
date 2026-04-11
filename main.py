"""
Agentrade Engine v2 — Entry Point

Usage:
    python main.py                          # Run with default config
    python main.py --config my_config.yaml  # Run with custom config

Environment variables:
    TELEGRAM_BOT_TOKEN      Telegram bot token
    TELEGRAM_ADMIN_CHAT_ID  Admin chat ID for system alerts
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import structlog
from dotenv import load_dotenv

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

# Load .env before any config reads
load_dotenv(Path(__file__).parent / ".env")

from core.engine import Engine
from core.types import MultiLevelTrailConfig, Position, Side, UserConfig


def setup_logging(level: str = "INFO", fmt: str = "console") -> None:
    """Configure structured logging"""
    processors = [
        structlog.stdlib.add_log_level,
        structlog.dev.set_exc_info,
        structlog.processors.TimeStamper(fmt="iso"),
    ]

    if fmt == "json":
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=True))

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


async def main(config_path: str) -> None:
    """Main async entry point"""
    logger = structlog.get_logger("main")

    logger.info("=" * 60)
    logger.info("  AGENTRADE ENGINE v2")
    logger.info("  Real-time trailing TP/SL engine")
    logger.info("=" * 60)

    engine = Engine(config_path=config_path)
    await engine.start()

    logger.info("engine_ready", stats=engine.stats)

    # Block until shutdown signal (Ctrl+C)
    await engine.wait()


async def demo() -> None:
    """
    Demo mode — runs engine with a simulated position.
    Use this to verify the engine starts correctly without live exchange.

    Usage: python main.py --demo
    """
    logger = structlog.get_logger("demo")
    logger.info("running_in_demo_mode")

    engine = Engine(config_path="config/default.yaml")

    # Start without exchange connections
    engine._running = True
    await engine.state_store.connect()
    await engine.position_manager.start()
    await engine.order_queue.start()
    await engine.notifier.start()

    # Add a fake position to verify the pipeline
    from core.types import PositionState

    demo_position = Position(
        user_id="demo_user",
        symbol="SOL/USDT:USDT",
        side=Side.SHORT,
        exchange="bybit",
        entry_price=90.83,
        size=22.7,
        leverage=20.0,
        margin_mode="cross",
        state=PositionState.OPEN,
        strategy_name="multi_level_trail",
        strategy_config={
            "tp1_pct": 3.0,
            "tp2_pct": 5.0,
            "tp3_pct": 8.0,
            "tp1_close_pct": 0.33,
            "tp2_close_pct": 0.33,
            "tp3_close_pct": 0.0,
            "trail_pct": 1.5,
            "min_sl_change_pct": 0.1,
            "sl_type": "limit",
            "sl_buffer_pct": 0.3,
        },
    )
    engine.position_manager.add_position(demo_position)

    # Simulate price ticks
    from core.types import Event, PriceUpdate
    import time

    prices = [
        90.00, 89.50, 89.00, 88.50, 88.00,  # Price dropping (good for short)
        87.50, 87.00, 86.50, 86.00,           # Approaching TP1 (3% = $88.10)
        85.50, 85.00, 84.50,                    # Past TP1 → SL should move to breakeven
        84.00, 83.50, 83.00,                    # Approaching TP2 (5% = $86.29)
        82.50, 82.00, 81.50,                    # Past TP2 → SL should move to TP1 price
        81.00, 80.50, 80.00,                    # Approaching TP3 (8% = $83.56)
        79.50, 79.00, 78.50,                    # Past TP3 → trailing activated
        79.00, 79.50, 80.00,                    # Price bouncing up
        80.50,                                   # SL should trail at 1.5% above lowest
    ]

    logger.info("simulating_price_ticks", count=len(prices))

    for i, price in enumerate(prices):
        update = PriceUpdate(
            symbol="SOL/USDT:USDT",
            last_price=price,
            mark_price=price,  # Same for demo
            timestamp=time.time(),
            exchange="bybit",
        )

        await engine.event_bus.emit(Event(name="price_update", data=update))
        await asyncio.sleep(0.1)  # 100ms between ticks

        # Log position state
        pos = engine.position_manager.get_position(demo_position.position_key)
        if pos:
            pnl = ((pos.entry_price - price) / pos.entry_price) * 100
            logger.info(
                f"tick_{i+1:02d}",
                price=price,
                pnl_pct=f"{pnl:.2f}%",
                sl=pos.current_sl or "none",
                tp1=pos.tp1_hit,
                tp2=pos.tp2_hit,
                tp3=pos.tp3_hit,
                state=pos.state.value,
            )

    # Print final stats
    logger.info("demo_complete", stats=engine.stats)
    logger.info(
        "order_queue_stats",
        queued=engine.order_queue.stats["total_queued"],
        deduped=engine.order_queue.stats["total_deduped"],
        executed=engine.order_queue.stats["total_executed"],
    )

    await engine.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Agentrade Engine v2")
    parser.add_argument(
        "--config",
        default="config/default.yaml",
        help="Path to config YAML file",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Run in demo mode with simulated prices",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    parser.add_argument(
        "--log-format",
        default="console",
        choices=["console", "json"],
    )

    args = parser.parse_args()

    setup_logging(level=args.log_level, fmt=args.log_format)

    if args.demo:
        asyncio.run(demo())
    else:
        asyncio.run(main(args.config))

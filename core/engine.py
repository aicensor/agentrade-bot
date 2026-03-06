"""
Agentrade Engine v2 — Main Engine

Wires all components together and manages the lifecycle.
This is the top-level orchestrator.

Flow:
  1. Load config
  2. Connect to persistence (Redis / file)
  3. Restore state from snapshot (Problem #3)
  4. Start price feeds (WebSocket)
  5. Start position manager (evaluates strategies per tick)
  6. Start order queue (batches + executes)
  7. Start periodic reconciliation (Problem #4)
  8. Start periodic snapshots (Problem #3)
  9. Start Telegram notifier
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from pathlib import Path
from typing import Any

import structlog
import yaml

from core.event_bus import EventBus
from core.types import UserConfig
from exchange.rate_limiter import RateLimiterPool
from execution.order_executor import OrderExecutor
from execution.order_queue import OrderQueue
from execution.position_manager import PositionManager
from feed.price_feed import PriceFeed
from notification.telegram import TelegramNotifier
from persistence.state_store import StateStore

logger = structlog.get_logger(__name__)


class Engine:
    """
    Main engine. One instance per deployment.

    Usage:
        engine = Engine(config_path="config/default.yaml")
        await engine.start()
        await engine.wait()  # Runs until shutdown signal
    """

    def __init__(self, config_path: str = "config/default.yaml") -> None:
        self.config = self._load_config(config_path)
        self._running = False
        self._start_time = 0.0

        # ─── Core Components ──────────────────────────────────────────
        self.event_bus = EventBus()
        self.rate_pool = RateLimiterPool()

        # ─── Modules ─────────────────────────────────────────────────
        self.position_manager = PositionManager(self.event_bus)

        self.order_executor = OrderExecutor(self.rate_pool)
        self.order_executor.set_position_manager(self.position_manager)

        self.order_queue = OrderQueue(
            event_bus=self.event_bus,
            execute_callback=self.order_executor.execute,
            flush_interval_ms=self.config.get("engine", {}).get("flush_interval_ms", 500),
            max_jitter_ms=self.config.get("engine", {}).get("max_jitter_ms", 20),
        )

        # Persistence
        persist_cfg = self.config.get("persistence", {})
        self.state_store = StateStore(
            redis_url=persist_cfg.get("redis_url"),
            fallback_path=persist_cfg.get("fallback_path", "state/positions.json"),
            snapshot_interval_sec=self.config.get("engine", {}).get("snapshot_interval_sec", 5),
        )

        # Telegram
        tg_cfg = self.config.get("telegram", {})
        bot_token = tg_cfg.get("bot_token") or os.getenv("TELEGRAM_BOT_TOKEN", "")
        admin_chat = tg_cfg.get("admin_chat_id") or int(os.getenv("TELEGRAM_ADMIN_CHAT_ID", "0"))
        self.notifier = TelegramNotifier(
            bot_token=bot_token,
            event_bus=self.event_bus,
            default_chat_id=admin_chat,
            verbose_trailing=tg_cfg.get("verbose_trailing", False),
        )

        # Price feeds — one per exchange
        self._price_feeds: dict[str, PriceFeed] = {}

        # Background tasks
        self._tasks: list[asyncio.Task] = []

    # ─── Lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start all engine components"""
        self._running = True
        self._start_time = time.time()

        logger.info("engine_starting", config_keys=list(self.config.keys()))

        # 1. Connect persistence
        await self.state_store.connect()

        # 2. Restore state from last snapshot
        snapshot = await self.state_store.load_snapshot()
        if snapshot:
            count = self.position_manager.restore(snapshot)
            logger.info("state_restored", positions=count)

        # 3. Start position manager (registers event handlers)
        await self.position_manager.start()

        # 4. Start order queue
        await self.order_queue.start()

        # 5. Start notifier
        await self.notifier.start()

        # 6. Start price feeds for all active symbols
        symbols = self.position_manager.active_symbols
        if symbols:
            await self._start_price_feeds(symbols)

        # 7. Start periodic tasks
        await self.state_store.start_periodic_snapshots(
            get_positions_fn=self.position_manager.snapshot,
        )

        self._tasks.append(
            asyncio.create_task(
                self._reconciliation_loop(),
                name="reconciliation",
            )
        )

        # 8. Register shutdown handlers
        self._register_signals()

        logger.info(
            "engine_started",
            positions=len(self.position_manager.all_positions),
            symbols=len(symbols),
            feeds=len(self._price_feeds),
        )

    async def stop(self) -> None:
        """Graceful shutdown"""
        logger.info("engine_stopping")
        self._running = False

        # Final snapshot before shutdown
        positions = self.position_manager.snapshot()
        if positions:
            await self.state_store.save_snapshot(positions)
            logger.info("final_snapshot_saved", positions=len(positions))

        # Stop components in reverse order
        await self.order_queue.stop()
        await self.notifier.stop()
        await self.state_store.stop()

        for feed in self._price_feeds.values():
            await feed.stop()

        await self.order_executor.close_all()

        # Cancel background tasks
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

        logger.info("engine_stopped", uptime_sec=round(time.time() - self._start_time))

    async def wait(self) -> None:
        """Block until shutdown signal"""
        try:
            while self._running:
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()

    # ─── User & Symbol Management ─────────────────────────────────────────

    async def add_user(self, user: UserConfig) -> None:
        """Register a new user with the engine"""
        await self.order_executor.register_user(user)
        self.notifier.register_user(user.user_id, user.telegram_chat_id)
        logger.info("user_added", user_id=user.user_id, exchange=user.exchange)

    async def add_symbol(self, exchange: str, symbol: str) -> None:
        """Subscribe to a new symbol's price feed"""
        feed = self._price_feeds.get(exchange)
        if not feed:
            feed = self._create_feed(exchange)
            self._price_feeds[exchange] = feed

        feed.subscribe([symbol])

        # Start feed if not already running
        if not feed._running:
            await feed.start()

    # ─── Price Feed Management ────────────────────────────────────────────

    async def _start_price_feeds(self, symbols: set[str]) -> None:
        """Start price feeds for all required exchanges/symbols"""
        # Group symbols by exchange (from position data)
        exchange_symbols: dict[str, list[str]] = {}
        for pos in self.position_manager.all_positions:
            ex = pos.exchange
            if ex not in exchange_symbols:
                exchange_symbols[ex] = []
            if pos.symbol not in exchange_symbols[ex]:
                exchange_symbols[ex].append(pos.symbol)

        for exchange, syms in exchange_symbols.items():
            feed = self._create_feed(exchange)
            feed.subscribe(syms)
            self._price_feeds[exchange] = feed
            await feed.start()
            logger.info("price_feed_started", exchange=exchange, symbols=len(syms))

    def _create_feed(self, exchange: str) -> PriceFeed:
        """Create a price feed for an exchange"""
        feed_cfg = self.config.get("price_feed", {})
        return PriceFeed(
            exchange_id=exchange,
            event_bus=self.event_bus,
            stale_threshold_sec=feed_cfg.get("stale_threshold_sec", 3.0),
            critical_threshold_sec=feed_cfg.get("critical_threshold_sec", 30.0),
        )

    # ─── Reconciliation Loop (Problem #4) ─────────────────────────────────

    async def _reconciliation_loop(self) -> None:
        """Periodically reconcile in-memory state with exchange"""
        interval = self.config.get("engine", {}).get("reconciliation_interval_sec", 30)

        while self._running:
            await asyncio.sleep(interval)

            try:
                # Reconcile each user's positions
                for user_id, config in self.order_executor._user_configs.items():
                    adapter = await self.order_executor.get_adapter(user_id)
                    if not adapter:
                        continue

                    ex_positions = await adapter.fetch_positions()
                    issues = self.position_manager.reconcile_with_exchange(ex_positions)

                    if issues:
                        await self.event_bus.emit(
                            event=__import__("core.types", fromlist=["Event"]).Event(
                                name="alert",
                                data={
                                    "user_id": user_id,
                                    "level": "warning",
                                    "message": f"⚠️ Position drift detected: {'; '.join(issues[:3])}",
                                },
                            )
                        )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("reconciliation_error", error=str(e))

    # ─── Config ───────────────────────────────────────────────────────────

    @staticmethod
    def _load_config(path: str) -> dict:
        """Load YAML config with env variable override support"""
        config_file = Path(path)
        if config_file.exists():
            with open(config_file) as f:
                config = yaml.safe_load(f) or {}
            logger.info("config_loaded", path=path)
            return config

        logger.warning("config_not_found", path=path, using="defaults")
        return {}

    # ─── Signal Handling ──────────────────────────────────────────────────

    def _register_signals(self) -> None:
        """Register SIGINT/SIGTERM for graceful shutdown"""
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, lambda: asyncio.create_task(self.stop()))
            except NotImplementedError:
                pass  # Windows doesn't support add_signal_handler

    # ─── Stats ────────────────────────────────────────────────────────────

    @property
    def stats(self) -> dict:
        return {
            "uptime_sec": round(time.time() - self._start_time) if self._start_time else 0,
            "event_bus": self.event_bus.stats,
            "position_manager": self.position_manager.stats,
            "order_queue": self.order_queue.stats,
            "state_store": self.state_store.stats,
            "notifier": self.notifier.stats,
            "price_feeds": {
                name: feed.stats for name, feed in self._price_feeds.items()
            },
        }

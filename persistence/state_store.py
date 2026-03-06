"""
Agentrade Engine v2 — State Persistence

Periodic snapshots of all position state to Redis (or JSON file fallback).
On crash recovery: load snapshot → reconcile with exchange → resume trailing.

Solves Problem #3: State Recovery on Crash
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# Redis is optional — falls back to file-based persistence
try:
    import redis.asyncio as aioredis
    HAS_REDIS = True
except ImportError:
    HAS_REDIS = False
    logger.warning("redis_not_available", fallback="file")


class StateStore:
    """
    Persists position state for crash recovery.

    Two backends:
    - Redis (preferred): fast, atomic, supports TTL
    - JSON file (fallback): works everywhere, no dependencies

    Snapshot frequency: every 5 seconds (configurable).
    On restart: load last snapshot, engine reconciles with exchange.
    """

    def __init__(
        self,
        redis_url: str | None = "redis://localhost:6379/0",
        fallback_path: str = "state/positions.json",
        snapshot_interval_sec: float = 5.0,
        key_prefix: str = "agentrade:",
    ) -> None:
        self.redis_url = redis_url
        self.fallback_path = Path(fallback_path)
        self.snapshot_interval = snapshot_interval_sec
        self.key_prefix = key_prefix

        self._redis: Any | None = None
        self._use_redis = HAS_REDIS and redis_url is not None
        self._running = False
        self._snapshot_count = 0

    async def connect(self) -> None:
        """Connect to Redis (or prepare file fallback)"""
        if self._use_redis:
            try:
                self._redis = aioredis.from_url(self.redis_url)
                await self._redis.ping()
                logger.info("state_store_connected", backend="redis", url=self.redis_url)
                return
            except Exception as e:
                logger.warning("redis_connect_failed", error=str(e), fallback="file")
                self._use_redis = False

        # File fallback
        self.fallback_path.parent.mkdir(parents=True, exist_ok=True)
        logger.info("state_store_connected", backend="file", path=str(self.fallback_path))

    async def save_snapshot(self, positions: list[dict]) -> None:
        """Save all positions to persistent storage"""
        self._snapshot_count += 1

        payload = {
            "timestamp": time.time(),
            "snapshot_id": self._snapshot_count,
            "positions": positions,
        }

        if self._use_redis and self._redis:
            try:
                key = f"{self.key_prefix}snapshot"
                await self._redis.set(key, json.dumps(payload))
                await self._redis.set(f"{self.key_prefix}snapshot_time", str(time.time()))
                return
            except Exception as e:
                logger.error("redis_save_failed", error=str(e))

        # File fallback
        try:
            self.fallback_path.write_text(json.dumps(payload, indent=2))
        except Exception as e:
            logger.error("file_save_failed", error=str(e))

    async def load_snapshot(self) -> list[dict]:
        """Load positions from last snapshot"""
        if self._use_redis and self._redis:
            try:
                key = f"{self.key_prefix}snapshot"
                data = await self._redis.get(key)
                if data:
                    payload = json.loads(data)
                    age = time.time() - payload.get("timestamp", 0)
                    logger.info(
                        "snapshot_loaded",
                        backend="redis",
                        positions=len(payload.get("positions", [])),
                        age_seconds=round(age),
                    )
                    return payload.get("positions", [])
            except Exception as e:
                logger.error("redis_load_failed", error=str(e))

        # File fallback
        try:
            if self.fallback_path.exists():
                data = json.loads(self.fallback_path.read_text())
                age = time.time() - data.get("timestamp", 0)
                logger.info(
                    "snapshot_loaded",
                    backend="file",
                    positions=len(data.get("positions", [])),
                    age_seconds=round(age),
                )
                return data.get("positions", [])
        except Exception as e:
            logger.error("file_load_failed", error=str(e))

        logger.info("no_snapshot_found")
        return []

    async def start_periodic_snapshots(self, get_positions_fn) -> None:
        """
        Start background task that snapshots every N seconds.
        get_positions_fn: callable that returns list[dict] of positions.
        """
        self._running = True

        async def _loop():
            while self._running:
                await asyncio.sleep(self.snapshot_interval)
                try:
                    positions = get_positions_fn()
                    if positions:
                        await self.save_snapshot(positions)
                except Exception as e:
                    logger.error("snapshot_loop_error", error=str(e))

        asyncio.create_task(_loop(), name="state_snapshots")
        logger.info("periodic_snapshots_started", interval_sec=self.snapshot_interval)

    async def stop(self) -> None:
        """Stop periodic snapshots and close connections"""
        self._running = False
        if self._redis:
            await self._redis.close()

    @property
    def stats(self) -> dict:
        return {
            "backend": "redis" if self._use_redis else "file",
            "snapshots_saved": self._snapshot_count,
        }

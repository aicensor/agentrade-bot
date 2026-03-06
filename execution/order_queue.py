"""
Agentrade Engine v2 — Order Execution Queue

The bridge between strategy decisions and exchange API calls.
Handles the three critical optimizations:
  A) Deduplication — only latest SL value per position (Problem #2B)
  B) Smart batching — flush every N ms, not per tick (Problem #2C)
  C) Concurrent execution — all users in parallel (Problem #2)
  D) Priority ordering — critical actions first (near-liquidation)
  E) Staggered execution — anti-thundering-herd (Problem #2A)
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any, Callable, Coroutine

import structlog

from core.event_bus import EventBus
from core.types import Action, ActionType, Event, Priority

logger = structlog.get_logger(__name__)

# Type for the execute callback
ExecuteCallback = Callable[[Action], Coroutine[Any, Any, bool]]


class OrderQueue:
    """
    Batching + deduplication + priority queue for order execution.

    Flow:
      1. Actions arrive from PositionManager (via event bus)
      2. Dedup: only keep latest action per position_key
      3. Every flush_interval_ms: sort by priority, execute batch
      4. Execution is concurrent (asyncio.gather) with staggered jitter

    Usage:
        queue = OrderQueue(event_bus, execute_fn, flush_interval_ms=500)
        await queue.start()
    """

    def __init__(
        self,
        event_bus: EventBus,
        execute_callback: ExecuteCallback,
        flush_interval_ms: int = 500,       # Flush every 500ms
        max_jitter_ms: int = 20,            # Anti-thundering-herd jitter
        max_batch_size: int = 200,          # Max actions per flush
        max_retries: int = 3,               # Retry failed actions
    ) -> None:
        self.event_bus = event_bus
        self.execute_callback = execute_callback
        self.flush_interval = flush_interval_ms / 1000.0
        self.max_jitter_ms = max_jitter_ms
        self.max_batch_size = max_batch_size
        self.max_retries = max_retries

        # Dedup buffer: only latest action per position_key
        # This is the key optimization — 4 ticks in 400ms = 1 API call, not 4
        self._pending: dict[str, Action] = {}

        # Retry queue
        self._retry: list[tuple[Action, int]] = []  # (action, attempt)

        # Stats
        self._total_queued = 0
        self._total_deduped = 0
        self._total_executed = 0
        self._total_failed = 0
        self._running = False

    async def start(self) -> None:
        """Start the queue — register handler + launch flush loop"""
        self._running = True
        self.event_bus.on("action", self._on_action)
        asyncio.create_task(self._flush_loop(), name="order_queue_flush")
        logger.info("order_queue_started", flush_interval_ms=self.flush_interval * 1000)

    async def stop(self) -> None:
        """Stop the queue — flush remaining actions"""
        self._running = False
        if self._pending:
            await self._flush()

    async def _on_action(self, event: Event) -> None:
        """
        Receive action from PositionManager.
        Dedup: overwrites previous action for same position_key.
        """
        action: Action = event.data
        self._total_queued += 1

        key = action.position_key

        # Dedup: keep only the latest action per position
        if key in self._pending:
            self._total_deduped += 1

        self._pending[key] = action

    async def _flush_loop(self) -> None:
        """Periodic flush — executes batched actions"""
        while self._running:
            await asyncio.sleep(self.flush_interval)
            if self._pending or self._retry:
                await self._flush()

    async def _flush(self) -> None:
        """
        Execute all pending actions.
        1. Take snapshot of pending (atomic swap)
        2. Sort by priority
        3. Execute concurrently with staggered jitter
        """
        # Atomic swap — grab all pending and clear
        batch = list(self._pending.values())
        self._pending.clear()

        # Add retry items
        retry_batch = [(a, attempt) for a, attempt in self._retry]
        self._retry.clear()

        # Merge and sort by priority (0=critical first)
        all_actions = [(a, 0) for a in batch] + retry_batch
        all_actions.sort(key=lambda x: x[0].priority.value)

        # Limit batch size
        if len(all_actions) > self.max_batch_size:
            # Keep highest priority, re-queue the rest
            overflow = all_actions[self.max_batch_size:]
            all_actions = all_actions[:self.max_batch_size]
            for action, attempt in overflow:
                self._pending[action.position_key] = action

        if not all_actions:
            return

        logger.debug(
            "flush_batch",
            count=len(all_actions),
            priorities={p.name: sum(1 for a, _ in all_actions if a.priority == p) for p in Priority},
        )

        # Execute concurrently with staggered jitter (Problem #2A)
        tasks = []
        for i, (action, attempt) in enumerate(all_actions):
            jitter = (i * 0.01) + random.uniform(0, self.max_jitter_ms / 1000)
            tasks.append(self._execute_with_jitter(action, attempt, jitter))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Handle results
        for (action, attempt), result in zip(all_actions, results):
            if isinstance(result, Exception):
                logger.error("execute_exception", action=action.reason, error=str(result))
                self._handle_failure(action, attempt)
            elif result is False:
                self._handle_failure(action, attempt)

    async def _execute_with_jitter(self, action: Action, attempt: int, jitter: float) -> bool:
        """Execute a single action with jitter delay"""
        if jitter > 0:
            await asyncio.sleep(jitter)

        try:
            success = await self.execute_callback(action)
            if success:
                self._total_executed += 1
                # Emit success event for logging/notification
                self.event_bus.emit_nowait(Event(name="order_executed", data=action))
            return success
        except Exception as e:
            logger.error("execute_error", reason=action.reason, error=str(e))
            raise

    def _handle_failure(self, action: Action, attempt: int) -> None:
        """Handle failed execution — retry or give up"""
        self._total_failed += 1
        attempt += 1

        if attempt < self.max_retries:
            self._retry.append((action, attempt))
            logger.warning(
                "action_retry_queued",
                position=action.position_key,
                attempt=attempt,
                max=self.max_retries,
            )
        else:
            logger.error(
                "action_permanently_failed",
                position=action.position_key,
                reason=action.reason,
            )
            # Emit alert for permanent failure
            self.event_bus.emit_nowait(Event(
                name="alert",
                data={
                    "user_id": action.user_id,
                    "level": "critical",
                    "message": (
                        f"❌ Failed to update SL for {action.symbol} after {self.max_retries} attempts. "
                        f"Check manually! Reason: {action.reason}"
                    ),
                },
            ))

    @property
    def stats(self) -> dict:
        return {
            "pending": len(self._pending),
            "retry_queue": len(self._retry),
            "total_queued": self._total_queued,
            "total_deduped": self._total_deduped,
            "total_executed": self._total_executed,
            "total_failed": self._total_failed,
            "dedup_rate": (
                round(self._total_deduped / max(self._total_queued, 1) * 100, 1)
            ),
        }

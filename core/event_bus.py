"""
Agentrade Engine v2 — Async Event Bus

Lightweight pub/sub system built on asyncio.
All communication between modules goes through here.

Events flow:
  PriceFeed → event_bus.emit("price_update") → PositionManager → Strategy → OrderQueue
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from typing import Any, Callable, Coroutine

import structlog

from core.types import Event

logger = structlog.get_logger(__name__)

# Type alias for event handlers
EventHandler = Callable[[Event], Coroutine[Any, Any, None]]


class EventBus:
    """
    Async event bus with named channels.

    Usage:
        bus = EventBus()
        bus.on("price_update", my_handler)
        await bus.emit(Event(name="price_update", data=price))

    Features:
    - Multiple handlers per event name
    - Handlers run concurrently via asyncio.gather
    - Error isolation: one handler failing doesn't kill others
    - Latency tracking for monitoring (Problem #8)
    """

    def __init__(self) -> None:
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)
        self._event_count: int = 0
        self._error_count: int = 0
        self._last_emit_time: dict[str, float] = {}

    def on(self, event_name: str, handler: EventHandler) -> None:
        """Register a handler for an event type"""
        self._handlers[event_name].append(handler)
        logger.info("handler_registered", event_name=event_name, handler_name=handler.__qualname__)

    def off(self, event_name: str, handler: EventHandler) -> None:
        """Unregister a handler"""
        if handler in self._handlers[event_name]:
            self._handlers[event_name].remove(handler)

    async def emit(self, event: Event) -> None:
        """
        Emit an event to all registered handlers.
        Handlers run concurrently. Errors are caught and logged, not propagated.
        """
        handlers = self._handlers.get(event.name, [])
        if not handlers:
            return

        self._event_count += 1
        self._last_emit_time[event.name] = time.time()

        # Run all handlers concurrently
        results = await asyncio.gather(
            *[self._safe_call(handler, event) for handler in handlers],
            return_exceptions=True,
        )

        # Log any exceptions (don't crash the bus)
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                self._error_count += 1
                logger.error(
                    "handler_error",
                    event=event.name,
                    handler=handlers[i].__qualname__,
                    error=str(result),
                    exc_info=result,
                )

    async def _safe_call(self, handler: EventHandler, event: Event) -> None:
        """Call handler with error isolation"""
        try:
            await handler(event)
        except Exception as e:
            raise e  # Re-raise so gather() captures it

    def emit_nowait(self, event: Event) -> None:
        """
        Fire-and-forget emit. Schedules emission without awaiting.
        Use for non-critical events (logging, metrics).
        """
        loop = asyncio.get_running_loop()
        loop.create_task(self.emit(event))

    @property
    def stats(self) -> dict:
        """Monitoring stats (Problem #8)"""
        return {
            "total_events": self._event_count,
            "total_errors": self._error_count,
            "handler_counts": {
                name: len(handlers) for name, handlers in self._handlers.items()
            },
            "last_emit_times": dict(self._last_emit_time),
        }

"""
Agentrade Engine v2 — Telegram Notifications

Sends alerts to users via Telegram.
Listens to "alert" and "order_executed" events from the EventBus.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from core.event_bus import EventBus
from core.types import Action, ActionType, Event

logger = structlog.get_logger(__name__)

# telegram.Bot is optional
try:
    from telegram import Bot
    HAS_TELEGRAM = True
except ImportError:
    HAS_TELEGRAM = False
    logger.warning("telegram_not_available")


class TelegramNotifier:
    """
    Sends Telegram messages on key events:
    - TP level hit (SL moved to breakeven, etc.)
    - Trailing SL updates (configurable verbosity)
    - Critical alerts (WS disconnect, SL update failures)
    - Position opened/closed summaries

    Rate limited: max 30 messages/second (Telegram limit).
    """

    def __init__(
        self,
        bot_token: str,
        event_bus: EventBus,
        default_chat_id: int = 0,
        verbose_trailing: bool = False,  # Send message on every SL update?
        allowed_user_ids: list[int] | None = None,  # Telegram user ID whitelist
    ) -> None:
        self.bot_token = bot_token
        self.event_bus = event_bus
        self.default_chat_id = default_chat_id
        self.verbose_trailing = verbose_trailing
        self.allowed_user_ids: set[int] = set(allowed_user_ids or [])

        self._bot: Any | None = None
        self._chat_ids: dict[str, int] = {}  # user_id → chat_id

        # Rate limiting (Telegram allows ~30 msg/sec)
        self._send_queue: asyncio.Queue = asyncio.Queue()
        self._messages_sent = 0
        self._running = False

    def is_user_allowed(self, user_id: int) -> bool:
        """Check if a Telegram user ID is in the whitelist"""
        if not self.allowed_user_ids:
            return True  # No whitelist = allow all
        return user_id in self.allowed_user_ids

    async def start(self) -> None:
        """Initialize bot and register event handlers"""
        self._running = True

        if HAS_TELEGRAM and self.bot_token:
            self._bot = Bot(token=self.bot_token)
            logger.info(
                "telegram_notifier_started",
                allowed_users=len(self.allowed_user_ids) or "all",
            )
        else:
            logger.warning("telegram_running_in_dry_mode")

        # Register event handlers
        self.event_bus.on("alert", self._on_alert)
        self.event_bus.on("order_executed", self._on_order_executed)

        # Start send queue processor
        asyncio.create_task(self._send_loop(), name="telegram_sender")

    async def stop(self) -> None:
        self._running = False

    def register_user(self, user_id: str, chat_id: int) -> None:
        """Map user_id to Telegram chat_id"""
        self._chat_ids[user_id] = chat_id

    # ─── Event Handlers ───────────────────────────────────────────────────

    async def _on_alert(self, event: Event) -> None:
        """Handle alert events — always send"""
        data = event.data
        if isinstance(data, dict):
            user_id = data.get("user_id", "")
            message = data.get("message", "")
            level = data.get("level", "info")

            if user_id:
                await self._queue_message(user_id, message)
            elif self.default_chat_id:
                # System-wide alert → send to admin
                await self._queue_message_direct(self.default_chat_id, f"[{level.upper()}] {message}")

    async def _on_order_executed(self, event: Event) -> None:
        """Handle executed order events — format and send"""
        action: Action = event.data

        # Skip verbose trailing updates unless configured
        if action.type == ActionType.MOVE_SL and not self.verbose_trailing:
            # Only send for TP-level moves, not regular trailing updates
            if "TP" not in action.reason and "breakeven" not in action.reason:
                return

        message = self._format_action(action)
        if message:
            await self._queue_message(action.user_id, message)

    # ─── Message Formatting ───────────────────────────────────────────────

    def _format_action(self, action: Action) -> str:
        """Format an action into a user-friendly Telegram message"""
        symbol = action.symbol.replace("/USDT:USDT", "").replace("/USDT", "")

        if action.type == ActionType.MOVE_SL:
            return f"🔄 {symbol} | SL → ${action.price:.4f}\n📝 {action.reason}"

        elif action.type == ActionType.PARTIAL_CLOSE:
            return (
                f"✅ {symbol} | Partial close {action.close_pct*100:.0f}%\n"
                f"🔄 SL → ${action.price:.4f}\n"
                f"📝 {action.reason}"
            )

        elif action.type == ActionType.FULL_CLOSE:
            return f"🏁 {symbol} | Position CLOSED\n📝 {action.reason}"

        elif action.type == ActionType.ALERT:
            return action.message

        return ""

    # ─── Send Queue (Rate Limited) ────────────────────────────────────────

    async def _queue_message(self, user_id: str, text: str) -> None:
        """Queue a message for a user"""
        chat_id = self._chat_ids.get(user_id, self.default_chat_id)
        if chat_id:
            await self._send_queue.put((chat_id, text))

    async def _queue_message_direct(self, chat_id: int, text: str) -> None:
        """Queue a message to a specific chat_id"""
        await self._send_queue.put((chat_id, text))

    async def _send_loop(self) -> None:
        """Process send queue — rate limited to ~25 msg/sec"""
        while self._running:
            try:
                chat_id, text = await asyncio.wait_for(
                    self._send_queue.get(),
                    timeout=5.0,
                )
                await self._send(chat_id, text)
                self._messages_sent += 1

                # Rate limit: ~25 msg/sec (Telegram limit is 30)
                await asyncio.sleep(0.04)

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("send_loop_error", error=str(e))
                await asyncio.sleep(1.0)

    async def _send(self, chat_id: int, text: str) -> None:
        """Actually send a Telegram message"""
        if self._bot:
            try:
                await self._bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.error("telegram_send_failed", chat_id=chat_id, error=str(e))
        else:
            # Dry mode — log instead
            logger.info("telegram_dry", chat_id=chat_id, text=text[:100])

    @property
    def stats(self) -> dict:
        return {
            "messages_sent": self._messages_sent,
            "queue_size": self._send_queue.qsize(),
            "registered_users": len(self._chat_ids),
        }

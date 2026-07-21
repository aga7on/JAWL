"""Opt-in Telegram delivery for passive coding approval events."""

from __future__ import annotations

from typing import Any, Dict, Literal

from src.l2_interfaces.host.os.coding_approvals import (
    format_coding_approval_notification,
)
from src.utils._tools import parse_int_or_str
from src.utils.event.bus import EventBus
from src.utils.event.registry import Events
from src.utils.logger import main_logger


class TelegramCodingApprovalNotifications:
    """Lifecycle consumer that pushes redacted approval metadata to one chat."""

    def __init__(
        self,
        event_bus: EventBus,
        client: Any,
        chat_id: int | str,
        transport: Literal["telethon", "aiogram"],
    ) -> None:
        self.bus = event_bus
        self.client = client
        self.chat_id = chat_id
        self.transport = transport
        self._started = False

    async def _on_requested(self, approval: Dict[str, Any], **_: Any) -> None:
        if not isinstance(approval, dict):
            return
        message = format_coding_approval_notification(approval)
        try:
            if self.transport == "telethon":
                await self.client.client().send_message(
                    parse_int_or_str(self.chat_id), message
                )
            else:
                await self.client.bot().send_message(
                    chat_id=parse_int_or_str(self.chat_id), text=message
                )
        except Exception as exc:
            main_logger.warning(
                f"[Telegram {self.transport}] Coding approval push failed: "
                f"{type(exc).__name__}."
            )

    async def start(self) -> None:
        if self._started:
            return
        self.bus.subscribe(
            Events.CODING_APPROVAL_REQUESTED, self._on_requested
        )
        self._started = True
        main_logger.info(
            f"[Telegram {self.transport}] Coding approval pushes enabled."
        )

    async def stop(self) -> None:
        if not self._started:
            return
        self.bus.unsubscribe(
            Events.CODING_APPROVAL_REQUESTED, self._on_requested
        )
        self._started = False


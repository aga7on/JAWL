"""Opt-in Telegram delivery for passive coding approval events."""

from __future__ import annotations

import asyncio
import re
from typing import Any, Callable, Dict, Literal

from src.l2_interfaces.host.os.coding_approvals import (
    CodingApprovalStore,
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
        remote_decisions: bool = False,
    ) -> None:
        self.bus = event_bus
        self.client = client
        self.chat_id = chat_id
        self.transport = transport
        self.remote_decisions = remote_decisions
        self._started = False

    async def _on_requested(self, approval: Dict[str, Any], **_: Any) -> None:
        if not isinstance(approval, dict):
            return
        message = format_coding_approval_notification(approval)
        if self.remote_decisions:
            approval_id = str(approval.get("id") or "")[:16]
            message += (
                f"\nRemote approve: /jawl_approve {approval_id}"
                f"\nRemote deny: /jawl_deny {approval_id}"
            )
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


class TelegramCodingApprovalControl:
    """Consume exact authenticated approval commands before agent event routing."""

    _COMMAND = re.compile(
        r"/jawl_(approve|deny)[ \t]+([0-9a-fA-F]{16})"
    )

    def __init__(
        self,
        event_bus: EventBus,
        client: Any,
        approval_store: Callable[[], CodingApprovalStore | None],
        chat_id: int,
        actor_id: int,
        transport: Literal["telethon", "aiogram"],
    ) -> None:
        self.bus = event_bus
        self.client = client
        self.approval_store = approval_store
        self.chat_id = int(chat_id)
        self.actor_id = int(actor_id)
        self.transport = transport

    async def _send(self, chat_id: int, text: str) -> None:
        if self.transport == "telethon":
            await self.client.client().send_message(chat_id, text)
        else:
            await self.client.bot().send_message(chat_id=chat_id, text=text)

    async def _send_best_effort(self, chat_id: int, message: str) -> None:
        try:
            await self._send(chat_id, message)
        except Exception as exc:
            main_logger.warning(
                f"[Telegram {self.transport}] Approval acknowledgement failed: "
                f"{type(exc).__name__}."
            )

    async def handle_message(
        self,
        *,
        raw_text: str,
        chat_id: Any,
        sender_id: Any,
        message_id: Any,
    ) -> bool:
        """Return True only for a syntactically exact control command."""

        match = self._COMMAND.fullmatch(str(raw_text or ""))
        if match is None:
            return False
        if chat_id != self.chat_id:
            main_logger.warning(
                f"[Telegram {self.transport}] Ignored approval command from "
                "an untrusted chat."
            )
            return True
        if sender_id != self.actor_id:
            main_logger.warning(
                f"[Telegram {self.transport}] Rejected approval command from "
                "an untrusted actor."
            )
            await self._send_best_effort(
                self.chat_id, "JAWL approval command rejected."
            )
            return True
        store = self.approval_store()
        if store is None:
            await self._send_best_effort(
                self.chat_id, "JAWL coding approval store is unavailable."
            )
            return True
        decision, approval_id = match.groups()
        actor = (
            f"telegram:{self.transport}:chat:{self.chat_id}:"
            f"actor:{self.actor_id}:message:{message_id}"
        )
        try:
            record = await asyncio.to_thread(
                store.decide,
                approval_id.lower(),
                decision == "approve",
                actor,
            )
        except (OSError, PermissionError, ValueError) as exc:
            await self._send_best_effort(
                self.chat_id,
                "JAWL approval decision rejected: " + str(exc)[:500],
            )
            return True
        await self.bus.publish(
            Events.CODING_APPROVAL_DECIDED,
            approval=dict(record),
            transport=self.transport,
        )
        await self._send_best_effort(
            self.chat_id,
            f"JAWL approval {record['id']} is now {record['status']}.",
        )
        return True

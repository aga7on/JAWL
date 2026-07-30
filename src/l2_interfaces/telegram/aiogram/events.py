"""
Background Aiogram events listener (Dispatcher).

Uses Long Polling to intercept messages and system chat events.
Analyzes incoming data, updates L0 State, and publishes events to EventBus
to wake up the agent core.
"""

import asyncio
import uuid
from pathlib import Path
from typing import Optional

from aiogram import Dispatcher, F
from aiogram.types import Message

from src.utils.event.bus import EventBus
from src.utils.event.registry import Events
from src.utils.logger import main_logger
from src.instances.paths import get_instance_paths
from src.utils._tools import get_project_root
from src.utils.settings import AiogramConfig

from src.l2_interfaces.telegram.aiogram.state import AiogramState
from src.l2_interfaces.telegram.aiogram.client import AiogramClient
from src.l2_interfaces.telegram.coding_approval_notifications import (
    TelegramCodingApprovalControl,
)


class AiogramEvents:
    """
    Background Aiogram poller.
    Orchestrates routers, handlers, and EventBus communication.
    """

    def __init__(
        self,
        aiogram_client: AiogramClient,
        state: AiogramState,
        event_bus: EventBus,
        config: AiogramConfig,
        approval_control: Optional[TelegramCodingApprovalControl] = None,
    ) -> None:
        """
        Initializes the events listener.

        Args:
            aiogram_client (AiogramClient): Initialized bot client.
            state (AiogramState): L0 state to update active chats list.
            event_bus (EventBus): Global event bus.
        """
        self.client = aiogram_client
        self.state = state
        self.bus = event_bus
        self.config = config
        self.approval_control = approval_control

        self.dp = Dispatcher()
        self._polling_task: Optional[asyncio.Task] = None

    async def _download_visual_media(self, message: Message) -> list[str]:
        """Download one supported Bot API image/video into the sandbox."""

        if not self.config.download_visual_media:
            return []
        attachment = None
        mime_type = ""
        suffix = ""
        if message.photo:
            attachment = message.photo[-1]
            mime_type, suffix = "image/jpeg", ".jpg"
        elif message.video:
            attachment = message.video
            mime_type = message.video.mime_type or "video/mp4"
            suffix = Path(message.video.file_name or "").suffix or ".mp4"
        elif message.document:
            mime_type = message.document.mime_type or ""
            suffix_by_mime = {
                "image/jpeg": ".jpg",
                "image/png": ".png",
                "image/webp": ".webp",
                "image/gif": ".gif",
                "video/mp4": ".mp4",
                "video/webm": ".webm",
                "video/quicktime": ".mov",
                "video/x-matroska": ".mkv",
            }
            suffix = suffix_by_mime.get(mime_type.lower(), "")
            if suffix:
                attachment = message.document
        if attachment is None or not suffix:
            return []
        max_bytes = int(self.config.visual_media_max_mb) * 1024 * 1024
        if int(getattr(attachment, "file_size", 0) or 0) > max_bytes:
            main_logger.warning("[Aiogram] Skipped oversized visual media.")
            return []
        instance_paths = get_instance_paths()
        media_dir = (
            get_project_root() / "sandbox" / "telegram_media"
            if instance_paths.legacy_default
            else instance_paths.telegram_media_dir
        )
        media_dir.mkdir(parents=True, exist_ok=True)
        target = media_dir / (
            f"bot_{message.message_id}_{uuid.uuid4().hex[:8]}{suffix.lower()}"
        )
        try:
            bot = self.client.bot()
            remote_file = await bot.get_file(attachment.file_id)
            await bot.download_file(remote_file.file_path, destination=target)
            if not target.is_file() or target.stat().st_size > max_bytes:
                target.unlink(missing_ok=True)
                return []
            main_logger.info(
                f"[Aiogram] Visual media downloaded: {target.name} ({mime_type})"
            )
            return [str(target.resolve())]
        except Exception as exc:
            target.unlink(missing_ok=True)
            main_logger.warning(f"[Aiogram] Failed to download visual media: {exc}")
            return []

    async def start(self) -> None:
        """
        Registers message handlers, resets old Webhook/Updates,
        and starts Long Polling as a background task.
        """
        if self._polling_task:
            return

        bot = self.client.bot()

        # Register handlers with chat type filters
        self.dp.message.register(self._on_private_message, F.chat.type == "private")
        self.dp.message.register(
            self._on_group_message, F.chat.type.in_({"group", "supergroup"})
        )
        self.dp.message.register(
            self._on_system_message,
            F.content_type.in_(
                {
                    "new_chat_members",
                    "left_chat_member",
                    "new_chat_title",
                    "new_chat_photo",
                    "delete_chat_photo",
                    "pinned_message",
                }
            ),
        )

        # Drop pending updates accumulated during offline
        await bot.delete_webhook(drop_pending_updates=True)

        self._polling_task = asyncio.create_task(self.dp.start_polling(bot))
        main_logger.info("[Telegram Aiogram] Background polling started.")

    async def stop(self) -> None:
        """Stops polling and correctly closes the Dispatcher."""
        if self._polling_task:
            self._polling_task.cancel()
            self._polling_task = None

        try:
            await self.dp.stop_polling()
        except RuntimeError:
            pass  # Ignore "Polling is not started" error

        main_logger.info("[Telegram Aiogram] Background polling stopped.")

    async def _update_state(self, message: Message) -> None:
        """
        Saves chat to cache and formats string for the dashboard.
        Works on the MRU (Most Recently Used) principle, evicting older dialogues.

        Args:
            message (Message): Incoming message from Aiogram.
        """
        chat_type = "User" if message.chat.type == "private" else "Group"
        chat_name = message.chat.title or message.chat.full_name or message.from_user.full_name

        chat_str = f"{chat_type} | ID: {message.chat.id} | Name: {chat_name}"

        # Remove the key so that upon insertion it is at the end of the dictionary (freshest)
        self.state._chats_cache.pop(message.chat.id, None)
        self.state._chats_cache[message.chat.id] = chat_str

        # Evict old chats if limit is exceeded
        if len(self.state._chats_cache) > self.state.number_of_last_chats:
            first_key = next(iter(self.state._chats_cache))
            del self.state._chats_cache[first_key]
            self.state.recent_messages.pop(first_key, None)

        # Reverse the list so that new (latest) ones are at the top
        lines = list(self.state._chats_cache.values())[::-1]
        self.state.last_chats = "\n".join(lines)

        # Интегрируем сохранение текста сообщений в кэш
        if message.chat.id not in self.state.recent_messages:
            self.state.recent_messages[message.chat.id] = []

        sender_name = message.from_user.first_name if message.from_user else "Unknown"
        msg_text = message.text or message.caption or "[Media]"
        self.state.recent_messages[message.chat.id].append(f"[{sender_name}]: {msg_text}")

        if len(self.state.recent_messages[message.chat.id]) > self.state.recent_messages_limit:
            self.state.recent_messages[message.chat.id].pop(0)

    async def _on_private_message(self, message: Message) -> None:
        """Trigger on incoming private messages (DMs) to the bot."""
        if self.approval_control is not None and await self.approval_control.handle_message(
            raw_text=message.text or message.caption or "",
            chat_id=message.chat.id,
            sender_id=message.from_user.id if message.from_user else None,
            message_id=message.message_id,
        ):
            return
        await self._update_state(message)

        sender_name = message.from_user.first_name if message.from_user else "Unknown"

        payload = dict(
            message=message.text or message.caption or "[Media]",
            raw_text=message.text or message.caption or "",
            sender_name=sender_name,
            chat_id=message.chat.id,
            sender_id=message.from_user.id if message.from_user else None,
            msg_id=message.message_id,
        )
        media_paths = await self._download_visual_media(message)
        if media_paths:
            payload["media_paths"] = media_paths
        await self.bus.publish(Events.AIOGRAM_MESSAGE_INCOMING, **payload)

    async def _on_group_message(self, message: Message) -> None:
        """Trigger on messages in groups/supergroups."""
        if self.approval_control is not None and await self.approval_control.handle_message(
            raw_text=message.text or message.caption or "",
            chat_id=message.chat.id,
            sender_id=message.from_user.id if message.from_user else None,
            message_id=message.message_id,
        ):
            return
        await self._update_state(message)

        bot = self.client.bot()
        me = await bot.get_me()

        # Check if the bot was mentioned: via @username or via reply
        is_mentioned = False
        if message.text and me.username in message.text:
            is_mentioned = True
        elif message.reply_to_message and message.reply_to_message.from_user.id == me.id:
            is_mentioned = True

        event_type = (
            Events.AIOGRAM_GROUP_MENTION if is_mentioned else Events.AIOGRAM_GROUP_MESSAGE
        )
        sender_name = message.from_user.first_name if message.from_user else "Unknown"

        payload = dict(
            message=message.text or message.caption or "[Media]",
            raw_text=message.text or message.caption or "",
            sender_name=sender_name,
            chat_id=message.chat.id,
            sender_id=message.from_user.id if message.from_user else None,
            msg_id=message.message_id,
        )
        media_paths = await self._download_visual_media(message)
        if media_paths:
            payload["media_paths"] = media_paths
        await self.bus.publish(event_type, **payload)

    async def _on_system_message(self, message: Message) -> None:
        """Trigger on system service events (join, leave, title change)."""
        await self._update_state(message)

        action_text = "[System action]"

        if message.new_chat_members:
            users = ", ".join([u.first_name for u in message.new_chat_members if u.first_name])
            action_text = f"[System action] {users} joined the chat."

        elif message.left_chat_member:
            action_text = (
                f"[System action] {message.left_chat_member.first_name} left the chat."
            )

        elif message.new_chat_title:
            action_text = f"[System action] Chat title changed to '{message.new_chat_title}'."

        elif message.pinned_message:
            action_text = "[System action] New message pinned."

        elif message.new_chat_photo or message.delete_chat_photo:
            action_text = "[System action] Chat photo was changed/deleted."

        payload = {
            "message": action_text,
            "sender_name": "System",
            "chat_id": message.chat.id,
        }

        await self.bus.publish(Events.AIOGRAM_CHAT_ACTION, **payload)

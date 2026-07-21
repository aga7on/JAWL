"""
Aiogram plugin for Telegram Bot API.
"""

from typing import List, Any, Dict, Optional
from src.utils.logger import main_logger
from src.l2_interfaces.base import BaseInterface
from src.l2_interfaces.telegram.aiogram.state import AiogramState
from src.l2_interfaces.telegram.aiogram.client import AiogramClient
from src.l2_interfaces.telegram.aiogram.events import AiogramEvents
from src.l2_interfaces.telegram.coding_approval_notifications import (
    TelegramCodingApprovalControl,
    TelegramCodingApprovalNotifications,
)
from src.l2_interfaces.telegram.aiogram.skills.chats import AiogramChats
from src.l2_interfaces.telegram.aiogram.skills.messages import AiogramMessages
from src.l2_interfaces.telegram.aiogram.skills.moderation import AiogramModeration
from src.l3_agent.skills.registry import register_instance
from src.l3_agent.context.registry import ContextSection
from src.system.container import SystemContainer
from src.utils.settings import InterfacesConfig


class AiogramPlugin(BaseInterface):
    @property
    def name(self) -> str:
        return "AIOGRAM"

    @property
    def description(self) -> str:
        return "Telegram Bot API. Connects to the registered bot account."

    def is_enabled(self, config: InterfacesConfig) -> bool:
        return config.telegram.aiogram.enabled

    def setup(
        self, container: SystemContainer, env_vars: Dict[str, Optional[str]]
    ) -> List[Any]:
        bot_token = env_vars.get("AIOGRAM_BOT_TOKEN")
        if not bot_token:
            main_logger.error("[Aiogram] BOT_TOKEN not found. Interface disabled.")
            self.register_off_provider(container.context_registry)
            return []

        config = container.interfaces_config.telegram.aiogram
        state = AiogramState(number_of_last_chats=config.recent_chats_limit)
        container.l0_states["aiogram"] = state

        client = AiogramClient(
            bot_token=bot_token, state=state, proxy_url=env_vars.get("PROXY_URL")
        )
        approval_control = (
            TelegramCodingApprovalControl(
                container.event_bus,
                client,
                lambda: container.coding_approvals,
                config.coding_approval_chat_id,
                config.coding_approval_actor_id,
                "aiogram",
            )
            if config.coding_approval_remote_decisions
            else None
        )
        events = AiogramEvents(
            aiogram_client=client,
            state=state,
            event_bus=container.event_bus,
            approval_control=approval_control,
        )
        approval_notifications = (
            TelegramCodingApprovalNotifications(
                container.event_bus,
                client,
                config.coding_approval_chat_id,
                "aiogram",
                remote_decisions=config.coding_approval_remote_decisions,
            )
            if config.coding_approval_chat_id is not None
            else None
        )

        register_instance(AiogramChats(client, state))
        register_instance(AiogramMessages(client))
        register_instance(AiogramModeration(client))

        container.context_registry.register_provider(
            "aiogram", client.get_context_block, ContextSection.INTERFACES
        )
        main_logger.info("[Aiogram] Interface loaded (Plugin).")
        lifecycle = [client, events]
        if approval_notifications is not None:
            lifecycle.append(approval_notifications)
        return lifecycle

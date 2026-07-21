from unittest.mock import MagicMock, patch

from src.l2_interfaces.telegram.aiogram.plugin import AiogramPlugin
from src.l2_interfaces.telegram.telethon.plugin import TelethonPlugin
from src.l3_agent.context.registry import ContextRegistry
from src.system.container import SystemContainer
from src.utils.event.bus import EventBus
from src.utils.settings import InterfacesConfig, SettingsConfig


def container_with_remote_transport(transport):
    interfaces = InterfacesConfig(
        telegram={
            transport: {
                "enabled": True,
                "coding_approval_chat_id": -100123,
                "coding_approval_remote_decisions": True,
                "coding_approval_actor_id": 456,
            }
        }
    )
    container = SystemContainer(SettingsConfig(), interfaces, EventBus())
    container.context_registry = MagicMock(spec=ContextRegistry)
    return container


def assert_remote_components_are_lazily_store_bound(lifecycle, container):
    assert lifecycle[1].__class__.__name__ in {"TelethonEvents", "AiogramEvents"}
    assert lifecycle[2].__class__.__name__ == "TelegramCodingApprovalNotifications"
    events = lifecycle[1]
    notifier = lifecycle[2]
    assert events.approval_control.chat_id == -100123
    assert events.approval_control.actor_id == 456
    assert notifier.remote_decisions is True
    assert events.approval_control.approval_store() is None

    marker = object()
    container.coding_approvals = marker
    assert events.approval_control.approval_store() is marker


def test_telethon_plugin_wires_remote_approval_control():
    container = container_with_remote_transport("telethon")
    with (
        patch(
            "src.l2_interfaces.telegram.telethon.plugin.TelethonClient",
            return_value=MagicMock(),
        ),
        patch("src.l2_interfaces.telegram.telethon.plugin.register_instance"),
    ):
        lifecycle = TelethonPlugin().setup(
            container,
            env_vars={"TELETHON_API_ID": "123", "TELETHON_API_HASH": "hash"},
        )

    assert_remote_components_are_lazily_store_bound(lifecycle, container)


def test_aiogram_plugin_wires_remote_approval_control():
    container = container_with_remote_transport("aiogram")
    with (
        patch(
            "src.l2_interfaces.telegram.aiogram.plugin.AiogramClient",
            return_value=MagicMock(),
        ),
        patch("src.l2_interfaces.telegram.aiogram.plugin.register_instance"),
    ):
        lifecycle = AiogramPlugin().setup(
            container,
            env_vars={"AIOGRAM_BOT_TOKEN": "token"},
        )

    assert_remote_components_are_lazily_store_bound(lifecycle, container)

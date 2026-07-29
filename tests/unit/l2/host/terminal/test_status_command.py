from unittest.mock import AsyncMock

import pytest

from src.l2_interfaces.host.terminal.events import HostTerminalEvents


@pytest.mark.asyncio
async def test_status_slash_command_does_not_publish_chat_event(
    terminal_client, mock_bus
):
    terminal_client.control_handler = AsyncMock(
        return_value={
            "agent": {
                "state": "thinking",
                "step": 4,
                "max_steps": 15,
                "model": "qwen3.8-max-preview",
            },
            "goal": {
                "status": "active",
                "last_cycle_status": "running",
                "task_ledger": {
                    "current_phase": "debug",
                    "next_action": "capture stack",
                },
            },
            "heartbeat": {"active_cycle": True},
        }
    )
    terminal_client.broadcast_message = AsyncMock()
    events = HostTerminalEvents(terminal_client, mock_bus)

    handled = await events._handle_local_command("/status")

    assert handled is True
    terminal_client.control_handler.assert_awaited_once_with("status.get", {})
    terminal_client.broadcast_message.assert_awaited_once()
    assert "did not interrupt" in terminal_client.broadcast_message.await_args.args[0]
    mock_bus.publish.assert_not_called()

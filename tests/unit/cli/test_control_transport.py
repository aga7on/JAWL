import asyncio

import pytest

from src.cli.control_client import request_control_async
from src.l2_interfaces.host.terminal.client import HostTerminalClient
from src.l2_interfaces.host.terminal.state import HostTerminalState
from src.utils.settings import HostTerminalConfig


@pytest.mark.asyncio
async def test_control_transport_is_separate_from_chat_events(tmp_path):
    calls = []

    async def handler(action, params):
        calls.append((action, params))
        return {"state": "ok"}

    client = HostTerminalClient(
        state=HostTerminalState(context_limit=10),
        config=HostTerminalConfig(),
        data_dir=tmp_path,
        agent_name="Test",
        timezone=0,
        control_handler=handler,
    )
    await client.start()
    try:
        result = await request_control_async(
            "status.get",
            {"detail": True},
            port_file=client.port_file,
        )
    finally:
        await client.stop()

    assert result == {"state": "ok"}
    assert calls == [("status.get", {"detail": True})]
    assert client.incoming_queue.empty()

"""Live stdio MCP facade and lifecycle ownership verification."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import psutil
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


pytestmark = pytest.mark.skipif(
    os.environ.get("JAWL_DEBUG_BROKER_MCP_LIVE") != "1",
    reason="set JAWL_DEBUG_BROKER_MCP_LIVE=1 to spawn the stdio broker",
)

TARGET = Path(r"G:\RE\Workspaces\live-tests\dotnet\jawl_live_target.exe")


@pytest.mark.asyncio
async def test_external_mcp_discovers_dbgeng_and_cleans_owned_target() -> None:
    assert TARGET.is_file()
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "src.l2_interfaces.debug_broker.mcp_server"],
        cwd=str(Path.cwd()),
        env={**os.environ, "PYTHONPATH": str(Path.cwd())},
    )
    owned_pid: int | None = None

    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert {item.name for item in tools.tools} == {
                "list_providers",
                "search_operations",
                "start_session",
                "call_operation",
                "wait_session",
                "session_snapshot",
                "stop_session",
            }

            discovered = await session.call_tool(
                "search_operations",
                {"query": "engine", "provider": "windbg", "limit": 10},
            )
            assert not discovered.isError
            operation = discovered.structuredContent["operations"][0]
            assert operation["operation"] == "dbgeng_status"
            engine = await session.call_tool(
                "call_operation",
                {
                    "provider": "windbg",
                    "operation": "dbgeng_status",
                    "arguments": {},
                    "expected_schema_sha256": operation["schema_sha256"],
                },
            )
            assert not engine.isError
            assert engine.structuredContent["result"]["all_components_installed"]

            started = await session.call_tool(
                "start_session",
                {
                    "provider": "frida",
                    "target": str(TARGET),
                    "options": {"arguments": ["loop"]},
                },
            )
            assert not started.isError
            owned_pid = int(
                started.structuredContent["metadata"]["provider"]["pid"]
            )
            assert psutil.pid_exists(owned_pid)
            # Deliberately omit stop_session: the MCP lifespan must own cleanup.

    assert owned_pid is not None
    for _ in range(40):
        if not psutil.pid_exists(owned_pid):
            break
        await asyncio.sleep(0.1)
    assert not psutil.pid_exists(owned_pid)

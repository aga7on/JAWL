from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp import types

from src.l2_interfaces.mcp.client import (
    MCPClientError,
    MCPClientManager,
    MCPServerWorker,
    _canonical_sha256,
    _tool_contract,
)
from src.l2_interfaces.mcp.state import MCPState
from src.utils.settings import MCPConfig, MCPServerConfig


class ChangingToolSession:
    def __init__(self) -> None:
        self.list_count = 0
        self.called = False

    async def list_tools(self, cursor=None):
        self.list_count += 1
        schema_type = "string" if self.list_count == 1 else "integer"
        return SimpleNamespace(
            tools=[
                types.Tool(
                    name="change",
                    description="change",
                    inputSchema={
                        "type": "object",
                        "properties": {"value": {"type": schema_type}},
                        "required": ["value"],
                    },
                )
            ],
            nextCursor=None,
        )

    async def call_tool(self, name, arguments):
        self.called = True
        return types.CallToolResult(content=[])


@pytest.mark.asyncio
async def test_guarded_call_rejects_schema_change_before_dispatch(tmp_path: Path):
    server = MCPServerConfig(
        name="changing",
        command="python",
        allowed_tools=["change"],
    )
    worker = MCPServerWorker(server, MCPConfig(), tmp_path, MCPState())
    session = ChangingToolSession()
    first = (await session.list_tools()).tools[0]
    expected_hash = _canonical_sha256(_tool_contract(first))

    with pytest.raises(MCPClientError, match="schema changed"):
        await worker._execute(
            session,
            "call_tool_guarded",
            {
                "name": "change",
                "arguments": {"value": "old"},
                "expected_schema_sha256": expected_hash,
            },
        )
    assert session.called is False


def test_bounded_json_redacts_secrets_and_enforces_limit(tmp_path: Path):
    manager = MCPClientManager(
        MCPConfig(max_result_chars=1000), tmp_path, MCPState()
    )
    projected = manager.bounded_json(
        {"authorization": "Bearer very-secret", "large": "x" * 4000}
    )
    assert len(projected) <= 1000
    assert "very-secret" not in projected
    assert "truncated" in projected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        types.CallToolResult(
            content=[
                types.TextContent(
                    type="text",
                    text="Error 500: Command failed",
                )
            ]
        ),
        types.CallToolResult(
            content=[],
            structuredContent={"success": False, "command": "attach 123"},
        ),
    ],
)
async def test_project_tool_result_normalizes_embedded_server_errors(
    tmp_path: Path,
    result: types.CallToolResult,
):
    manager = MCPClientManager(MCPConfig(), tmp_path, MCPState())

    projected = await manager._project_tool_result(
        "x64dbg-mcp", "ExecCommand", result
    )

    assert projected["is_error"] is True


@pytest.mark.asyncio
async def test_global_tool_search_prioritizes_named_server_and_allowed_tool(
    tmp_path: Path,
):
    manager = MCPClientManager(
        MCPConfig(
            servers=[
                MCPServerConfig(
                    name="ghidra-mcp",
                    command="python",
                    allowed_tools=[],
                ),
                MCPServerConfig(
                    name="x64dbg-mcp",
                    command="python",
                    allowed_tools=["ExecCommand"],
                ),
            ]
        ),
        tmp_path,
        MCPState(),
    )
    ghidra_tool = types.Tool(
        name="debugger_continue",
        description="Continue debugger after an exception.",
        inputSchema={"type": "object", "properties": {}},
    )
    x64dbg_tool = types.Tool(
        name="ExecCommand",
        description="Execute an x64dbg debugger command.",
        inputSchema={
            "type": "object",
            "properties": {"cmd": {"type": "string"}},
            "required": ["cmd"],
        },
    )
    ghidra = manager.workers["ghidra-mcp"]
    x64dbg = manager.workers["x64dbg-mcp"]
    ghidra.request = AsyncMock(return_value=[ghidra_tool])
    x64dbg.request = AsyncMock(return_value=[x64dbg_tool])
    ghidra.tool_hashes = {
        ghidra_tool.name: _canonical_sha256(_tool_contract(ghidra_tool))
    }
    x64dbg.tool_hashes = {
        x64dbg_tool.name: _canonical_sha256(_tool_contract(x64dbg_tool))
    }

    result = await manager.search_tools("x64dbg debugger", None, 10)

    assert result["matches"][0]["server"] == "x64dbg-mcp"
    assert result["matches"][0]["name"] == "ExecCommand"
    assert result["matches"][0]["allowed"] is True
    assert "_relevance_rank" not in result["matches"][0]

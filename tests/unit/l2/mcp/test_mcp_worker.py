from pathlib import Path
from types import SimpleNamespace

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

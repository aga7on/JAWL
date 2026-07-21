import json
import sys
from pathlib import Path

import pytest

from src.l2_interfaces.mcp.client import MCPClientError, MCPClientManager
from src.l2_interfaces.mcp.state import MCPState
from src.utils.settings import MCPConfig


def _manager(root_dir: Path) -> MCPClientManager:
    config = MCPConfig.model_validate(
        {
            "enabled": True,
            "startup_timeout_sec": 15,
            "request_timeout_sec": 15,
            "servers": [
                {
                    "name": "fixture",
                    "transport": "stdio",
                    "command": sys.executable,
                    "args": ["tests/fixtures/mcp_test_server.py"],
                    "cwd": ".",
                    "allowed_tools": ["echo"],
                    "resources_enabled": True,
                    "prompts_enabled": True,
                }
            ],
        }
    )
    return MCPClientManager(config, root_dir, MCPState())


@pytest.mark.asyncio
async def test_real_stdio_mcp_progressive_discovery_and_guarded_call():
    root_dir = Path(__file__).resolve().parents[5]
    manager = _manager(root_dir)
    await manager.start()
    try:
        context = await manager.get_context_block()
        assert "tools=2" in context
        assert "input_schema" not in context
        assert "echo" not in context

        catalog = await manager.search_tools("echo message", "fixture", 5)
        match = next(item for item in catalog["matches"] if item["name"] == "echo")
        assert match["allowed"] is True
        assert len(match["schema_sha256"]) == 64

        success, result = await manager.call_tool(
            "fixture",
            "echo",
            {"message": "hello"},
            match["schema_sha256"],
        )
        assert success is True
        assert result["content"][0]["text"] == "hello"

        with pytest.raises(MCPClientError, match="schema hash is stale"):
            await manager.call_tool(
                "fixture", "echo", {"message": "hello"}, "0" * 64
            )
        with pytest.raises(MCPClientError, match="allowlist"):
            blocked = await manager.search_tools("blocked tool", "fixture", 5)
            blocked_match = next(
                item for item in blocked["matches"]
                if item["name"] == "blocked_tool"
            )
            await manager.call_tool(
                "fixture",
                "blocked_tool",
                {"value": "no"},
                blocked_match["schema_sha256"],
            )
        with pytest.raises(MCPClientError, match="tool arguments"):
            await manager.call_tool(
                "fixture", "echo", {}, match["schema_sha256"]
            )
    finally:
        await manager.stop()
    assert manager.workers["fixture"].task is None


@pytest.mark.asyncio
async def test_real_stdio_mcp_resources_prompts_and_bounded_projection():
    root_dir = Path(__file__).resolve().parents[5]
    manager = _manager(root_dir)
    await manager.start()
    try:
        resources = await manager.list_resources("fixture")
        assert resources["resources"][0]["uri"] == "memo://status"
        read = await manager.read_resource("fixture", "memo://status")
        assert read["contents"][0]["text"] == "ready"

        prompts = await manager.list_prompts("fixture")
        prompt = next(item for item in prompts["prompts"] if item["name"] == "greet")
        rendered = await manager.get_prompt(
            "fixture", "greet", {"name": "JAWL"}, prompt["schema_sha256"]
        )
        assert "Hello, JAWL." in json.dumps(rendered)
        with pytest.raises(MCPClientError, match="SHA-256"):
            await manager.get_prompt(
                "fixture", "greet", {"name": "JAWL"}, "NOT-A-HASH"
            )
    finally:
        await manager.stop()

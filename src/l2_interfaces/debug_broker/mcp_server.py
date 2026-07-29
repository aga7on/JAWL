"""Standalone stdio MCP facade for the native JAWL Debug Broker.

This process is intentionally thin: JAWL uses the native skill surface, while
other agents can launch this module and receive the same typed operation
catalog and durable session semantics over MCP.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from src.l2_interfaces.debug_broker.client import DebugBrokerClient
from src.utils.settings import load_config


FRAMEWORK_ROOT = Path(__file__).resolve().parents[3]
_, _interfaces = load_config()
client = DebugBrokerClient(
    _interfaces.debug_broker,
    FRAMEWORK_ROOT,
    state_namespace=f"mcp-{os.getpid()}",
)
_start_lock = asyncio.Lock()
_started = False


@asynccontextmanager
async def _broker_lifespan(_: FastMCP):
    """Bind provider ownership to the external MCP server process lifetime."""

    global _started
    await client.start()
    _started = True
    try:
        yield {"client": client}
    finally:
        await client.stop()
        _started = False


server = FastMCP("jawl-debug-broker", lifespan=_broker_lifespan)


async def _ensure_started() -> None:
    global _started
    if _started:
        return
    async with _start_lock:
        if not _started:
            await client.start()
            _started = True


@server.tool()
async def list_providers() -> dict[str, Any]:
    """List installed debug providers, availability, and operation counts."""

    await _ensure_started()
    return {"providers": client.provider_snapshot()}


@server.tool()
async def search_operations(
    query: str = "",
    provider: str | None = None,
    limit: int = 12,
) -> dict[str, Any]:
    """Discover operation names, exact input schemas, and schema hashes."""

    await _ensure_started()
    return client.search_operations(query, provider, limit)


@server.tool()
async def start_session(
    provider: str,
    target: str | None = None,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a durable session and auto-start its provider when necessary."""

    await _ensure_started()
    return await client.start_session(provider, target, options)


@server.tool()
async def call_operation(
    provider: str,
    operation: str,
    arguments: dict[str, Any],
    expected_schema_sha256: str,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Call one exact provider operation after checking its current schema."""

    await _ensure_started()
    return await client.call_operation(
        provider,
        operation,
        arguments,
        expected_schema_sha256,
        session_id,
    )


@server.tool()
async def wait_session(
    session_id: str,
    timeout_seconds: float = 30,
) -> dict[str, Any]:
    """Wait at most 60 seconds for a session state transition."""

    await _ensure_started()
    if timeout_seconds < 0 or timeout_seconds > 60:
        raise ValueError("timeout_seconds must be between 0 and 60")
    return await client.wait_session(session_id, timeout_seconds)


@server.tool()
async def session_snapshot(session_id: str | None = None) -> dict[str, Any]:
    """Inspect durable sessions without touching a provider process."""

    await _ensure_started()
    return client.session_snapshot(session_id)


@server.tool()
async def stop_session(session_id: str) -> dict[str, Any]:
    """Close a session and only provider processes owned by that session."""

    await _ensure_started()
    return await client.stop_session(session_id)


if __name__ == "__main__":
    server.run(transport="stdio")

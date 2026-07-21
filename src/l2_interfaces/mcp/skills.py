"""Bounded model-facing skills for progressive MCP discovery and execution."""

from __future__ import annotations

from typing import Any, Optional

from src.l2_interfaces.mcp.client import (
    MCPClientError,
    MCPClientManager,
    MCPToolOutcomeUnknown,
)
from src.l3_agent.skills.registry import SkillResult, skill


class MCPTools:
    """Expose MCP primitives without injecting every remote schema into context."""

    def __init__(self, client: MCPClientManager) -> None:
        self.client = client

    @staticmethod
    def _validate_identifier(value: str, *, field: str, max_chars: int = 256) -> str:
        normalized = value.strip()
        if (
            not normalized
            or len(normalized) > max_chars
            or "\x00" in normalized
            or any(character in normalized for character in ("\r", "\n"))
        ):
            raise MCPClientError(f"Invalid MCP {field}.")
        return normalized

    @skill()
    async def list_servers(self) -> SkillResult:
        """List configured MCP servers and payload-free connection health."""

        return SkillResult.ok(
            self.client.bounded_json(
                {
                    "online": self.client.state.is_online,
                    "servers": self.client.state.snapshot(),
                }
            )
        )

    @skill()
    async def reconnect_server(self, server: str) -> SkillResult:
        """Reconnect one configured MCP server without replaying prior calls."""

        try:
            server = self._validate_identifier(
                server, field="server name", max_chars=64
            )
            await self.client.reconnect(server)
            return SkillResult.ok(
                self.client.bounded_json(
                    {
                        "server": server,
                        "state": self.client.state.servers.get(server, {}).get(
                            "state"
                        ),
                    }
                )
            )
        except (MCPClientError, FileNotFoundError, TimeoutError) as exc:
            return SkillResult.fail(str(exc))

    @skill()
    async def search_tools(
        self,
        query: str,
        server: Optional[str] = None,
        limit: int = 12,
    ) -> SkillResult:
        """Search current MCP tool catalogs and return exact schemas plus hashes."""

        try:
            if server is not None:
                server = self._validate_identifier(
                    server, field="server name", max_chars=64
                )
            payload = await self.client.search_tools(query, server, limit)
            return SkillResult.ok(self.client.bounded_json(payload))
        except (MCPClientError, FileNotFoundError, TimeoutError) as exc:
            return SkillResult.fail(str(exc))

    @skill()
    async def call_tool(
        self,
        server: str,
        tool: str,
        arguments: dict[str, Any],
        expected_schema_sha256: str,
    ) -> SkillResult:
        """Call one allowlisted MCP tool after an exact current schema check.

        Use the schema hash returned by search_tools. Calls are never retried
        automatically because cancellation or timeout can leave external effects
        with an unknown outcome.
        """

        try:
            server = self._validate_identifier(
                server, field="server name", max_chars=64
            )
            tool = self._validate_identifier(tool, field="tool name")
            success, payload = await self.client.call_tool(
                server,
                tool,
                arguments,
                expected_schema_sha256,
            )
            message = self.client.bounded_json(payload)
            return SkillResult.ok(message) if success else SkillResult.fail(message)
        except MCPToolOutcomeUnknown as exc:
            return SkillResult.fail(
                self.client.bounded_json(
                    {
                        "server": server,
                        "tool": tool,
                        "outcome_unknown": True,
                        "retry_performed": False,
                        "error": str(exc),
                    }
                )
            )
        except (MCPClientError, FileNotFoundError, TimeoutError) as exc:
            return SkillResult.fail(str(exc))

    @skill()
    async def list_resources(self, server: str) -> SkillResult:
        """List resources/templates when explicitly enabled for an MCP server."""

        try:
            server = self._validate_identifier(
                server, field="server name", max_chars=64
            )
            return SkillResult.ok(
                self.client.bounded_json(
                    await self.client.list_resources(server)
                )
            )
        except (MCPClientError, FileNotFoundError, TimeoutError) as exc:
            return SkillResult.fail(str(exc))

    @skill()
    async def read_resource(self, server: str, uri: str) -> SkillResult:
        """Read one MCP resource with bounded text or sandboxed binary artifacts."""

        try:
            server = self._validate_identifier(
                server, field="server name", max_chars=64
            )
            uri = self._validate_identifier(uri, field="resource URI", max_chars=2000)
            return SkillResult.ok(
                self.client.bounded_json(
                    await self.client.read_resource(server, uri)
                )
            )
        except (MCPClientError, FileNotFoundError, TimeoutError) as exc:
            return SkillResult.fail(str(exc))

    @skill()
    async def list_prompts(self, server: str) -> SkillResult:
        """List prompts when explicitly enabled for an MCP server."""

        try:
            server = self._validate_identifier(
                server, field="server name", max_chars=64
            )
            return SkillResult.ok(
                self.client.bounded_json(await self.client.list_prompts(server))
            )
        except (MCPClientError, FileNotFoundError, TimeoutError) as exc:
            return SkillResult.fail(str(exc))

    @skill()
    async def get_prompt(
        self,
        server: str,
        prompt: str,
        arguments: dict[str, str],
        expected_schema_sha256: str,
    ) -> SkillResult:
        """Get one MCP prompt after validating its current exact schema hash."""

        try:
            server = self._validate_identifier(
                server, field="server name", max_chars=64
            )
            prompt = self._validate_identifier(prompt, field="prompt name")
            return SkillResult.ok(
                self.client.bounded_json(
                    await self.client.get_prompt(
                        server,
                        prompt,
                        arguments,
                        expected_schema_sha256,
                    )
                )
            )
        except (MCPClientError, FileNotFoundError, TimeoutError) as exc:
            return SkillResult.fail(str(exc))

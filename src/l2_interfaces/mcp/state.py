"""Payload-free L0 health state for configured MCP servers."""

from __future__ import annotations

from typing import Any


class MCPState:
    """Retain bounded connection metadata without tool inputs or outputs."""

    def __init__(self) -> None:
        self.is_online = False
        self.servers: dict[str, dict[str, Any]] = {}

    def update_server(self, server_key: str, **values: Any) -> None:
        current = dict(self.servers.get(server_key, {}))
        current.update(values)
        self.servers[server_key] = current
        self.is_online = any(
            server.get("state") == "online" for server in self.servers.values()
        )

    def snapshot(self) -> list[dict[str, Any]]:
        fields = (
            "name",
            "transport",
            "state",
            "protocol_version",
            "server_name",
            "server_version",
            "tool_count",
            "resource_count",
            "prompt_count",
            "last_error",
        )
        return [
            {field: data.get(field) for field in fields if field in data}
            for _, data in sorted(self.servers.items())
        ]

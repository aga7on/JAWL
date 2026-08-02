"""Abstract provider boundary used by every JAWL cognitive subsystem."""

from __future__ import annotations

from abc import ABC, abstractmethod

from src.l3_agent.llm.providers.contracts import (
    LLMRequest,
    LLMResult,
    ProviderCapabilities,
    ProviderHealth,
    ToolTransport,
    normalize_tool_transport,
)


class LLMProvider(ABC):
    name = "provider"

    def __init__(self, capabilities: ProviderCapabilities) -> None:
        self.capabilities = capabilities

    def resolve_tool_transport(self, requested: str) -> ToolTransport:
        transport = normalize_tool_transport(requested)
        if transport == "auto":
            return "native" if self.capabilities.native_tools else "json_envelope"
        if transport == "native" and not self.capabilities.native_tools:
            raise ValueError(
                f"provider '{self.name}' does not advertise native tool support"
            )
        return transport

    @abstractmethod
    async def complete(self, request: LLMRequest) -> LLMResult:
        raise NotImplementedError

    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider=self.name,
            status="ok",
            detail="configured",
            capabilities=self.capabilities,
        )

    async def close(self) -> None:
        return None

    def on_rate_limit(self, error: Exception) -> None:
        """Allow key-owning providers to update local key rotation state."""

    def on_authentication_error(self, error: Exception) -> None:
        """Allow key-owning providers to remove invalid credentials."""


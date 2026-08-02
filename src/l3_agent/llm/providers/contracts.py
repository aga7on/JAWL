"""Typed provider-neutral request, response, error, and capability records."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Sequence


ToolTransport = Literal["native", "json_envelope", "auto"]
ProviderErrorCategory = Literal[
    "transport",
    "timeout",
    "rate_limit",
    "authentication",
    "authorization",
    "configuration",
    "input_rejected",
    "session_state",
    "provider",
    "invalid_response",
    "tool_protocol",
    "cancelled",
]


def normalize_tool_transport(value: str | None) -> ToolTransport:
    """Migrate legacy names while exposing only the new public contract."""

    normalized = str(value or "json_envelope").strip().lower()
    aliases = {
        "wrapper": "json_envelope",
        "hybrid": "auto",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in {"native", "json_envelope", "auto"}:
        raise ValueError(
            "tool transport must be native, json_envelope, or auto"
        )
    return normalized  # type: ignore[return-value]


@dataclass(frozen=True)
class ProviderCapabilities:
    native_tools: bool = False
    json_schema: bool = False
    vision: bool = False
    video: bool = False
    image_generation: bool = False
    reasoning: bool = False
    context_window: int = 0
    streaming: bool = False
    server_side_conversation: bool = False

    def __post_init__(self) -> None:
        if self.context_window < 0:
            raise ValueError("context_window cannot be negative")

    def public(self) -> dict[str, Any]:
        return {
            "native_tools": self.native_tools,
            "json_schema": self.json_schema,
            "vision": self.vision,
            "video": self.video,
            "image_generation": self.image_generation,
            "reasoning": self.reasoning,
            "context_window": self.context_window,
            "streaming": self.streaming,
            "server_side_conversation": self.server_side_conversation,
        }


@dataclass(frozen=True)
class LLMToolCall:
    id: str
    name: str
    arguments: str

    def __post_init__(self) -> None:
        if not self.id or len(self.id) > 200:
            raise ValueError("tool call id must contain 1-200 characters")
        if not self.name or len(self.name) > 256:
            raise ValueError("tool name must contain 1-256 characters")

    def as_openai_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": self.arguments,
            },
        }


@dataclass(frozen=True)
class LLMMessage:
    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: Any = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: tuple[LLMToolCall, ...] = ()

    @classmethod
    def from_openai_dict(cls, payload: Mapping[str, Any]) -> "LLMMessage":
        role = str(payload.get("role") or "")
        if role not in {"system", "developer", "user", "assistant", "tool"}:
            raise ValueError(f"unsupported LLM message role: {role or '<empty>'}")
        calls: list[LLMToolCall] = []
        for index, item in enumerate(payload.get("tool_calls") or []):
            if not isinstance(item, Mapping):
                raise ValueError("message tool_calls must contain objects")
            function = item.get("function") or {}
            if not isinstance(function, Mapping):
                raise ValueError("tool call function must be an object")
            calls.append(
                LLMToolCall(
                    id=str(item.get("id") or f"call_{index}"),
                    name=str(function.get("name") or ""),
                    arguments=str(function.get("arguments") or "{}"),
                )
            )
        return cls(
            role=role,  # type: ignore[arg-type]
            content=payload.get("content"),
            name=str(payload["name"]) if payload.get("name") else None,
            tool_call_id=(
                str(payload["tool_call_id"])
                if payload.get("tool_call_id")
                else None
            ),
            tool_calls=tuple(calls),
        )

    def as_openai_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "role": self.role,
            "content": self.content,
        }
        if self.name:
            payload["name"] = self.name
        if self.tool_call_id:
            payload["tool_call_id"] = self.tool_call_id
        if self.tool_calls:
            payload["tool_calls"] = [
                call.as_openai_dict() for call in self.tool_calls
            ]
        return payload


@dataclass(frozen=True)
class LLMToolDefinition:
    name: str
    description: str = ""
    parameters: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_openai_dict(
        cls, payload: Mapping[str, Any]
    ) -> "LLMToolDefinition":
        if payload.get("type", "function") != "function":
            raise ValueError("only function tools are supported")
        function = payload.get("function") or {}
        if not isinstance(function, Mapping):
            raise ValueError("tool function must be an object")
        parameters = function.get("parameters") or {"type": "object"}
        if not isinstance(parameters, Mapping):
            raise ValueError("tool parameters must be a JSON Schema object")
        return cls(
            name=str(function.get("name") or ""),
            description=str(function.get("description") or ""),
            parameters=dict(parameters),
        )

    def __post_init__(self) -> None:
        if not self.name or len(self.name) > 256:
            raise ValueError("tool name must contain 1-256 characters")

    def as_openai_dict(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": dict(self.parameters),
            },
        }


@dataclass(frozen=True)
class LLMRequest:
    model: str
    messages: tuple[LLMMessage, ...]
    temperature: float = 1.0
    tools: tuple[LLMToolDefinition, ...] = ()
    tool_choice: Any = None
    tool_transport: ToolTransport = "json_envelope"
    stream: bool = False
    enable_reasoning: bool | None = None
    session_id: str = ""
    timeout_seconds: float | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.model:
            raise ValueError("LLM request model is required")
        if self.temperature < 0 or self.temperature > 2:
            raise ValueError("temperature must be between 0 and 2")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        normalize_tool_transport(self.tool_transport)


@dataclass(frozen=True)
class LLMUsage:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    reasoning_tokens: int | None = None
    cached_tokens: int | None = None


@dataclass(frozen=True)
class LLMResult:
    content: str = ""
    tool_calls: tuple[LLMToolCall, ...] = ()
    finish_reason: str | None = None
    response_id: str | None = None
    model: str | None = None
    usage: LLMUsage = field(default_factory=LLMUsage)
    reasoning: str = ""
    provider_metadata: Mapping[str, Any] = field(default_factory=dict)
    raw: Any = None


@dataclass(frozen=True)
class ProviderHealth:
    provider: str
    status: Literal["ok", "degraded", "offline", "invalid"]
    detail: str = ""
    latency_ms: float | None = None
    capabilities: ProviderCapabilities = field(
        default_factory=ProviderCapabilities
    )
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "status": self.status,
            "detail": self.detail,
            "latency_ms": self.latency_ms,
            "capabilities": self.capabilities.public(),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class RetryPolicy:
    transport_retries: int = 1
    provider_retries: int = 2
    invalid_response_retries: int = 1
    tool_protocol_retries: int = 1
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 8.0

    def __post_init__(self) -> None:
        for value in (
            self.transport_retries,
            self.provider_retries,
            self.invalid_response_retries,
            self.tool_protocol_retries,
        ):
            if value < 0 or value > 10:
                raise ValueError("retry counts must be between 0 and 10")
        if self.base_delay_seconds < 0 or self.max_delay_seconds < 0:
            raise ValueError("retry delays cannot be negative")

    @property
    def max_attempts(self) -> int:
        return 1 + max(
            self.transport_retries,
            self.provider_retries,
            self.invalid_response_retries,
            self.tool_protocol_retries,
        )


class ProviderError(Exception):
    """A safe, classified provider failure consumed by the common executor."""

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        category: ProviderErrorCategory,
        status_code: int | None = None,
        code: str = "",
        retryable: bool = False,
        retry_after: float | None = None,
        details: Mapping[str, Any] | None = None,
        cause: Exception | None = None,
    ) -> None:
        self.provider = provider
        self.category = category
        self.status_code = status_code
        self.code = code
        self.retryable = retryable
        self.retry_after = retry_after
        self.details = dict(details or {})
        self.cause = cause
        super().__init__(message)


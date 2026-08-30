"""Standards-oriented OpenAI Chat Completions provider adapter."""

from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any, Mapping

import openai

from src.l3_agent.llm.client import LLMClient
from src.l3_agent.llm.exceptions import AllKeysExhaustedError
from src.l3_agent.llm.providers.base import LLMProvider
from src.l3_agent.llm.providers.contracts import (
    LLMRequest,
    LLMResult,
    LLMToolCall,
    LLMUsage,
    ProviderCapabilities,
    ProviderError,
    ProviderHealth,
)


class OpenAICompatibleProvider(LLMProvider):
    """Provider with no QWB headers, endpoints, or conversation assumptions."""

    name = "openai_compatible"

    def __init__(
        self,
        client: LLMClient,
        capabilities: ProviderCapabilities | None = None,
        *,
        provider_name: str = "openai_compatible",
        request_timeout_seconds: float | None = None,
    ) -> None:
        super().__init__(
            capabilities
            or ProviderCapabilities(
                native_tools=True,
                json_schema=True,
                streaming=True,
            )
        )
        self.client = client
        self.name = provider_name
        self.request_timeout_seconds = request_timeout_seconds
        self._last_session: Any = None

    def _request_kwargs(self, request: LLMRequest) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": request.model,
            "messages": [message.as_openai_dict() for message in request.messages],
            "temperature": request.temperature,
            "stream": request.stream,
        }
        if request.tools and self.capabilities.native_tools:
            kwargs["tools"] = [tool.as_openai_dict() for tool in request.tools]
        if request.tool_choice is not None and "tools" in kwargs:
            kwargs["tool_choice"] = request.tool_choice
        if request.tool_transport == "json_envelope" and self.capabilities.json_schema:
            kwargs["response_format"] = {"type": "json_object"}
        return kwargs

    async def complete(self, request: LLMRequest) -> LLMResult:
        if request.stream and not self.capabilities.streaming:
            raise ProviderError(
                "streaming is not supported by this provider",
                provider=self.name,
                category="configuration",
                retryable=False,
            )
        if (
            request.tools
            and request.tool_transport == "native"
            and not self.capabilities.native_tools
        ):
            raise ProviderError(
                "tools were supplied to a provider without tool capability",
                provider=self.name,
                category="configuration",
                retryable=False,
            )

        try:
            self._last_session = self.client.get_session()
            operation = self._last_session.chat.completions.create(
                **self._request_kwargs(request)
            )
            timeout_seconds = (
                request.timeout_seconds
                if request.timeout_seconds is not None
                else self.request_timeout_seconds
            )
            if timeout_seconds is not None:
                response = await asyncio.wait_for(
                    operation,
                    timeout=timeout_seconds,
                )
            else:
                response = await operation
            if request.stream:
                return await self._consume_stream(response, request)
            return self._convert_response(response, request)
        except asyncio.CancelledError:
            raise
        except ProviderError:
            raise
        except asyncio.TimeoutError as exc:
            raise ProviderError(
                "provider request timed out",
                provider=self.name,
                category="timeout",
                retryable=True,
                cause=exc,
            ) from exc
        except Exception as exc:
            raise self._map_error(exc) from exc

    def _convert_response(self, response: Any, request: LLMRequest) -> LLMResult:
        choices = getattr(response, "choices", None) or []
        if not choices:
            raise ProviderError(
                "provider response contains no choices",
                provider=self.name,
                category="invalid_response",
                retryable=True,
            )
        choice = choices[0]
        message = getattr(choice, "message", None)
        if message is None:
            raise ProviderError(
                "provider response choice contains no message",
                provider=self.name,
                category="invalid_response",
                retryable=True,
            )
        calls = tuple(
            self._convert_tool_call(item, index)
            for index, item in enumerate(getattr(message, "tool_calls", None) or [])
        )
        content = getattr(message, "content", None)
        reasoning = (
            getattr(message, "reasoning_content", None)
            or getattr(message, "reasoning", None)
            or ""
        )
        converted = LLMResult(
            content=content if isinstance(content, str) else "",
            tool_calls=calls,
            finish_reason=getattr(choice, "finish_reason", None),
            response_id=getattr(response, "id", None),
            model=getattr(response, "model", None) or request.model,
            usage=self._convert_usage(getattr(response, "usage", None)),
            reasoning=reasoning if isinstance(reasoning, str) else "",
            provider_metadata={"transport": "openai_chat_completions"},
            raw=response,
        )
        if not converted.content.strip() and not converted.tool_calls:
            raise ProviderError(
                "provider returned an empty final answer",
                provider=self.name,
                category="invalid_response",
                code="empty_response",
                retryable=True,
            )
        return converted

    async def _consume_stream(
        self, stream: Any, request: LLMRequest
    ) -> LLMResult:
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        call_parts: dict[int, dict[str, str]] = {}
        finish_reason = None
        response_id = None
        model = request.model
        usage = LLMUsage()
        event_count = 0

        async for chunk in stream:
            event_count += 1
            response_id = getattr(chunk, "id", None) or response_id
            model = getattr(chunk, "model", None) or model
            chunk_usage = getattr(chunk, "usage", None)
            if chunk_usage is not None:
                usage = self._convert_usage(chunk_usage)
            for choice in getattr(chunk, "choices", None) or []:
                finish_reason = getattr(choice, "finish_reason", None) or finish_reason
                delta = getattr(choice, "delta", None)
                if delta is None:
                    continue
                content = getattr(delta, "content", None)
                if isinstance(content, str):
                    content_parts.append(content)
                reasoning = (
                    getattr(delta, "reasoning_content", None)
                    or getattr(delta, "reasoning", None)
                )
                if isinstance(reasoning, str):
                    reasoning_parts.append(reasoning)
                for position, item in enumerate(
                    getattr(delta, "tool_calls", None) or []
                ):
                    index = getattr(item, "index", None)
                    if not isinstance(index, int):
                        index = position
                    target = call_parts.setdefault(
                        index,
                        {"id": "", "name": "", "arguments": ""},
                    )
                    item_id = getattr(item, "id", None)
                    if isinstance(item_id, str):
                        target["id"] += item_id
                    function = getattr(item, "function", None)
                    if function is not None:
                        name = getattr(function, "name", None)
                        arguments = getattr(function, "arguments", None)
                        if isinstance(name, str):
                            target["name"] += name
                        if isinstance(arguments, str):
                            target["arguments"] += arguments

        try:
            calls = tuple(
                LLMToolCall(
                    id=parts["id"] or self._generated_call_id(index, request.model),
                    name=parts["name"],
                    arguments=parts["arguments"] or "{}",
                )
                for index, parts in sorted(call_parts.items())
            )
        except ValueError as exc:
            raise ProviderError(
                f"invalid streamed tool call: {exc}",
                provider=self.name,
                category="tool_protocol",
                retryable=True,
                cause=exc,
            ) from exc
        content = "".join(content_parts)
        if event_count == 0 or (not content and not calls and not reasoning_parts):
            raise ProviderError(
                "provider stream ended without a response",
                provider=self.name,
                category="invalid_response",
                retryable=True,
            )
        return LLMResult(
            content=content,
            tool_calls=calls,
            finish_reason=finish_reason,
            response_id=response_id,
            model=model,
            usage=usage,
            reasoning="".join(reasoning_parts),
            provider_metadata={
                "transport": "openai_chat_completions",
                "stream_events": event_count,
            },
            raw=None,
        )

    @staticmethod
    def _generated_call_id(index: int, model: str) -> str:
        digest = hashlib.sha256(f"{model}:{index}".encode()).hexdigest()[:16]
        return f"call_{digest}"

    def _convert_tool_call(self, item: Any, index: int) -> LLMToolCall:
        function = getattr(item, "function", None)
        if function is None:
            raise ProviderError(
                "provider tool call contains no function",
                provider=self.name,
                category="tool_protocol",
                retryable=True,
            )
        try:
            return LLMToolCall(
                id=str(
                    getattr(item, "id", None)
                    or self._generated_call_id(index, self.name)
                ),
                name=str(getattr(function, "name", None) or ""),
                arguments=str(getattr(function, "arguments", None) or "{}"),
            )
        except ValueError as exc:
            raise ProviderError(
                f"invalid provider tool call: {exc}",
                provider=self.name,
                category="tool_protocol",
                retryable=True,
                cause=exc,
            ) from exc

    @staticmethod
    def _convert_usage(usage: Any) -> LLMUsage:
        if usage is None:
            return LLMUsage()
        prompt_details = getattr(usage, "prompt_tokens_details", None)
        completion_details = getattr(usage, "completion_tokens_details", None)
        return LLMUsage(
            prompt_tokens=getattr(usage, "prompt_tokens", None),
            completion_tokens=getattr(usage, "completion_tokens", None),
            total_tokens=getattr(usage, "total_tokens", None),
            reasoning_tokens=getattr(completion_details, "reasoning_tokens", None),
            cached_tokens=getattr(prompt_details, "cached_tokens", None),
        )

    def _map_error(self, error: Exception) -> ProviderError:
        status = self._status_code(error)
        code = self._error_code(error)
        text = str(error).lower()
        retry_after = self._retry_after(error)

        if isinstance(error, AllKeysExhaustedError):
            category, retryable = "rate_limit", True
            retry_after = float(error.wait_time + 1)
        elif isinstance(error, openai.APITimeoutError):
            category, retryable = "timeout", True
        elif isinstance(error, openai.APIConnectionError):
            category, retryable = "transport", True
        elif status == 401 or isinstance(error, openai.AuthenticationError):
            # The adapter owns a local credential pool, so one rejected key
            # can be banned and the next key tried by the common retry policy.
            category, retryable = "authentication", True
        elif status == 403 or isinstance(error, openai.PermissionDeniedError):
            category, retryable = "authorization", False
        elif status == 429 or isinstance(error, openai.RateLimitError):
            category, retryable = "rate_limit", True
        elif status in {408, 409}:
            category, retryable = "provider", True
        elif status is not None and status >= 500:
            category, retryable = "provider", True
        elif status == 400 or isinstance(error, openai.BadRequestError):
            static_markers = (
                "unsupported model",
                "unknown model",
                "model_not_found",
                "unknown parameter",
                "unsupported parameter",
                "missing required",
                "maximum context length",
                "context_length_exceeded",
            )
            if code.lower() in {"model_not_found", "context_length_exceeded"} or any(
                marker in text for marker in static_markers
            ):
                category, retryable = "configuration", False
            else:
                category, retryable = "input_rejected", True
        else:
            category, retryable = "provider", False

        return ProviderError(
            str(error),
            provider=self.name,
            category=category,  # type: ignore[arg-type]
            status_code=status,
            code=code,
            retryable=retryable,
            retry_after=retry_after,
            cause=error,
        )

    @staticmethod
    def _status_code(error: Exception) -> int | None:
        status = getattr(error, "status_code", None)
        if isinstance(status, int):
            return status
        response = getattr(error, "response", None)
        status = getattr(response, "status_code", None)
        return status if isinstance(status, int) else None

    @staticmethod
    def _error_code(error: Exception) -> str:
        body = getattr(error, "body", None)
        if isinstance(body, Mapping):
            nested = body.get("error")
            if isinstance(nested, Mapping) and nested.get("code"):
                return str(nested["code"])
            if body.get("code"):
                return str(body["code"])
        return ""

    @staticmethod
    def _retry_after(error: Exception) -> float | None:
        response = getattr(error, "response", None)
        headers = getattr(response, "headers", None)
        if not isinstance(headers, Mapping):
            return None
        raw = (
            headers.get("retry-after")
            or headers.get("x-ratelimit-reset")
            or headers.get("retry-after-ms")
        )
        if raw is None:
            return None
        try:
            value = float(raw)
            if headers.get("retry-after-ms"):
                value /= 1000
            if value > time.time():
                value -= time.time()
            return max(0.0, min(value, 86400.0))
        except (TypeError, ValueError):
            return None

    def on_rate_limit(self, error: Exception) -> None:
        session = self._last_session
        api_key = getattr(session, "api_key", None)
        if not api_key:
            return
        mapped = error if isinstance(error, ProviderError) else self._map_error(error)
        wait = int(mapped.retry_after or 30)
        self.client.rotator.cooldown_key(api_key, max(2, min(wait, 86400)))

    def on_authentication_error(self, error: Exception) -> None:
        api_key = getattr(self._last_session, "api_key", None)
        if api_key:
            self.client.rotator.ban_key(api_key)

    async def health(self) -> ProviderHealth:
        started = time.perf_counter()
        try:
            session = self.client.get_session()
            await asyncio.wait_for(session.models.list(), timeout=3.0)
            return ProviderHealth(
                provider=self.name,
                status="ok",
                detail="models endpoint reachable",
                latency_ms=round((time.perf_counter() - started) * 1000, 1),
                capabilities=self.capabilities,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            mapped = exc if isinstance(exc, ProviderError) else self._map_error(exc)
            return ProviderHealth(
                provider=self.name,
                status="offline" if mapped.retryable else "invalid",
                detail=f"{mapped.category}: {mapped}",
                latency_ms=round((time.perf_counter() - started) * 1000, 1),
                capabilities=self.capabilities,
            )

    async def close(self) -> None:
        await self.client.close()

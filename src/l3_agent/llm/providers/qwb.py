"""QWB-specific adapter; no Qwen Web assumptions escape this module."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from src.l3_agent.llm.client import LLMClient
from src.l3_agent.llm.providers.contracts import (
    LLMRequest,
    LLMResult,
    ProviderCapabilities,
    ProviderError,
    ProviderHealth,
)
from src.l3_agent.llm.providers.openai_compatible import (
    OpenAICompatibleProvider,
)


class QWBProvider(OpenAICompatibleProvider):
    name = "qwb"

    def __init__(
        self,
        client: LLMClient,
        capabilities: ProviderCapabilities | None = None,
        *,
        health_url: str = "",
        bridge_managed_accounts: bool = True,
        request_timeout_seconds: float | None = None,
    ) -> None:
        super().__init__(
            client,
            capabilities
            or ProviderCapabilities(
                # QWB exposes OpenAI tool_calls at its public boundary even
                # though the web model is driven through a JSON workaround.
                native_tools=True,
                json_schema=True,
                vision=True,
                video=True,
                image_generation=True,
                reasoning=True,
                context_window=262144,
                streaming=True,
                server_side_conversation=True,
            ),
            provider_name="qwb",
            request_timeout_seconds=request_timeout_seconds,
        )
        api_url = getattr(client, "api_url", "")
        self.health_url = health_url or self._default_health_url(
            api_url if isinstance(api_url, str) else ""
        )
        self.bridge_managed_accounts = bridge_managed_accounts

    @staticmethod
    def _default_health_url(api_url: str) -> str:
        parsed = urlsplit(api_url or "http://127.0.0.1:8000")
        path = parsed.path.rstrip("/")
        if path.endswith("/v1"):
            path = path[:-3]
        return urlunsplit((parsed.scheme, parsed.netloc, f"{path}/health", "", ""))

    def _request_kwargs(self, request: LLMRequest) -> dict[str, Any]:
        kwargs = super()._request_kwargs(request)
        if request.enable_reasoning is not None:
            kwargs["extra_body"] = {
                "enable_thinking": request.enable_reasoning
            }
        if request.session_id:
            lane = request.session_id.strip()
            if len(lane) > 200 or any(
                character
                not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._"
                for character in lane
            ):
                raise ProviderError(
                    "session_id contains unsupported characters",
                    provider=self.name,
                    category="configuration",
                    retryable=False,
                )
            kwargs["extra_headers"] = {"X-Session-Id": lane}
        return kwargs

    async def complete(self, request: LLMRequest) -> LLMResult:
        try:
            result = await super().complete(request)
        except ProviderError as exc:
            if exc.category == "invalid_response" and exc.code == "empty_response":
                raise ProviderError(
                    "QWB returned an empty/leaked Qwen answer",
                    provider=self.name,
                    category="invalid_response",
                    code="empty_qwen_answer",
                    retryable=True,
                    cause=exc,
                ) from exc
            raise
        if not result.content.strip() and not result.tool_calls:
            raise ProviderError(
                "QWB returned an empty/leaked Qwen answer",
                provider=self.name,
                category="invalid_response",
                code="empty_qwen_answer",
                retryable=True,
            )
        return LLMResult(
            content=result.content,
            tool_calls=result.tool_calls,
            finish_reason=result.finish_reason,
            response_id=result.response_id,
            model=result.model,
            usage=result.usage,
            reasoning=result.reasoning,
            provider_metadata={
                **dict(result.provider_metadata),
                "conversation": "server_side",
                "lane": request.session_id,
                "media_base_url": self.health_url.rsplit("/health", 1)[0],
            },
            raw=result.raw,
        )

    def _map_error(self, error: Exception) -> ProviderError:
        mapped = super()._map_error(error)
        text = f"{mapped.code} {error}".lower()
        if "tool_protocol_error" in text or "malformed jawl tool transport" in text:
            # The bridge already discarded the bad lane, so this is a bounded
            # protocol repair rather than an upstream outage: it must consume
            # the tool_protocol budget instead of the generic provider one.
            return ProviderError(
                str(error),
                provider=self.name,
                category="tool_protocol",
                status_code=mapped.status_code,
                code=mapped.code or "tool_protocol_error",
                retryable=True,
                retry_after=mapped.retry_after,
                cause=error,
            )
        if "chat_not_found" in text or (
            ("chat" in text or "chat_id" in text)
            and ("not exist" in text or "not found" in text)
        ):
            return ProviderError(
                str(error),
                provider=self.name,
                category="session_state",
                status_code=mapped.status_code,
                code=mapped.code or "CHAT_NOT_FOUND",
                retryable=True,
                cause=error,
            )
        if "empty/leaked qwen answer" in text:
            return ProviderError(
                str(error),
                provider=self.name,
                category="invalid_response",
                status_code=mapped.status_code,
                code="empty_qwen_answer",
                retryable=True,
                cause=error,
            )
        return ProviderError(
            str(mapped),
            provider=self.name,
            category=mapped.category,
            status_code=mapped.status_code,
            code=mapped.code,
            retryable=mapped.retryable,
            retry_after=mapped.retry_after,
            cause=error,
        )

    def on_rate_limit(self, error: Exception) -> None:
        # One JAWL credential represents the bridge, while QWB owns the real
        # account pool. Cooling the bridge credential would disable healthy
        # accounts and defeat upstream rotation.
        if self.bridge_managed_accounts:
            return None
        super().on_rate_limit(error)

    async def health(self) -> ProviderHealth:
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=2.0) as client:
                response = await client.get(self.health_url)
                response.raise_for_status()
                payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("health payload is not an object")
            accounts = payload.get("accounts") or []
            public_accounts = [
                {
                    "label": str(item.get("label") or "token")[:100],
                    "fingerprint": str(item.get("fingerprint") or "")[:32],
                    "state": str(item.get("state") or "unknown")[:40],
                    "cooldownUntil": item.get("cooldownUntil"),
                    "inFlight": item.get("inFlight", 0),
                }
                for item in accounts
                if isinstance(item, dict)
            ]
            status = str(payload.get("status") or "invalid")
            if status == "ok" and any(
                item["state"] != "healthy" for item in public_accounts
            ):
                status = "degraded"
            if status not in {"ok", "degraded", "offline", "invalid"}:
                status = "invalid"
            return ProviderHealth(
                provider=self.name,
                status=status,  # type: ignore[arg-type]
                detail=f"{len(public_accounts)} account(s)",
                latency_ms=round((time.perf_counter() - started) * 1000, 1),
                capabilities=self.capabilities,
                metadata={
                    "model": payload.get("model"),
                    "transport": payload.get("transport"),
                    "accounts": public_accounts,
                    "sessions": payload.get("sessions", 0),
                    "mediaJobs": payload.get("mediaJobs") or {},
                },
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return ProviderHealth(
                provider=self.name,
                status="offline",
                detail=f"{type(exc).__name__}: {exc}",
                latency_ms=round((time.perf_counter() - started) * 1000, 1),
                capabilities=self.capabilities,
            )

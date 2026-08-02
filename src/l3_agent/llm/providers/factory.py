"""Validated provider construction from JAWL settings and existing clients."""

from __future__ import annotations

from urllib.parse import urlparse

from src.l3_agent.llm.client import LLMClient
from src.l3_agent.llm.providers.base import LLMProvider
from src.l3_agent.llm.providers.contracts import (
    ProviderCapabilities,
    RetryPolicy,
)
from src.l3_agent.llm.providers.openai_compatible import (
    OpenAICompatibleProvider,
)
from src.l3_agent.llm.providers.qwb import QWBProvider
from src.utils.settings import LLMProviderConfig


def retry_policy_from_config(config: LLMProviderConfig) -> RetryPolicy:
    return RetryPolicy(**config.retry.model_dump())


def build_llm_provider(
    config: LLMProviderConfig,
    client: LLMClient,
) -> LLMProvider:
    capabilities = ProviderCapabilities(**config.resolved_capabilities())
    if config.kind == "qwb":
        return QWBProvider(
            client,
            capabilities,
            health_url=config.health_url,
            request_timeout_seconds=config.request_timeout_seconds,
        )
    if config.kind == "openai_compatible":
        return OpenAICompatibleProvider(
            client,
            capabilities,
            provider_name=config.display_name or "openai_compatible",
            request_timeout_seconds=config.request_timeout_seconds,
        )
    raise ValueError(f"unsupported LLM provider kind: {config.kind}")


def validate_provider_startup(
    config: LLMProviderConfig,
    *,
    api_url: str,
    api_keys: list[str],
    model: str,
    tool_transport: str,
) -> None:
    """Fail before agent startup with safe, actionable configuration errors."""

    model = str(model or "").strip()
    if not model or model.lower() == "unknown":
        raise ValueError("LLM main_model must be configured before startup")

    url = str(api_url or "").strip()
    if config.kind == "qwb" and not url:
        raise ValueError("QWB provider requires LLM_API_URL")
    if url:
        parsed = urlparse(url if "://" in url else f"http://{url}")
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("LLM_API_URL must be an HTTP(S) base URL")

    usable_keys = [str(item).strip() for item in api_keys if str(item).strip()]
    host = urlparse(url if "://" in url else f"http://{url}").hostname if url else ""
    is_local = host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
    if config.kind == "openai_compatible" and not is_local:
        if not usable_keys or usable_keys == ["local_dummy_key"]:
            raise ValueError(
                "OpenAI-compatible cloud provider requires LLM_API_KEY_1"
            )

    capabilities = ProviderCapabilities(
        **config.resolved_capabilities()
    )
    from src.l3_agent.llm.providers.contracts import normalize_tool_transport

    selected = normalize_tool_transport(tool_transport)
    if selected == "native" and not capabilities.native_tools:
        raise ValueError(
            "tool_transport=native requires provider capability native_tools=true"
        )

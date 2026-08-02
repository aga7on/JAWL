"""Discovery of local OpenAI-compatible servers and their usable models.

JAWL never guesses a critical capability from a model name. This module only
*discovers* what is actually reachable: which local runtimes answer, which
models they list, and — where the runtime exposes real metadata — what those
models genuinely support. Anything a runtime does not state is reported as
unknown and left for an explicit probe or an operator decision, so a wrong
guess can never silently disable tools, vision, or the JSON action plan.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

import httpx

# Local runtimes we can find without any configuration. Each entry is a plain
# OpenAI-compatible server; nothing here is JAWL- or QWB-specific.
LOCAL_RUNTIMES: tuple[dict[str, Any], ...] = (
    {
        "runtime": "ollama",
        "label": "Ollama",
        "base_url": "http://127.0.0.1:11434/v1",
        "native_api": "http://127.0.0.1:11434/api",
        "api_key": "local_dummy_key",
    },
    {
        "runtime": "lmstudio",
        "label": "LM Studio",
        "base_url": "http://127.0.0.1:1234/v1",
        "native_api": "http://127.0.0.1:1234/api/v0",
        "api_key": "local_dummy_key",
    },
)

# Markers indicating a model is *not* a general coding chat model. These only
# ever demote a model's ranking; they never change a declared capability.
# Matched on name-token boundaries: a bare substring test would misread
# "fable5-composer" as the "e5" embedding family.
_NON_CHAT_MARKERS = (
    "embed",
    "embedding",
    "embeddings",
    "reranker",
    "rerank",
    "bge",
    "gte",
    "e5",
    "minilm",
    "clip",
    "whisper",
    "tts",
    "sd",
    "sdxl",
    "flux",
    "moderation",
    "guard",
)

# Model names mix '-', '_', '.', '/', ':' and version digits as separators.
_NAME_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")

# Hints that a model is oriented at code. Used only for ordering suggestions.
_CODING_HINTS = (
    "coder",
    "code",
    "codestral",
    "deepseek",
    "qwen",
    "starcoder",
    "codellama",
    "devstral",
    "granite-code",
    "composer",
)


@dataclass(frozen=True)
class DiscoveredModel:
    """One model offered by a reachable local runtime."""

    id: str
    runtime: str
    base_url: str
    # ``None`` means the runtime did not state this; never assume either way.
    native_tools: bool | None = None
    vision: bool | None = None
    reasoning: bool | None = None
    context_window: int | None = None
    parameter_size: str = ""
    quantization: str = ""
    family: str = ""
    size_bytes: int | None = None
    coding_score: int = 0
    warnings: tuple[str, ...] = ()

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "runtime": self.runtime,
            "base_url": self.base_url,
            "native_tools": self.native_tools,
            "vision": self.vision,
            "reasoning": self.reasoning,
            "context_window": self.context_window,
            "parameter_size": self.parameter_size,
            "quantization": self.quantization,
            "family": self.family,
            "coding_score": self.coding_score,
            "warnings": list(self.warnings),
        }

    def summary(self) -> str:
        """One readable line for the CLI, honest about unknowns."""

        parts: list[str] = [self.id]
        if self.parameter_size:
            parts.append(self.parameter_size)
        if self.quantization:
            parts.append(self.quantization)
        if self.context_window:
            parts.append(f"ctx {self.context_window // 1024}k")
        flags = []
        if self.native_tools is True:
            flags.append("tools")
        elif self.native_tools is None:
            flags.append("tools?")
        if self.vision is True:
            flags.append("vision")
        if self.reasoning is True:
            flags.append("reasoning")
        if flags:
            parts.append("/".join(flags))
        return " · ".join(parts)


@dataclass
class DiscoveredEndpoint:
    """A reachable OpenAI-compatible server plus everything it advertises."""

    runtime: str
    label: str
    base_url: str
    api_key: str = "local_dummy_key"
    reachable: bool = False
    detail: str = ""
    latency_ms: float | None = None
    models: list[DiscoveredModel] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        return {
            "runtime": self.runtime,
            "label": self.label,
            "base_url": self.base_url,
            "reachable": self.reachable,
            "detail": self.detail,
            "latency_ms": self.latency_ms,
            "models": [model.public() for model in self.models],
        }


def _models_url(base_url: str) -> str:
    base = (base_url or "").strip() or "https://api.openai.com/v1"
    if "://" not in base:
        base = f"http://{base}"
    parsed = urlsplit(base)
    path = parsed.path.rstrip("/")
    if not path.endswith("/models"):
        path = f"{path}/models"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _looks_non_chat(model_id: str) -> bool:
    """Detect embedding/reranker/media models by whole name tokens only."""

    tokens = {
        token
        for token in _NAME_TOKEN_SPLIT.split(model_id.casefold())
        if token
    }
    if tokens & set(_NON_CHAT_MARKERS):
        return True
    # "nomic-embed-text", "text-embedding-3-large": compound embedding names.
    return any(
        token.startswith("embed") or token.endswith("embedding")
        for token in tokens
    )


def _coding_score(model_id: str, parameter_size: str) -> int:
    """Rank chat models for coding work. Ordering only — never a capability."""

    lowered = model_id.casefold()
    if _looks_non_chat(lowered):
        return 0
    score = 10
    # Cap the coding bonus so a name stuffed with hints cannot outrank a model
    # that actually declares tool support and a large context.
    score += min(12, sum(6 for hint in _CODING_HINTS if hint in lowered))
    if "instruct" in lowered or "chat" in lowered:
        score += 3
    if "obliterated" in lowered or "abliterated" in lowered or "uncensored" in lowered:
        # Fine for local coding work; simply not a code specialisation.
        score += 1
    match = re.search(r"(\d+(?:\.\d+)?)\s*b\b", f"{parameter_size} {lowered}")
    if match:
        try:
            billions = float(match.group(1))
        except ValueError:
            billions = 0.0
        # Prefer models large enough to hold a coding protocol without
        # over-rewarding sizes a local machine may not run comfortably.
        if billions >= 6:
            score += 6
        elif billions >= 3:
            score += 3
        if billions >= 60:
            score -= 2
    return score


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if 0 < number <= 10_000_000 else None


def _parse_ollama_details(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Read Ollama's ``/api/show`` output without inventing capabilities."""

    details = payload.get("details")
    details = details if isinstance(details, Mapping) else {}
    capabilities = payload.get("capabilities")
    capabilities = [
        str(item).casefold()
        for item in (capabilities or [])
        if isinstance(item, str)
    ]
    info = payload.get("model_info")
    info = info if isinstance(info, Mapping) else {}
    context_window = None
    for key, value in info.items():
        if str(key).endswith(".context_length"):
            context_window = _int_or_none(value)
            break

    parsed: dict[str, Any] = {
        "parameter_size": str(details.get("parameter_size") or ""),
        "quantization": str(details.get("quantization_level") or ""),
        "family": str(details.get("family") or ""),
        "context_window": context_window,
    }
    # Ollama states capabilities explicitly, so trust the list and only the list.
    if capabilities:
        parsed["native_tools"] = "tools" in capabilities
        parsed["vision"] = "vision" in capabilities
        parsed["reasoning"] = "thinking" in capabilities
    return parsed


def _parse_lmstudio_entry(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Read LM Studio's ``/api/v0/models`` entry; unknown stays unknown."""

    parsed: dict[str, Any] = {
        "quantization": str(payload.get("quantization") or ""),
        "family": str(payload.get("arch") or ""),
        "context_window": _int_or_none(payload.get("max_context_length")),
    }
    capabilities = payload.get("capabilities")
    if isinstance(capabilities, Sequence) and not isinstance(capabilities, str):
        lowered = [str(item).casefold() for item in capabilities]
        parsed["native_tools"] = "tool_use" in lowered or "tools" in lowered
        parsed["vision"] = "vision" in lowered
    if payload.get("vision") is not None:
        parsed["vision"] = bool(payload["vision"])
    if payload.get("trained_for_tool_use") is not None:
        parsed["native_tools"] = bool(payload["trained_for_tool_use"])
    if payload.get("size_bytes") is not None:
        parsed["size_bytes"] = _int_or_none(payload.get("size_bytes"))
    return parsed


async def list_models(
    base_url: str,
    api_key: str = "",
    *,
    timeout: float = 5.0,
    proxy_url: str | None = None,
) -> list[str]:
    """List model ids from any OpenAI-compatible endpoint, local or cloud.

    Raises ``httpx.HTTPError`` or ``ValueError`` so the caller can present an
    actionable message. Never logs or returns the supplied credential.
    """

    headers = {"Accept": "application/json"}
    key = str(api_key or "").strip()
    if key and key != "local_dummy_key":
        headers["Authorization"] = f"Bearer {key}"
    url = _models_url(base_url)
    is_local = (urlsplit(url).hostname or "") in {
        "localhost",
        "127.0.0.1",
        "::1",
        "0.0.0.0",
    }
    async with httpx.AsyncClient(
        timeout=timeout,
        trust_env=not is_local,
        proxy=None if is_local else proxy_url,
    ) as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, Mapping):
        raise ValueError("models endpoint did not return an object")
    data = payload.get("data")
    if not isinstance(data, Sequence):
        raise ValueError("models endpoint returned no data array")
    return [
        str(item["id"])
        for item in data
        if isinstance(item, Mapping) and item.get("id")
    ]


async def _enrich_ollama(
    client: httpx.AsyncClient, native_api: str, model_id: str
) -> dict[str, Any]:
    try:
        response = await client.post(
            f"{native_api}/show", json={"model": model_id}
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError, json.JSONDecodeError):
        return {}
    return _parse_ollama_details(payload) if isinstance(payload, Mapping) else {}


async def _enrich_lmstudio(
    client: httpx.AsyncClient, native_api: str
) -> dict[str, dict[str, Any]]:
    try:
        response = await client.get(f"{native_api}/models")
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError, json.JSONDecodeError):
        return {}
    data = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(data, Sequence):
        return {}
    enriched: dict[str, dict[str, Any]] = {}
    for item in data:
        if not isinstance(item, Mapping) or not item.get("id"):
            continue
        if str(item.get("type") or "").casefold() in {"embeddings", "embedding"}:
            enriched[str(item["id"])] = {"non_chat": True}
            continue
        enriched[str(item["id"])] = _parse_lmstudio_entry(item)
    return enriched


async def probe_endpoint(
    runtime: str,
    label: str,
    base_url: str,
    *,
    api_key: str = "local_dummy_key",
    native_api: str = "",
    timeout: float = 4.0,
    enrich: bool = True,
) -> DiscoveredEndpoint:
    """Probe one endpoint and describe every model it actually offers."""

    endpoint = DiscoveredEndpoint(
        runtime=runtime, label=label, base_url=base_url, api_key=api_key
    )
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        model_ids = await list_models(base_url, api_key, timeout=timeout)
    except asyncio.CancelledError:
        raise
    except (httpx.HTTPError, ValueError, json.JSONDecodeError, OSError) as exc:
        endpoint.detail = f"{type(exc).__name__}: {exc}"[:200]
        endpoint.latency_ms = round((loop.time() - started) * 1000, 1)
        return endpoint

    endpoint.reachable = True
    endpoint.latency_ms = round((loop.time() - started) * 1000, 1)
    endpoint.detail = f"{len(model_ids)} model(s)"

    metadata: dict[str, dict[str, Any]] = {}
    if enrich and native_api:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            if runtime == "lmstudio":
                metadata = await _enrich_lmstudio(client, native_api)
            elif runtime == "ollama":
                gathered = await asyncio.gather(
                    *(
                        _enrich_ollama(client, native_api, model_id)
                        for model_id in model_ids
                    ),
                    return_exceptions=True,
                )
                metadata = {
                    model_id: (item if isinstance(item, dict) else {})
                    for model_id, item in zip(model_ids, gathered)
                }

    models: list[DiscoveredModel] = []
    for model_id in model_ids:
        info = metadata.get(model_id, {})
        warnings: list[str] = []
        if info.get("non_chat") or _looks_non_chat(model_id):
            warnings.append("does not look like a chat model")
        if info.get("native_tools") is None:
            warnings.append(
                "runtime did not declare tool support; "
                "json_envelope transport is the safe default"
            )
        parameter_size = str(info.get("parameter_size") or "")
        score = 0 if info.get("non_chat") else _coding_score(model_id, parameter_size)
        models.append(
            DiscoveredModel(
                id=model_id,
                runtime=runtime,
                base_url=base_url,
                native_tools=info.get("native_tools"),
                vision=info.get("vision"),
                reasoning=info.get("reasoning"),
                context_window=info.get("context_window"),
                parameter_size=parameter_size,
                quantization=str(info.get("quantization") or ""),
                family=str(info.get("family") or ""),
                size_bytes=info.get("size_bytes"),
                coding_score=score,
                warnings=tuple(warnings),
            )
        )

    # Best coding candidate first, then a stable alphabetical order.
    models.sort(key=lambda item: (-item.coding_score, item.id))
    endpoint.models = models
    return endpoint


async def scan_local_runtimes(
    *,
    timeout: float = 4.0,
    enrich: bool = True,
    runtimes: Sequence[Mapping[str, Any]] | None = None,
) -> list[DiscoveredEndpoint]:
    """Scan every known local runtime concurrently; reachable ones come first."""

    targets = list(runtimes if runtimes is not None else LOCAL_RUNTIMES)
    results = await asyncio.gather(
        *(
            probe_endpoint(
                str(target["runtime"]),
                str(target.get("label") or target["runtime"]),
                str(target["base_url"]),
                api_key=str(target.get("api_key") or "local_dummy_key"),
                native_api=str(target.get("native_api") or ""),
                timeout=timeout,
                enrich=enrich,
            )
            for target in targets
        ),
        return_exceptions=True,
    )
    endpoints = [item for item in results if isinstance(item, DiscoveredEndpoint)]
    endpoints.sort(key=lambda item: (not item.reachable, item.label))
    return endpoints


def recommend_coding_model(
    endpoints: Sequence[DiscoveredEndpoint],
) -> DiscoveredModel | None:
    """Pick the best available local coding model, or nothing if unclear."""

    candidates = [
        model
        for endpoint in endpoints
        if endpoint.reachable
        for model in endpoint.models
        if model.coding_score > 0
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda model: (
            model.coding_score,
            model.native_tools is True,
            model.context_window or 0,
        ),
    )


def capabilities_for_model(model: DiscoveredModel) -> dict[str, Any]:
    """Build a conservative capability profile from *declared* facts only.

    Unknown never becomes ``True``: an undeclared capability is reported as
    unsupported so JAWL falls back to the JSON action plan instead of silently
    sending native tools to a model that cannot honour them.
    """

    return {
        "native_tools": model.native_tools is True,
        "json_schema": True,
        "vision": model.vision is True,
        "video": False,
        "image_generation": False,
        "reasoning": model.reasoning is True,
        "context_window": int(model.context_window or 0),
        "streaming": True,
        "server_side_conversation": False,
    }


def tool_transport_for_model(model: DiscoveredModel) -> str:
    """Choose the transport a model can actually honour."""

    return "native" if model.native_tools is True else "json_envelope"

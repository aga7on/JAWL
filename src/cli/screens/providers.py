"""Guided provider configuration without hand-editing YAML or secrets."""

from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import questionary
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from src.cli.screens.agent_control import _is_agent_running
from src.cli.screens.runtime import _provider_environment, _provider_health
from src.cli.widgets.ui import (
    console,
    draw_header,
    get_custom_style,
    print_error,
    print_info,
    print_success,
    set_window_title,
    wait_for_enter,
)
from src.instances.paths import get_instance_paths
from src.l3_agent.llm.providers.discovery import (
    DiscoveredEndpoint,
    DiscoveredModel,
    capabilities_for_model,
    list_models,
    recommend_coding_model,
    scan_local_runtimes,
    tool_transport_for_model,
)
from src.l3_agent.llm.providers.factory import validate_provider_startup
from src.utils.settings import LLMProviderConfig, load_config


def _qwb_health_url(base_url: str) -> str:
    parsed = urlsplit(base_url.strip() or "http://127.0.0.1:8000/v1")
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[:-3]
    return urlunsplit((parsed.scheme, parsed.netloc, f"{path}/health", "", ""))


def _write_env_values(path: Path, updates: dict[str, str]) -> None:
    """Update or append exact dotenv keys while rejecting multiline values."""

    for key, value in updates.items():
        if not key or any(character in key for character in "=\r\n"):
            raise ValueError("invalid environment key")
        if "\r" in value or "\n" in value:
            raise ValueError(f"{key} cannot contain newlines")
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = (
        path.read_text(encoding="utf-8-sig").splitlines()
        if path.is_file()
        else []
    )
    pending = dict(updates)
    output: list[str] = []
    for line in existing:
        stripped = line.lstrip()
        replaced = False
        if stripped and not stripped.startswith("#") and "=" in stripped:
            current_key = stripped.split("=", 1)[0].strip()
            if current_key in pending:
                escaped = pending.pop(current_key).replace("\\", "\\\\").replace('"', '\\"')
                output.append(f'{current_key}="{escaped}"')
                replaced = True
        if not replaced:
            output.append(line)
    for key, value in pending.items():
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        output.append(f'{key}="{escaped}"')
    path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")


def _write_provider_settings(
    path: Path,
    *,
    kind: str,
    display_name: str,
    model: str,
    tool_transport: str,
    health_url: str,
    capabilities: dict | None = None,
    available_models: list[str] | None = None,
) -> None:
    yaml = YAML()
    yaml.preserve_quotes = True
    data = yaml.load(path.read_text(encoding="utf-8-sig")) or CommentedMap()
    llm = data.setdefault("llm", CommentedMap())
    provider = llm.setdefault("provider", CommentedMap())
    provider["kind"] = kind
    provider["display_name"] = display_name
    provider["health_url"] = health_url
    # Switching profiles must not inherit incompatible hand-tuned capabilities.
    provider.pop("capabilities", None)
    if capabilities:
        # A discovered profile records what the runtime actually declared, so
        # the agent never infers a critical capability from the model name.
        discovered = CommentedMap()
        for key, value in capabilities.items():
            discovered[key] = value
        provider["capabilities"] = discovered
    llm["main_model"] = model
    if available_models is not None:
        available = [item for item in available_models if item]
    else:
        available = list(llm.get("available_models") or [])
    if model not in available:
        available.insert(0, model)
    llm["available_models"] = available
    llm["tool_transport"] = tool_transport
    llm["is_multimodal"] = (
        bool(capabilities.get("vision")) if capabilities else kind == "qwb"
    )
    with path.open("w", encoding="utf-8") as stream:
        yaml.dump(data, stream)


def save_provider_profile(
    *,
    kind: str,
    base_url: str,
    api_key: str,
    model: str,
    tool_transport: str,
    display_name: str = "",
    capabilities: dict | None = None,
    available_models: list[str] | None = None,
    settings_path: Path | None = None,
    env_path: Path | None = None,
) -> None:
    """Validate and persist one selected provider profile atomically by file."""

    paths = get_instance_paths()
    settings_file = settings_path or paths.config_dir / "settings.yaml"
    environment_file = env_path or paths.env_file
    if not settings_file.is_file():
        example = paths.config_dir / "settings.example.yaml"
        if not example.is_file():
            raise FileNotFoundError("settings.yaml and its template are missing")
        shutil.copy2(example, settings_file)

    config = LLMProviderConfig(
        kind=kind,
        display_name=display_name,
        capabilities=capabilities,
    )
    validate_provider_startup(
        config,
        api_url=base_url,
        api_keys=[api_key] if api_key else [],
        model=model,
        tool_transport=tool_transport,
    )
    health_url = _qwb_health_url(base_url) if kind == "qwb" else ""
    _write_provider_settings(
        settings_file,
        kind=kind,
        display_name=display_name,
        model=model,
        tool_transport=tool_transport,
        health_url=health_url,
        capabilities=capabilities,
        available_models=available_models,
    )
    _write_env_values(
        environment_file,
        {
            "LLM_API_URL": base_url.strip(),
            "LLM_API_KEY_1": api_key.strip() or "local_dummy_key",
        },
    )


def fetch_models(base_url: str, api_key: str, *, timeout: float = 8.0) -> list[str]:
    """List models for any endpoint and key, from synchronous CLI code."""

    return asyncio.run(list_models(base_url, api_key, timeout=timeout))


def _pick_model(
    base_url: str,
    api_key: str,
    *,
    current_model: str,
    style,
) -> tuple[str, list[str]]:
    """Offer the endpoint's real model list, with a manual fallback.

    Returns the chosen model and every model the endpoint advertised, so the
    saved profile records reality instead of a hand-typed guess.
    """

    try:
        models = fetch_models(base_url, api_key)
    except Exception as exc:  # network, auth, or a non-conforming endpoint
        print_error(
            f" Could not list models ({type(exc).__name__}). Enter one manually."
        )
        models = []

    if not models:
        typed = questionary.text(
            "Exact model name:",
            default=current_model,
            style=style,
        ).ask()
        return (typed or "").strip(), []

    choices = [
        questionary.Choice(
            f"{name}  [current]" if name == current_model else name, name
        )
        for name in models
    ]
    choices.append(questionary.Choice("Enter a different name manually", "__manual__"))
    selected = questionary.select(
        f"Model ({len(models)} available at this endpoint):",
        choices=choices,
        default=current_model if current_model in models else None,
        style=style,
        qmark="",
    ).ask()
    if not selected:
        return "", models
    if selected == "__manual__":
        typed = questionary.text(
            "Exact model name:",
            default=current_model,
            style=style,
        ).ask()
        return (typed or "").strip(), models
    return selected, models


def _describe_endpoint(endpoint: DiscoveredEndpoint) -> None:
    if not endpoint.reachable:
        console.print(
            f"[dim]{endpoint.label} ({endpoint.base_url}): "
            f"not running — {endpoint.detail}[/dim]"
        )
        return
    console.print(
        f"[bold green]{endpoint.label}[/bold green] {endpoint.base_url} "
        f"[dim]{endpoint.detail}, {endpoint.latency_ms}ms[/dim]"
    )
    for model in endpoint.models[:20]:
        console.print(f"  [cyan]•[/cyan] {model.summary()}")


def _use_local_runtime() -> None:
    """Scan Ollama and LM Studio, then configure a local coding model."""

    style = get_custom_style()
    print_info(" Scanning local runtimes (Ollama, LM Studio)...")
    endpoints = asyncio.run(scan_local_runtimes(timeout=5.0))
    for endpoint in endpoints:
        _describe_endpoint(endpoint)

    reachable = [item for item in endpoints if item.reachable and item.models]
    if not reachable:
        print_error(
            " No local OpenAI-compatible runtime answered. Start Ollama "
            "(`ollama serve`) or LM Studio's local server, then retry."
        )
        wait_for_enter()
        return

    suggested = recommend_coding_model(endpoints)
    choices: list[questionary.Choice] = []
    index: dict[str, tuple[DiscoveredEndpoint, DiscoveredModel]] = {}
    for endpoint in reachable:
        for model in endpoint.models:
            key = f"{endpoint.runtime}::{model.id}"
            index[key] = (endpoint, model)
            label = f"[{endpoint.label}] {model.summary()}"
            if suggested is not None and model.id == suggested.id:
                label = f"{label}  ← suggested for coding"
            choices.append(questionary.Choice(label, key))
    choices.append(questionary.Choice("Cancel", "__cancel__"))

    default_key = (
        f"{suggested.runtime}::{suggested.id}"
        if suggested is not None
        and f"{suggested.runtime}::{suggested.id}" in index
        else None
    )
    selected = questionary.select(
        "Local coding model:",
        choices=choices,
        default=default_key,
        style=style,
        qmark="",
    ).ask()
    if not selected or selected == "__cancel__":
        return

    endpoint, model = index[selected]
    capabilities = capabilities_for_model(model)
    transport = tool_transport_for_model(model)
    for warning in model.warnings:
        print_info(f" Note: {warning}.")
    console.print(
        f"[dim]Capabilities recorded from {endpoint.label}: "
        f"native_tools={capabilities['native_tools']}, "
        f"vision={capabilities['vision']}, "
        f"reasoning={capabilities['reasoning']}, "
        f"context_window={capabilities['context_window']}; "
        f"transport={transport}[/dim]"
    )
    if not questionary.confirm(
        f"Use {model.id} from {endpoint.label}?",
        default=True,
        style=style,
    ).ask():
        return

    save_provider_profile(
        kind="openai_compatible",
        base_url=endpoint.base_url,
        api_key=endpoint.api_key,
        model=model.id,
        tool_transport=transport,
        display_name=endpoint.label,
        capabilities=capabilities,
        available_models=[item.id for item in endpoint.models],
    )
    print_success(
        f" {endpoint.label} / {model.id} saved. It applies on the next agent start."
    )


def _configure() -> None:
    settings, _ = load_config()
    environment = _provider_environment()
    style = get_custom_style()
    kind = questionary.select(
        "Provider adapter:",
        choices=[
            questionary.Choice("QWB / Qwen Web bridge", "qwb"),
            questionary.Choice("OpenAI-compatible API", "openai_compatible"),
        ],
        default=settings.llm.provider.kind,
        style=style,
        qmark="",
    ).ask()
    if not kind:
        return
    default_url = environment.get("LLM_API_URL", "")
    if kind == "qwb" and not default_url:
        default_url = "http://127.0.0.1:8000/v1"
    base_url = questionary.text(
        "Base URL (blank means api.openai.com/v1 for standard provider):",
        default=default_url,
        style=style,
    ).ask()
    if base_url is None:
        return
    key_prompt = questionary.password(
        "API key (leave blank to preserve the current key):",
        style=style,
    ).ask()
    if key_prompt is None:
        return
    api_key = key_prompt.strip() or environment.get("LLM_API_KEY_1", "")
    if kind == "qwb" and not api_key:
        api_key = "local_dummy_key"
    model, discovered_models = _pick_model(
        base_url.strip(),
        api_key,
        current_model=settings.llm.main_model,
        style=style,
    )
    if not model:
        return
    transport = questionary.select(
        "Tool transport:",
        choices=[
            questionary.Choice("Auto by capability profile", "auto"),
            questionary.Choice("JAWL JSON action envelope", "json_envelope"),
            questionary.Choice("Native provider tool_calls", "native"),
        ],
        default=settings.llm.tool_transport,
        style=style,
        qmark="",
    ).ask()
    if not transport:
        return
    display_name = questionary.text(
        "Display name (optional):",
        default=settings.llm.provider.display_name,
        style=style,
    ).ask()
    if display_name is None:
        return
    save_provider_profile(
        kind=kind,
        base_url=base_url.strip(),
        api_key=api_key,
        model=model.strip(),
        tool_transport=transport,
        display_name=display_name.strip(),
        available_models=discovered_models or None,
    )
    print_success(" Provider profile saved. It will apply on the next agent start.")


def provider_screen() -> None:
    set_window_title("JAWL - LLM Providers")
    if _is_agent_running():
        print_error("Stop the agent before switching its LLM provider.")
        wait_for_enter()
        return

    while True:
        draw_header()
        settings, _ = load_config()
        health = _provider_health(timeout=2.0)
        capabilities = settings.llm.provider.resolved_capabilities()
        console.print(
            f"[bold cyan]Provider[/bold cyan]  "
            f"{settings.llm.provider.display_name or settings.llm.provider.kind}\n"
            f"[bold cyan]Model[/bold cyan]     {settings.llm.main_model}\n"
            f"[bold cyan]Transport[/bold cyan] {settings.llm.tool_transport}\n"
            f"[bold cyan]Health[/bold cyan]    {health.get('status', 'offline')}\n"
            f"[dim]Capabilities: {capabilities}[/dim]"
        )
        choice = questionary.select(
            "Provider controls:",
            choices=[
                questionary.Choice(
                    "Scan local runtimes (Ollama / LM Studio)", "scan"
                ),
                questionary.Choice("Configure / switch provider", "configure"),
                questionary.Choice("Refresh health", "refresh"),
                questionary.Choice("Back", "back"),
            ],
            style=get_custom_style(),
            qmark="",
        ).ask()
        if choice in {None, "back"}:
            return
        if choice == "scan":
            try:
                _use_local_runtime()
            except (OSError, ValueError) as exc:
                print_error(str(exc))
                time.sleep(1.5)
        elif choice == "configure":
            try:
                _configure()
            except (OSError, ValueError) as exc:
                print_error(str(exc))
                time.sleep(1.5)
        elif choice == "refresh":
            print_info(" Provider health refreshed.")

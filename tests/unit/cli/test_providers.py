from pathlib import Path
from unittest.mock import patch

import pytest
from ruamel.yaml import YAML

from src.cli.screens.providers import (
    _pick_model,
    _qwb_health_url,
    _write_env_values,
    save_provider_profile,
)
from src.l3_agent.llm.providers.discovery import (
    DiscoveredModel,
    capabilities_for_model,
    tool_transport_for_model,
)
from src.l3_agent.llm.providers.factory import validate_provider_startup
from src.utils.settings import LLMConfig, LLMProviderConfig, SettingsConfig


def _settings(path: Path) -> None:
    path.write_text(
        """identity:
  agent_name: Test
llm:
  main_model: old-model
  available_models: [old-model]
  tool_transport: wrapper
system: {}
""",
        encoding="utf-8",
    )


def test_legacy_tool_transport_migrates_and_capabilities_are_profile_based():
    assert LLMConfig(tool_transport="wrapper").tool_transport == "json_envelope"
    assert LLMConfig(tool_transport="hybrid").tool_transport == "auto"
    assert LLMProviderConfig(kind="qwb").resolved_capabilities()["vision"] is True
    assert (
        LLMProviderConfig(kind="openai_compatible")
        .resolved_capabilities()["server_side_conversation"]
        is False
    )


def test_startup_validation_rejects_static_errors_without_secret_values():
    with pytest.raises(ValueError, match="main_model"):
        validate_provider_startup(
            LLMProviderConfig(kind="qwb"),
            api_url="http://127.0.0.1:8000/v1",
            api_keys=["secret"],
            model="unknown",
            tool_transport="json_envelope",
        )
    with pytest.raises(ValueError, match="API_KEY") as caught:
        validate_provider_startup(
            LLMProviderConfig(kind="openai_compatible"),
            api_url="https://cloud.example/v1",
            api_keys=[],
            model="model",
            tool_transport="auto",
        )
    assert "secret" not in str(caught.value)


def test_env_update_appends_missing_keys_and_preserves_unrelated_values(tmp_path):
    path = tmp_path / ".env"
    path.write_text("OTHER=value\nLLM_API_URL=old\n", encoding="utf-8")

    _write_env_values(
        path,
        {"LLM_API_URL": "http://localhost:9000/v1", "LLM_API_KEY_1": "s-e-c-r-e-t"},
    )

    content = path.read_text(encoding="utf-8")
    assert "OTHER=value" in content
    assert 'LLM_API_URL="http://localhost:9000/v1"' in content
    assert 'LLM_API_KEY_1="s-e-c-r-e-t"' in content


def test_save_provider_profile_switches_without_putting_secret_in_yaml(tmp_path):
    settings_path = tmp_path / "settings.yaml"
    env_path = tmp_path / ".env"
    _settings(settings_path)

    save_provider_profile(
        kind="openai_compatible",
        base_url="http://127.0.0.1:9090/v1",
        api_key="private-key",
        model="coding-model",
        tool_transport="auto",
        display_name="Local API",
        settings_path=settings_path,
        env_path=env_path,
    )

    yaml = YAML(typ="safe")
    data = yaml.load(settings_path.read_text(encoding="utf-8"))
    assert data["llm"]["provider"]["kind"] == "openai_compatible"
    assert data["llm"]["provider"]["display_name"] == "Local API"
    assert data["llm"]["main_model"] == "coding-model"
    assert data["llm"]["tool_transport"] == "auto"
    assert "private-key" not in settings_path.read_text(encoding="utf-8")
    assert "private-key" in env_path.read_text(encoding="utf-8")
    parsed = SettingsConfig.model_validate(data)
    assert parsed.llm.provider.kind == "openai_compatible"


def test_qwb_health_url_follows_non_default_bridge_base_url():
    assert _qwb_health_url("http://localhost:8123/v1") == "http://localhost:8123/health"


def test_model_picker_offers_the_endpoint_list_and_records_every_model():
    with patch(
        "src.cli.screens.providers.fetch_models",
        return_value=["model-a", "model-b"],
    ), patch("src.cli.screens.providers.questionary.select") as select:
        select.return_value.ask.return_value = "model-b"
        chosen, available = _pick_model(
            "http://127.0.0.1:11434/v1",
            "local_dummy_key",
            current_model="model-a",
            style=None,
        )

    assert chosen == "model-b"
    assert available == ["model-a", "model-b"]
    # The current model must be selectable, plus an explicit manual escape.
    values = [choice.value for choice in select.call_args.kwargs["choices"]]
    assert values == ["model-a", "model-b", "__manual__"]


def test_model_picker_falls_back_to_manual_entry_when_listing_fails():
    with patch(
        "src.cli.screens.providers.fetch_models",
        side_effect=OSError("connection refused"),
    ), patch("src.cli.screens.providers.questionary.text") as text, patch(
        "src.cli.screens.providers.print_error"
    ) as reported:
        text.return_value.ask.return_value = " typed-model "
        chosen, available = _pick_model(
            "https://cloud.example/v1",
            "secret-key",
            current_model="old",
            style=None,
        )

    assert chosen == "typed-model"
    assert available == []
    # A failure must be explained without echoing the credential.
    assert "secret-key" not in str(reported.call_args)


def test_discovered_local_profile_persists_declared_capabilities(tmp_path):
    settings_path = tmp_path / "settings.yaml"
    env_path = tmp_path / ".env"
    _settings(settings_path)
    model = DiscoveredModel(
        id="gemma-4-12b-coder:latest",
        runtime="ollama",
        base_url="http://127.0.0.1:11434/v1",
        native_tools=True,
        reasoning=True,
        context_window=262144,
    )

    save_provider_profile(
        kind="openai_compatible",
        base_url=model.base_url,
        api_key="local_dummy_key",
        model=model.id,
        tool_transport=tool_transport_for_model(model),
        display_name="Ollama",
        capabilities=capabilities_for_model(model),
        available_models=[model.id, "other:latest"],
        settings_path=settings_path,
        env_path=env_path,
    )

    data = YAML(typ="safe").load(settings_path.read_text(encoding="utf-8"))
    parsed = SettingsConfig.model_validate(data)
    assert parsed.llm.main_model == model.id
    assert parsed.llm.available_models == [model.id, "other:latest"]
    # Declared tool support enables the native transport; nothing is inferred.
    assert parsed.llm.tool_transport == "native"
    resolved = parsed.llm.provider.resolved_capabilities()
    assert resolved["native_tools"] is True
    assert resolved["reasoning"] is True
    assert resolved["context_window"] == 262144
    assert resolved["server_side_conversation"] is False
    # Vision was never declared, so multimodal input stays off.
    assert resolved["vision"] is False
    assert parsed.llm.is_multimodal is False


def test_local_profile_without_declared_tools_stays_on_the_json_envelope(tmp_path):
    settings_path = tmp_path / "settings.yaml"
    env_path = tmp_path / ".env"
    _settings(settings_path)
    model = DiscoveredModel(
        id="mystery:latest",
        runtime="ollama",
        base_url="http://127.0.0.1:11434/v1",
    )

    save_provider_profile(
        kind="openai_compatible",
        base_url=model.base_url,
        api_key="local_dummy_key",
        model=model.id,
        tool_transport=tool_transport_for_model(model),
        display_name="Ollama",
        capabilities=capabilities_for_model(model),
        settings_path=settings_path,
        env_path=env_path,
    )

    parsed = SettingsConfig.model_validate(
        YAML(typ="safe").load(settings_path.read_text(encoding="utf-8"))
    )
    assert parsed.llm.tool_transport == "json_envelope"
    assert parsed.llm.provider.resolved_capabilities()["native_tools"] is False

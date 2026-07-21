import pytest
import yaml
from pathlib import Path
from yaml.constructor import ConstructorError
from unittest.mock import patch

from src.utils.settings import (
    load_yaml,
    load_config,
    HostOSConfig,
    TelethonConfig,
    AiogramConfig,
    LifecycleCommandHookConfig,
    MCPConfig,
    MCPServerConfig,
    _log_missing_defaults,
    SystemConfig,
)


def test_mcp_config_enforces_transport_and_authorization_boundaries():
    stdio = MCPServerConfig(
        name="local",
        command="python",
        args=["server.py"],
        allowed_tools=["search"],
    )
    assert stdio.transport == "stdio"
    assert stdio.allowed_tools == ["search"]

    remote = MCPServerConfig(
        name="remote",
        transport="streamable_http",
        url="https://mcp.example.com/mcp",
        bearer_token_env="MCP_TOKEN",
    )
    assert remote.url == "https://mcp.example.com/mcp"

    with pytest.raises(ValueError, match="require HTTPS"):
        MCPServerConfig(
            name="remote",
            transport="streamable_http",
            url="http://mcp.example.com/mcp",
        )
    with pytest.raises(ValueError, match="credentials"):
        MCPServerConfig(
            name="remote",
            transport="streamable_http",
            url="https://user:secret@mcp.example.com/mcp",
        )
    with pytest.raises(ValueError, match="Invalid MCP header"):
        MCPServerConfig(
            name="remote",
            transport="streamable_http",
            url="https://mcp.example.com/mcp",
            headers_from_env={"Authorization": "TOKEN"},
        )
    with pytest.raises(ValueError, match="must be unique"):
        MCPConfig(
            servers=[
                {"name": "duplicate", "command": "python"},
                {"name": "duplicate", "command": "python"},
            ]
        )


def test_load_yaml_success(tmp_path: Path):
    """Test: load_yaml parses YAML correctly."""
    test_file = tmp_path / "test.yaml"

    test_data = {"host_pc": {"enabled": True, "access_level": 2}}
    with open(test_file, "w", encoding="utf-8") as f:
        yaml.dump(test_data, f)

    result = load_yaml(test_file)
    assert result["host_pc"]["access_level"] == 2


def test_load_yaml_file_not_found():
    """Test: FileNotFound raises correct exception."""
    fake_path = Path("/this/file/does/not/exist.yaml")

    with pytest.raises(FileNotFoundError, match="Configuration file not found"):
        load_yaml(fake_path)


def test_host_os_config_parsing():
    """Test: Pydantic model parses valid data."""
    data = {
        "enabled": True,
        "access_level": 3,
        "env_access": False,
        "monitoring_interval_sec": 30,
        "execution_timeout_sec": 60,
        "file_read_max_chars": 5000,
        "file_list_limit": 100,
        "http_response_max_chars": 5000,
        "top_processes_limit": 10,
    }
    config = HostOSConfig(**data)

    assert config.access_level == 3
    assert config.enabled is True
    assert config.file_read_max_chars == 5000
    assert config.desktop_max_elements == 250

    with pytest.raises(ValueError):
        HostOSConfig(desktop_max_result_chars=1000)


def test_host_os_config_validation():
    """Test: ValidationError on wrong types."""
    data = {
        "enabled": True,
        "access_level": 3,
        "env_access": False,
        "monitoring_interval_sec": "not_a_number",
        "execution_timeout_sec": 60,
        "file_read_max_chars": 5000,
        "file_list_limit": 100,
        "http_response_max_chars": 5000,
        "top_processes_limit": 10,
    }
    with pytest.raises(ValueError):
        HostOSConfig(**data)

    assert HostOSConfig(coding_approval_mode="disabled").coding_approval_mode == (
        "disabled"
    )
    with pytest.raises(ValueError):
        HostOSConfig(coding_approval_mode=False)
    with pytest.raises(ValueError, match="profile names must be unique"):
        HostOSConfig(
            coding_command_profiles=[
                {"name": "tests", "argv": ["python", "-m", "pytest"]},
                {"name": "tests", "argv": ["python", "-m", "pytest", "-q"]},
            ]
        )
    with pytest.raises(ValueError, match="container profile names must be unique"):
        HostOSConfig(
            coding_container_profiles=[
                {"name": "ci", "image": "python:3.12-slim"},
                {"name": "ci", "image": "python:3.13-slim"},
            ]
        )
    with pytest.raises(ValueError, match="unknown container profiles"):
        HostOSConfig(
            coding_command_profiles=[
                {
                    "name": "tests",
                    "argv": ["python", "-m", "pytest"],
                    "container_profile": "missing",
                }
            ]
        )
    assert TelethonConfig(coding_approval_chat_id=" 123 ").coding_approval_chat_id == (
        "123"
    )
    assert AiogramConfig(coding_approval_chat_id=-100123).coding_approval_chat_id == (
        -100123
    )
    with pytest.raises(ValueError, match="non-empty bounded chat ID"):
        TelethonConfig(coding_approval_chat_id="  ")
    for config_type in (TelethonConfig, AiogramConfig):
        with pytest.raises(ValueError, match="require numeric"):
            config_type(coding_approval_remote_decisions=True)
        with pytest.raises(ValueError, match="require numeric"):
            config_type(
                coding_approval_chat_id="123",
                coding_approval_actor_id=456,
                coding_approval_remote_decisions=True,
            )
        with pytest.raises(ValueError):
            config_type(
                coding_approval_chat_id=True,
                coding_approval_actor_id=True,
                coding_approval_remote_decisions=True,
            )
        remote = config_type(
            coding_approval_chat_id=-100123,
            coding_approval_actor_id=456,
            coding_approval_remote_decisions=True,
        )
        assert remote.coding_approval_chat_id == -100123
        assert remote.coding_approval_actor_id == 456


def test_lifecycle_command_hook_config_is_exact_argv_not_shell_text():
    profile = LifecycleCommandHookConfig(
        name="python-tests",
        phase="post_tool_use",
        argv=["python", "-m", "pytest", "-q"],
        scope="repository",
        working_directory="workspace",
    )

    assert profile.argv == ["python", "-m", "pytest", "-q"]
    with pytest.raises(ValueError):
        LifecycleCommandHookConfig(
            name="invalid-shell-text",
            phase="post_tool_use",
            argv="python -m pytest",
        )


def test_lifecycle_command_hook_config_accepts_cross_boundary_phases():
    phases = {
        "pre_context_compaction",
        "post_context_compaction",
        "pre_system_stop",
        "post_system_stop",
        "pre_delegation",
        "post_delegation",
        "delegation_error",
        "delegation_cancelled",
    }

    assert {
        LifecycleCommandHookConfig(
            name=f"hook-{index}", phase=phase, argv=["python", "hook.py"]
        ).phase
        for index, phase in enumerate(sorted(phases))
    } == phases


def test_load_yaml_duplicate_keys(tmp_path: Path):
    """Test: duplicate keys raise ConstructorError."""
    test_file = tmp_path / "test_dup.yaml"

    test_data = """
system:
  timezone: 3
system:
  timezone: 5
    """
    test_file.write_text(test_data.strip(), encoding="utf-8")

    with pytest.raises(ConstructorError, match="Duplicate key 'system'"):
        load_yaml(test_file)


def test_log_missing_defaults():
    """Test: recursively logs missing keys set to defaults."""

    partial_config = SystemConfig.model_validate({"timezone": 3})

    with patch("src.utils.settings.main_logger.debug") as mock_debug:
        _log_missing_defaults(partial_config, prefix="", file_name="settings.yaml")

        assert mock_debug.call_count >= 1

        log_messages = " ".join([call[0][0] for call in mock_debug.call_args_list])

        assert "settings.yaml" in log_messages
        assert "'heartbeat_interval'" in log_messages
        assert "'continuous_cycle'" in log_messages
        assert "'event_acceleration.critical_multiplier'" in log_messages


def test_load_config_auto_recover(tmp_path):
    """Test: load_config recovers .yaml files from .example.yaml."""

    with patch("src.utils.settings.Path.cwd", return_value=tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        settings_example = config_dir / "settings.example.yaml"
        settings_example.write_text(
            "identity:\n  agent_name: 'RecoveredAgent'\n", encoding="utf-8"
        )

        interfaces_example = config_dir / "interfaces.example.yaml"
        interfaces_example.write_text("host:\n  os:\n    enabled: true\n", encoding="utf-8")

        assert not (config_dir / "settings.yaml").exists()

        settings, interfaces = load_config()

        assert (config_dir / "settings.yaml").exists()
        assert (config_dir / "interfaces.yaml").exists()

        assert settings.identity.agent_name == "RecoveredAgent"
        assert interfaces.host.os.enabled is True

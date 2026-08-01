import pytest
from types import SimpleNamespace
from unittest.mock import patch, MagicMock, AsyncMock

from src.cli.screens.agent_control import (
    _is_agent_running,
    _check_and_setup_prompts,
)


@patch("src.cli.screens.agent_control.is_agent_running")
def test_is_agent_running_proxy(mock_global_is_running):
    """Тест: локальная функция просто проксирует вызов в утилиты."""
    mock_global_is_running.return_value = True
    assert _is_agent_running() is True
    mock_global_is_running.assert_called_once()


@patch("src.cli.screens.agent_control.PROMPTS_DIR")
def test_check_and_setup_prompts(mock_prompts_dir, tmp_path):
    """Тест: копирование файлов личности из шаблонов."""
    test_dir = tmp_path / "prompts"
    test_dir.mkdir()
    (test_dir / "SOUL.example.md").touch()

    mock_prompts_dir.exists.return_value = True
    mock_prompts_dir.rglob.return_value = [test_dir / "SOUL.example.md"]

    with patch("src.cli.screens.agent_control.shutil.copy") as mock_copy:
        _check_and_setup_prompts()
        mock_copy.assert_called_once()
        assert "SOUL.md" in str(mock_copy.call_args[0][1])


def test_telethon_preflight_uses_shared_resilient_client(tmp_path):
    from src.cli.screens.agent_control import _telethon_auth_flow

    settings = SimpleNamespace(system=SimpleNamespace(timezone=3))
    interfaces = SimpleNamespace(
        telegram=SimpleNamespace(
            telethon=SimpleNamespace(enabled=True, session_name="test_session")
        )
    )
    instance_paths = SimpleNamespace(data_dir=tmp_path)
    runtime_client = MagicMock()
    runtime_client.start = AsyncMock()
    runtime_client.stop = AsyncMock()
    runtime_client.client.return_value.get_me = AsyncMock(
        return_value=SimpleNamespace(first_name="Test", last_name=None)
    )

    with (
        patch(
            "src.cli.screens.agent_control.load_config",
            return_value=(settings, interfaces),
        ),
        patch(
            "src.cli.screens.agent_control.dotenv_values",
            return_value={"TELETHON_API_ID": "123", "TELETHON_API_HASH": "hash"},
        ),
        patch("src.cli.screens.agent_control.INSTANCE_PATHS", instance_paths),
        patch(
            "src.cli.screens.agent_control.TelethonClient",
            return_value=runtime_client,
        ) as client_class,
    ):
        assert _telethon_auth_flow() is True

    runtime_client.start.assert_awaited_once()
    runtime_client.stop.assert_awaited_once()
    assert client_class.call_args.kwargs["session_path"].endswith("test_session")


def test_telethon_preflight_does_not_block_agent_when_saved_session_exists(
    tmp_path,
):
    from src.cli.screens.agent_control import _telethon_auth_flow

    settings = SimpleNamespace(system=SimpleNamespace(timezone=3))
    interfaces = SimpleNamespace(
        telegram=SimpleNamespace(
            telethon=SimpleNamespace(enabled=True, session_name="saved")
        )
    )
    session_dir = tmp_path / "interfaces" / "telegram" / "telethon"
    session_dir.mkdir(parents=True)
    (session_dir / "saved.session").touch()
    runtime_client = MagicMock()
    runtime_client.start = AsyncMock(side_effect=ConnectionError("offline"))
    runtime_client.stop = AsyncMock()

    with (
        patch(
            "src.cli.screens.agent_control.load_config",
            return_value=(settings, interfaces),
        ),
        patch(
            "src.cli.screens.agent_control.dotenv_values",
            return_value={"TELETHON_API_ID": "123", "TELETHON_API_HASH": "hash"},
        ),
        patch(
            "src.cli.screens.agent_control.INSTANCE_PATHS",
            SimpleNamespace(data_dir=tmp_path),
        ),
        patch(
            "src.cli.screens.agent_control.TelethonClient",
            return_value=runtime_client,
        ),
    ):
        assert _telethon_auth_flow() is True

    runtime_client.stop.assert_awaited_once()

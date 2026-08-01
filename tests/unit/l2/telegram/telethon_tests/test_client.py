import pytest
from unittest.mock import patch, MagicMock, AsyncMock


@pytest.mark.asyncio
@patch("src.l2_interfaces.telegram.telethon.client.TelegramClient")
@patch("src.l2_interfaces.telegram.telethon.client.parse_proxy_url")
async def test_telethon_client_with_proxy(mock_parse_proxy, mock_tg_client_cls):
    """Тест: TelethonClient корректно парсит прокси и отдает его в TelegramClient."""
    from src.l2_interfaces.telegram.telethon.client import TelethonClient
    from src.l2_interfaces.telegram.telethon.state import TelethonState

    mock_parse_proxy.return_value = (3, "127.0.0.1", 9050, True, "user", "pass")

    client = TelethonClient(
        state=TelethonState(),
        api_id=123,
        api_hash="hash",
        session_path="session",
        timezone=3,
        proxy_url="socks5://user:pass@127.0.0.1:9050",
    )

    # Мокаем внутренние вызовы
    mock_instance = MagicMock()
    mock_instance.get_me = AsyncMock(return_value=MagicMock(username="TestBot"))
    mock_instance.start = AsyncMock()  # ФИКС: Явно указываем, что start это корутина
    mock_tg_client_cls.return_value = mock_instance
    client.update_profile_state = AsyncMock()

    await client.start()

    # Проверяем, что парсер был вызван
    mock_parse_proxy.assert_called_once_with("socks5://user:pass@127.0.0.1:9050")

    # Проверяем, что в TelegramClient улетел аргумент proxy
    call_kwargs = mock_tg_client_cls.call_args[1]
    assert "proxy" in call_kwargs
    assert call_kwargs["proxy"] == mock_parse_proxy.return_value


@pytest.mark.asyncio
@patch("src.l2_interfaces.telegram.telethon.client.TelegramClient")
async def test_telethon_client_without_proxy(mock_tg_client_cls):
    """Тест: TelethonClient работает без прокси, если URL не передан."""
    from src.l2_interfaces.telegram.telethon.client import TelethonClient
    from src.l2_interfaces.telegram.telethon.state import TelethonState

    client = TelethonClient(
        state=TelethonState(),
        api_id=123,
        api_hash="hash",
        session_path="session",
        timezone=3,
        proxy_url=None,
    )

    mock_instance = MagicMock()
    mock_instance.get_me = AsyncMock(return_value=MagicMock(username="TestBot"))
    mock_instance.start = AsyncMock()  # ФИКС: Явно указываем, что start это корутина
    mock_tg_client_cls.return_value = mock_instance
    client.update_profile_state = AsyncMock()

    await client.start()

    # Проверяем, что proxy=None
    call_kwargs = mock_tg_client_cls.call_args[1]
    assert call_kwargs.get("proxy") is None


@pytest.mark.asyncio
@patch("src.l2_interfaces.telegram.telethon.client.TelegramClient")
async def test_failed_proxy_is_disconnected_before_direct_fallback(
    mock_tg_client_cls, tmp_path
):
    from src.l2_interfaces.telegram.telethon.client import TelethonClient
    from src.l2_interfaces.telegram.telethon.state import TelethonState
    from src.l2_interfaces.telegram.telethon.proxy_manager import MTProxyManager

    failed = MagicMock()
    failed.start = AsyncMock(side_effect=TimeoutError("connect timeout"))
    failed.disconnect = AsyncMock()
    direct = MagicMock()
    direct.start = AsyncMock()
    direct.get_me = AsyncMock(return_value=MagicMock(username="TestBot"))
    mock_tg_client_cls.side_effect = [failed, direct]
    manager = MTProxyManager(cache_dir=tmp_path)
    manager.discover_candidates = AsyncMock(return_value=[])
    manager.select_first_working = AsyncMock(return_value=None)
    client = TelethonClient(
        state=TelethonState(),
        api_id=123,
        api_hash="hash",
        session_path=str(tmp_path / "session"),
        timezone=3,
        proxy_url=(
            "tg://proxy?server=dead.example&port=443&secret="
            "ee00112233445566778899aabbccddeeff"
        ),
        proxy_manager=manager,
    )
    client.update_profile_state = AsyncMock()

    await client.start()

    failed.disconnect.assert_awaited_once()
    assert mock_tg_client_cls.call_count == 2
    assert mock_tg_client_cls.call_args_list[1].kwargs["proxy"] is None
    assert client.state.is_online is True


@pytest.mark.asyncio
@patch("src.l2_interfaces.telegram.telethon.client.TelegramClient")
async def test_stale_configured_proxy_uses_discovered_route_before_direct(
    mock_tg_client_cls, tmp_path
):
    from src.l2_interfaces.telegram.telethon.client import TelethonClient
    from src.l2_interfaces.telegram.telethon.state import TelethonState
    from src.l2_interfaces.telegram.telethon.proxy_manager import MTProxyManager

    stale_proxy = (
        "tg://proxy?server=dead.example&port=443&secret="
        "ee00112233445566778899aabbccddeeff"
    )
    working_proxy = (
        "tg://proxy?server=live.example&port=8443&secret="
        "ddffeeddccbbaa99887766554433221100"
    )
    failed = MagicMock()
    failed.start = AsyncMock(side_effect=TimeoutError("connect timeout"))
    failed.disconnect = AsyncMock()
    working = MagicMock()
    working.start = AsyncMock()
    working.get_me = AsyncMock(return_value=MagicMock(username="TestBot"))
    mock_tg_client_cls.side_effect = [failed, working]

    manager = MTProxyManager(cache_dir=tmp_path)
    manager.discover_candidates = AsyncMock(return_value=[working_proxy])
    manager.select_first_working = AsyncMock(return_value=working_proxy)
    client = TelethonClient(
        state=TelethonState(),
        api_id=123,
        api_hash="hash",
        session_path=str(tmp_path / "session"),
        timezone=3,
        proxy_url=stale_proxy,
        proxy_manager=manager,
    )
    client.update_profile_state = AsyncMock()

    await client.start()

    manager.discover_candidates.assert_awaited_once()
    manager.select_first_working.assert_awaited_once()
    assert mock_tg_client_cls.call_count == 2
    assert mock_tg_client_cls.call_args_list[1].kwargs["proxy"][0] == "live.example"
    assert manager.load_cached() == working_proxy
    assert client.state.is_online is True


def test_proxy_log_label_redacts_mtproxy_secret() -> None:
    from src.l2_interfaces.telegram.telethon.client import TelethonClient

    proxy = (
        "tg://proxy?server=proxy.example&port=443&secret="
        "ee00112233445566778899aabbccddeeff"
    )
    label = TelethonClient._proxy_label(proxy)

    assert label == "proxy.example:443 (MTProxy)"
    assert "secret" not in label
    assert "001122" not in label

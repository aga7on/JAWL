from unittest.mock import MagicMock

from src.l2_interfaces.host.os.plugin import HostOsPlugin
from src.system.container import SystemContainer
from src.utils.settings import SettingsConfig, InterfacesConfig
from src.utils.event.bus import EventBus
from src.l3_agent.context.registry import ContextRegistry


def test_host_os_plugin_setup(tmp_path):
    """
    Тест: HostOsPlugin корректно инициализирует стейт, клиентов и регистрирует
    провайдер контекста в ядре системы.
    """
    
    settings = SettingsConfig()
    interfaces = InterfacesConfig()
    interfaces.host.os.enabled = True

    container = SystemContainer(settings, interfaces, EventBus())
    container.root_dir = tmp_path
    container.local_data_dir = tmp_path / "data"
    container.context_registry = MagicMock(spec=ContextRegistry)

    plugin = HostOsPlugin()

    # Проверяем флаг включения
    assert plugin.is_enabled(interfaces) is True

    # Запускаем сборку
    lifecycle_components = plugin.setup(container, env_vars={})

    # ПРОВЕРКИ ИНВЕРСИИ ЗАВИСИМОСТЕЙ (DI):
    # 1. Стейт должен быть уложен в словарь l0_states, а не как магический атрибут
    assert "os" in container.l0_states

    # 2. Компоненты жизненного цикла (Events) должны быть возвращены
    assert len(lifecycle_components) == 2
    assert lifecycle_components[0].__class__.__name__ == "HostOSEvents"
    assert lifecycle_components[1].__class__.__name__ == (
        "HostOSCodingLanguageServer"
    )
    assert container.coding_lsp is lifecycle_components[1]

    # 3. Контекст должен быть зарегистрирован в реестре ядра
    container.context_registry.register_provider.assert_called_once()
    call_args = container.context_registry.register_provider.call_args[1]
    assert call_args["name"] == "host_os"


def test_host_os_plugin_adds_approval_notifier_only_for_desktop_opt_in(tmp_path):
    settings = SettingsConfig()
    interfaces = InterfacesConfig()
    interfaces.host.os.enabled = True
    interfaces.host.os.desktop_interactions = True
    container = SystemContainer(settings, interfaces, EventBus())
    container.root_dir = tmp_path
    container.local_data_dir = tmp_path / "data"
    container.context_registry = MagicMock(spec=ContextRegistry)

    lifecycle = HostOsPlugin().setup(container, env_vars={})

    assert [item.__class__.__name__ for item in lifecycle] == [
        "HostOSEvents",
        "HostOSCodingLanguageServer",
        "HostOSCodingApprovalNotifications",
    ]

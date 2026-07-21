from unittest.mock import Mock, patch

from src.l2_interfaces.mcp.plugin import MCPPlugin
from src.utils.settings import InterfacesConfig


def test_mcp_plugin_registers_lifecycle_client_and_payload_free_context(tmp_path):
    container = Mock()
    container.root_dir = tmp_path
    container.interfaces_config = InterfacesConfig.model_validate(
        {
            "mcp": {
                "enabled": True,
                "servers": [
                    {
                        "name": "local",
                        "command": "python",
                        "allowed_tools": ["echo"],
                    }
                ],
            }
        }
    )
    container.l0_states = {}
    container.l2_clients = {}
    container.context_registry = Mock()

    with patch("src.l2_interfaces.mcp.plugin.register_instance") as register:
        components = MCPPlugin().setup(container, {})

    assert components == [container.l2_clients["mcp"]]
    assert container.l0_states["mcp"].snapshot() == []
    register.assert_called_once()
    provider = container.context_registry.register_provider.call_args.args[1]
    assert provider.__self__ is container.l2_clients["mcp"]

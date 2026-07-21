"""Model Context Protocol interface plugin."""

from typing import Any, Dict, List, Optional

from src.l2_interfaces.base import BaseInterface
from src.l2_interfaces.mcp.client import MCPClientManager
from src.l2_interfaces.mcp.skills import MCPTools
from src.l2_interfaces.mcp.state import MCPState
from src.l3_agent.context.registry import ContextSection
from src.l3_agent.skills.registry import register_instance
from src.system.container import SystemContainer
from src.utils.logger import main_logger
from src.utils.settings import InterfacesConfig


class MCPPlugin(BaseInterface):
    @property
    def name(self) -> str:
        return "MCP"

    @property
    def description(self) -> str:
        return "Policy-controlled Model Context Protocol client."

    def is_enabled(self, config: InterfacesConfig) -> bool:
        return config.mcp.enabled

    def setup(
        self,
        container: SystemContainer,
        env_vars: Dict[str, Optional[str]],
    ) -> List[Any]:
        state = MCPState()
        container.l0_states["mcp"] = state
        client = MCPClientManager(
            container.interfaces_config.mcp,
            container.root_dir,
            state,
        )
        container.l2_clients["mcp"] = client
        register_instance(MCPTools(client))
        container.context_registry.register_provider(
            "mcp", client.get_context_block, ContextSection.INTERFACES
        )
        main_logger.info(
            f"[MCP] Interface loaded with {len(client.workers)} enabled servers."
        )
        return [client]

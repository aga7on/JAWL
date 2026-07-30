"""Native Debug Broker interface plugin."""

from typing import Any, Dict, List, Optional

from src.l2_interfaces.base import BaseInterface
from src.l2_interfaces.debug_broker.client import DebugBrokerClient
from src.l2_interfaces.debug_broker.skills import DebugBroker
from src.l3_agent.context.registry import ContextSection
from src.l3_agent.skills.registry import register_instance
from src.system.container import SystemContainer
from src.utils.logger import main_logger
from src.utils.settings import InterfacesConfig


class DebugBrokerPlugin(BaseInterface):
    @property
    def name(self) -> str:
        return "DEBUG BROKER"

    @property
    def description(self) -> str:
        return "Typed native debugger, instrumentation, and binary-analysis sessions."

    def is_enabled(self, config: InterfacesConfig) -> bool:
        return config.debug_broker.enabled

    def setup(
        self,
        container: SystemContainer,
        env_vars: Dict[str, Optional[str]],
    ) -> List[Any]:
        client = DebugBrokerClient(
            container.interfaces_config.debug_broker,
            container.root_dir,
            state_namespace=(
                None
                if container.instance_paths.legacy_default
                else container.instance_id
            ),
        )
        container.l2_clients["debug_broker"] = client
        register_instance(DebugBroker(client))
        container.context_registry.register_provider(
            "debug_broker",
            client.get_context_block,
            ContextSection.INTERFACES,
        )
        main_logger.info("[DebugBroker] Interface loaded.")
        return [client]

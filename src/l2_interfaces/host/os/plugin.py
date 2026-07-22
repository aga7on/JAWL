"""
Host OS interface plugin.
"""

from typing import List, Any, Dict, Optional

from src.utils.logger import main_logger
from src.l2_interfaces.base import BaseInterface
from src.l2_interfaces.host.os.state import HostOSState
from src.l2_interfaces.host.os.client import HostOSClient
from src.l2_interfaces.host.os.events import HostOSEvents
from src.l2_interfaces.host.os.coding_approvals import CodingApprovalStore
from src.l2_interfaces.host.os.coding_approval_notifications import (
    HostOSCodingApprovalNotifications,
)

from src.l2_interfaces.host.os.skills.execution import HostOSExecution
from src.l2_interfaces.host.os.skills.process_sessions import HostOSProcessSessions
from src.l2_interfaces.host.os.skills.monitoring import HostOSMonitoring
from src.l2_interfaces.host.os.skills.network import HostOSNetwork
from src.l2_interfaces.host.os.skills.desktop import HostOSDesktop
from src.l2_interfaces.host.os.skills.deploy import HostOSDeploy
from src.l2_interfaces.host.os.skills.files.reader import HostOSReader
from src.l2_interfaces.host.os.skills.files.writer import HostOSWriter
from src.l2_interfaces.host.os.skills.files.editor import HostOSEditor
from src.l2_interfaces.host.os.skills.files.search import HostOSSearch
from src.l2_interfaces.host.os.skills.files.archive import HostOSArchive
from src.l2_interfaces.host.os.skills.files.workspace import HostOSWorkspace
from src.l2_interfaces.host.os.skills.files.metadata import HostOSMetadata
from src.l2_interfaces.host.os.skills.files.documents import HostOSDocuments
from src.l2_interfaces.host.os.skills.coding_workspaces import HostOSCodingWorkspaces
from src.l2_interfaces.host.os.skills.coding_verification import HostOSCodingVerification
from src.l2_interfaces.host.os.skills.coding_context import HostOSCodingContext
from src.l2_interfaces.host.os.skills.coding_dependencies import HostOSCodingDependencies
from src.l2_interfaces.host.os.skills.coding_files import HostOSCodingFiles
from src.l2_interfaces.host.os.skills.coding_lsp import HostOSCodingLanguageServer
from src.l2_interfaces.host.os.skills.coding_plans import HostOSCodingPlans
from src.l2_interfaces.host.os.skills.coding_recovery import HostOSCodingRecovery
from src.l2_interfaces.host.os.skills.coding_execution import HostOSCodingExecution

from src.l3_agent.skills.registry import register_instance
from src.l3_agent.context.registry import ContextSection
from src.system.container import SystemContainer
from src.utils.settings import InterfacesConfig


class HostOsPlugin(BaseInterface):
    @property
    def name(self) -> str:
        return "HOST OS"

    @property
    def description(self) -> str:
        return "Host operating system access (files, processes, shell, network)."

    def is_enabled(self, config: InterfacesConfig) -> bool:
        return config.host.os.enabled

    def setup(
        self, container: SystemContainer, env_vars: Dict[str, Optional[str]]
    ) -> List[Any]:
        config = container.interfaces_config.host.os

        state = HostOSState()
        container.l0_states["os"] = state

        client = HostOSClient(
            base_dir=container.root_dir,
            config=config,
            state=state,
            timezone=container.settings.system.timezone,
        )

        events = HostOSEvents(
            host_os_client=client, state=state, event_bus=container.event_bus
        )

        execution = HostOSExecution(client)
        register_instance(execution)
        process_sessions = HostOSProcessSessions(
            client, execution, event_bus=container.event_bus
        )
        register_instance(process_sessions)
        register_instance(HostOSNetwork(client))
        register_instance(HostOSMonitoring(client, events))
        register_instance(HostOSDeploy(client))
        reader = HostOSReader(client)
        editor = HostOSEditor(client)
        search = HostOSSearch(client)
        coding_context = HostOSCodingContext(client)
        register_instance(reader)
        register_instance(HostOSWriter(client))
        register_instance(editor)
        register_instance(search)
        register_instance(HostOSArchive(client))
        register_instance(HostOSWorkspace(client))
        register_instance(HostOSMetadata(client))
        register_instance(HostOSDocuments(client))
        coding_workspaces = HostOSCodingWorkspaces(client, coding_context)
        container.coding_workspaces = coding_workspaces
        coding_approvals = CodingApprovalStore(
            client.system_dir / "coding_approvals.json"
        )
        container.coding_approvals = coding_approvals
        register_instance(coding_workspaces)
        register_instance(
            HostOSCodingFiles(
                client, coding_workspaces, reader, editor, search, coding_context
            )
        )
        coding_plans = HostOSCodingPlans(client, coding_workspaces)
        container.coding_plans = coding_plans
        register_instance(coding_plans)
        register_instance(
            HostOSCodingExecution(
                client,
                coding_workspaces,
                coding_approvals,
                event_bus=container.event_bus,
            )
        )
        # Normal bootstrap has L0/L1 ready before interfaces. Keeping this guard
        # also permits intentionally partial interface-only test containers.
        if container.sql is not None:
            register_instance(
                HostOSCodingRecovery(
                    client,
                    coding_workspaces,
                    container.sql.ticks,
                    container.agent_state,
                )
            )
        register_instance(
            HostOSCodingVerification(client, coding_workspaces, coding_plans)
        )
        register_instance(coding_context)
        coding_lsp = HostOSCodingLanguageServer(
            client,
            coding_context,
            workspaces=coding_workspaces,
            editor=editor,
        )
        container.coding_lsp = coding_lsp
        register_instance(coding_lsp)
        register_instance(HostOSCodingDependencies(client))

        approval_notifications = None
        if config.desktop_interactions:
            register_instance(HostOSDesktop(client))
            approval_notifications = HostOSCodingApprovalNotifications(
                container.event_bus
            )

        container.context_registry.register_provider(
            name="host_os",
            provider_func=client.get_context_block,
            section=ContextSection.INTERFACES,
        )

        main_logger.info("[Host OS] Interface loaded.")
        lifecycle = [events, coding_lsp, process_sessions]
        if approval_notifications is not None:
            lifecycle.append(approval_notifications)
        return lifecycle

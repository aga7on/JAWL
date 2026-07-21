"""Task-scoped file operations for managed coding workspaces."""

from __future__ import annotations

from typing import Dict, List, Optional

from src.l2_interfaces.host.os.client import HostOSAccessLevel, HostOSClient
from src.l2_interfaces.host.os.decorators import require_access
from src.l2_interfaces.host.os.skills.coding_workspaces import HostOSCodingWorkspaces
from src.l2_interfaces.host.os.skills.files.editor import HostOSEditor
from src.l2_interfaces.host.os.skills.files.reader import HostOSReader
from src.l2_interfaces.host.os.skills.files.search import HostOSSearch
from src.l3_agent.skills.registry import SkillResult, skill
from src.l3_agent.swarm.roles import Subagents


class HostOSCodingFiles:
    """Delegate bounded file tools through a stable task/workspace handle."""

    def __init__(
        self,
        host_os_client: HostOSClient,
        workspaces: HostOSCodingWorkspaces,
        reader: HostOSReader,
        editor: HostOSEditor,
        search: HostOSSearch,
    ) -> None:
        self.host_os = host_os_client
        self.workspaces = workspaces
        self.reader = reader
        self.editor = editor
        self.search = search

    @skill(swarm=[Subagents.CODER, Subagents.QA_ENGINEER])
    @require_access(HostOSAccessLevel.SANDBOX)
    async def read_coding_file_range(
        self,
        task_id: str,
        relative_path: str,
        start_line: int = 1,
        end_line: Optional[int] = None,
        max_lines: int = 400,
    ) -> SkillResult:
        """Read a numbered file range from a managed task workspace."""

        try:
            path = self.workspaces.resolve_workspace_path(
                task_id, relative_path, is_write=False
            )
            return await self.reader.read_file_range(
                str(path),
                start_line=start_line,
                end_line=end_line,
                max_lines=max_lines,
            )
        except (PermissionError, ValueError) as exc:
            return SkillResult.fail(str(exc))

    @skill(swarm=[Subagents.CODER, Subagents.QA_ENGINEER])
    @require_access(HostOSAccessLevel.SANDBOX)
    async def search_coding_workspace(
        self,
        task_id: str,
        query: str,
        regex: bool = False,
        case_sensitive: bool = False,
        globs: Optional[List[str]] = None,
        context_lines: int = 2,
        max_matches: int = 50,
    ) -> SkillResult:
        """Search only inside a managed task workspace."""

        try:
            workspace = self.workspaces.resolve_workspace_path(
                task_id, ".", is_write=False
            )
            return await self.search.search_repository(
                query=query,
                path=str(workspace),
                regex=regex,
                case_sensitive=case_sensitive,
                globs=globs,
                context_lines=context_lines,
                max_matches=max_matches,
            )
        except (PermissionError, ValueError) as exc:
            return SkillResult.fail(str(exc))

    @skill(swarm=[Subagents.CODER, Subagents.QA_ENGINEER])
    @require_access(HostOSAccessLevel.SANDBOX)
    async def apply_coding_file_patch(
        self,
        task_id: str,
        relative_path: str,
        edits: List[Dict[str, str]],
        expected_sha256: Optional[str] = None,
    ) -> SkillResult:
        """Atomically patch a task-relative file with the existing SHA guard."""

        try:
            path = self.workspaces.resolve_workspace_path(
                task_id, relative_path, is_write=True
            )
            return await self.editor.apply_file_patch(
                str(path), edits=edits, expected_sha256=expected_sha256
            )
        except (PermissionError, ValueError) as exc:
            return SkillResult.fail(str(exc))

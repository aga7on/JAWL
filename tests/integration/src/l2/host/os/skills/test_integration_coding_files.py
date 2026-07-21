import json
import re
import subprocess
from pathlib import Path

import pytest

from src.l2_interfaces.host.os.skills.coding_files import HostOSCodingFiles
from src.l2_interfaces.host.os.skills.coding_workspaces import HostOSCodingWorkspaces
from src.l2_interfaces.host.os.skills.files.editor import HostOSEditor
from src.l2_interfaces.host.os.skills.files.reader import HostOSReader
from src.l2_interfaces.host.os.skills.files.search import HostOSSearch


def git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, timeout=30
    )


@pytest.mark.asyncio
async def test_task_scoped_files_read_search_and_patch_without_workspace_path(
    os_client,
):
    repository = os_client.sandbox_dir / "task_files_repo"
    repository.mkdir()
    git(repository, "init", "-b", "main")
    git(repository, "config", "user.name", "Test User")
    git(repository, "config", "user.email", "test@example.com")
    (repository / "app.py").write_text("value = 1\n", encoding="utf-8")
    git(repository, "add", "--all")
    git(repository, "commit", "-m", "initial")
    workspaces = HostOSCodingWorkspaces(os_client)
    created = await workspaces.create_coding_workspace(
        "sandbox/task_files_repo", "task-files"
    )
    assert created.is_success is True, created.message
    files = HostOSCodingFiles(
        os_client,
        workspaces,
        HostOSReader(os_client),
        HostOSEditor(os_client),
        HostOSSearch(os_client),
    )

    read = await files.read_coding_file_range("task-files", "app.py")
    digest = re.search(r"SHA-256: ([0-9a-f]{64})", read.message)
    searched = await files.search_coding_workspace("task-files", "value")
    patched = await files.apply_coding_file_patch(
        "task-files",
        "app.py",
        edits=[{"search": "value = 1", "replace": "value = 2"}],
        expected_sha256=digest.group(1) if digest else None,
    )

    assert read.is_success is True
    assert "value = 1" in read.message
    assert digest is not None
    assert searched.is_success is True
    assert (
        json.loads(searched.message)["matches"][0]["path"].removeprefix("./")
        == "app.py"
    )
    assert patched.is_success is True, patched.message
    workspace = Path(json.loads(created.message)["workspace_path"])
    assert (workspace / "app.py").read_text(encoding="utf-8") == "value = 2\n"
    assert (repository / "app.py").read_text(encoding="utf-8") == "value = 1\n"


@pytest.mark.asyncio
async def test_task_scoped_files_reject_workspace_escape(os_client):
    repository = os_client.sandbox_dir / "escape_repo"
    repository.mkdir()
    git(repository, "init", "-b", "main")
    git(repository, "config", "user.name", "Test User")
    git(repository, "config", "user.email", "test@example.com")
    (repository / "app.py").write_text("value = 1\n", encoding="utf-8")
    git(repository, "add", "--all")
    git(repository, "commit", "-m", "initial")
    workspaces = HostOSCodingWorkspaces(os_client)
    await workspaces.create_coding_workspace("sandbox/escape_repo", "escape")
    files = HostOSCodingFiles(
        os_client,
        workspaces,
        HostOSReader(os_client),
        HostOSEditor(os_client),
        HostOSSearch(os_client),
    )

    escaped = await files.read_coding_file_range("escape", "../outside.txt")
    git_metadata = await files.read_coding_file_range("escape", ".git")

    assert escaped.is_success is False
    assert "inside the managed workspace" in escaped.message
    assert git_metadata.is_success is False
    assert "inside the managed workspace" in git_metadata.message

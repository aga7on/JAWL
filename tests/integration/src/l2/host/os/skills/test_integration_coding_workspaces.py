import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from src.l2_interfaces.host.os.skills.coding_workspaces import (
    HostOSCodingWorkspaces,
)


def run_git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def create_repository(root: Path, name: str = "project") -> Path:
    repository = root / name
    repository.mkdir()
    run_git(repository, "init", "-b", "main")
    run_git(repository, "config", "user.name", "Test User")
    run_git(repository, "config", "user.email", "test@example.com")
    (repository / "app.py").write_text("value = 1\n", encoding="utf-8")
    run_git(repository, "add", "--all")
    run_git(repository, "commit", "-m", "initial")
    return repository


@pytest.mark.asyncio
async def test_coding_workspace_isolates_commits_and_persists_status(os_client):
    repository = create_repository(os_client.sandbox_dir)
    manager = HostOSCodingWorkspaces(os_client)

    created = await manager.create_coding_workspace(
        repository_path="sandbox/project", task_id="feature-123"
    )

    assert created.is_success is True, created.message
    payload = json.loads(created.message)
    workspace = Path(payload["workspace_path"])
    assert workspace.is_relative_to(os_client.sandbox_dir / ".jawl-worktrees")
    assert workspace.is_dir()
    assert run_git(workspace, "branch", "--show-current") == payload["branch"]

    (workspace / "app.py").write_text("value = 2\n", encoding="utf-8")
    assert (repository / "app.py").read_text(encoding="utf-8") == "value = 1\n"

    # A new manager instance simulates a later ReAct cycle/framework restart.
    resumed_manager = HostOSCodingWorkspaces(os_client)
    status = await resumed_manager.get_coding_workspace_status("feature-123")
    status_payload = json.loads(status.message)
    assert status_payload["state"] == "dirty"
    assert "app.py" in status_payload["diff_stat"]

    committed = await resumed_manager.commit_coding_workspace(
        "feature-123", "implement feature"
    )
    commit_payload = json.loads(committed.message)
    assert committed.is_success is True
    assert run_git(workspace, "rev-parse", "HEAD") == commit_payload["commit"]

    clean_status = await resumed_manager.get_coding_workspace_status("feature-123")
    assert json.loads(clean_status.message)["state"] == "clean"

    removed = await resumed_manager.remove_coding_workspace("feature-123")
    removed_payload = json.loads(removed.message)
    assert removed.is_success is True
    assert not workspace.exists()
    assert removed_payload["preserved_branch"] == payload["branch"]
    assert run_git(
        repository, "show-ref", "--verify", f"refs/heads/{payload['branch']}"
    )


@pytest.mark.asyncio
async def test_coding_workspace_rejects_dirty_base_by_default(os_client):
    repository = create_repository(os_client.sandbox_dir)
    (repository / "uncommitted.py").write_text("pending = True\n", encoding="utf-8")
    manager = HostOSCodingWorkspaces(os_client)

    rejected = await manager.create_coding_workspace(
        repository_path="sandbox/project", task_id="dirty-base"
    )

    assert rejected.is_success is False
    assert "uncommitted changes" in rejected.message
    assert json.loads(manager.registry_file.read_text(encoding="utf-8"))[
        "workspaces"
    ] == {}


@pytest.mark.asyncio
async def test_coding_workspace_force_cleanup_is_explicit(os_client):
    create_repository(os_client.sandbox_dir)
    manager = HostOSCodingWorkspaces(os_client)
    created = await manager.create_coding_workspace(
        repository_path="sandbox/project", task_id="cleanup-guard"
    )
    workspace = Path(json.loads(created.message)["workspace_path"])
    (workspace / "dirty.txt").write_text("do not lose silently\n", encoding="utf-8")

    refused = await manager.remove_coding_workspace("cleanup-guard")

    assert refused.is_success is False
    assert "uncommitted changes" in refused.message
    assert workspace.exists()

    forced = await manager.remove_coding_workspace("cleanup-guard", force=True)
    forced_payload = json.loads(forced.message)

    assert forced.is_success is True
    assert not workspace.exists()
    assert forced_payload["recovery_stash"]
    assert "dirty.txt" in run_git(
        os_client.sandbox_dir / "project",
        "stash",
        "show",
        "--name-only",
        "--include-untracked",
        forced_payload["recovery_stash"],
    )


@pytest.mark.asyncio
async def test_coding_workspace_validates_task_identifier(os_client):
    create_repository(os_client.sandbox_dir)
    manager = HostOSCodingWorkspaces(os_client)

    result = await manager.create_coding_workspace(
        repository_path="sandbox/project", task_id="../escape"
    )

    assert result.is_success is False
    assert "task_id must be" in result.message


@pytest.mark.asyncio
async def test_parallel_tasks_receive_independent_worktrees(os_client):
    repository = create_repository(os_client.sandbox_dir)
    manager = HostOSCodingWorkspaces(os_client)

    first, second = await asyncio.gather(
        manager.create_coding_workspace("sandbox/project", "parallel-a"),
        manager.create_coding_workspace("sandbox/project", "parallel-b"),
    )

    assert first.is_success is True, first.message
    assert second.is_success is True, second.message
    first_payload = json.loads(first.message)
    second_payload = json.loads(second.message)
    first_workspace = Path(first_payload["workspace_path"])
    second_workspace = Path(second_payload["workspace_path"])
    assert first_workspace != second_workspace
    assert first_payload["branch"] != second_payload["branch"]

    (first_workspace / "app.py").write_text("value = 'a'\n", encoding="utf-8")
    (second_workspace / "app.py").write_text("value = 'b'\n", encoding="utf-8")

    assert (first_workspace / "app.py").read_text(encoding="utf-8") == "value = 'a'\n"
    assert (second_workspace / "app.py").read_text(encoding="utf-8") == "value = 'b'\n"
    assert (repository / "app.py").read_text(encoding="utf-8") == "value = 1\n"

    assert (await manager.remove_coding_workspace("parallel-a", force=True)).is_success
    assert (await manager.remove_coding_workspace("parallel-b", force=True)).is_success

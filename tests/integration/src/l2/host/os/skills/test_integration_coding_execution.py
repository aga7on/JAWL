import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest
import psutil

from src.l2_interfaces.host.os.skills.coding_execution import HostOSCodingExecution
from src.l2_interfaces.host.os.skills.coding_workspaces import HostOSCodingWorkspaces


def run_git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


async def create_workspace(os_client, task_id: str):
    repository = os_client.sandbox_dir / "project"
    repository.mkdir()
    run_git(repository, "init", "-b", "main")
    run_git(repository, "config", "user.name", "Test User")
    run_git(repository, "config", "user.email", "test@example.com")
    (repository / "app.py").write_text("value = 1\n", encoding="utf-8")
    run_git(repository, "add", "--all")
    run_git(repository, "commit", "-m", "initial")
    workspaces = HostOSCodingWorkspaces(os_client)
    created = await workspaces.create_coding_workspace(
        "sandbox/project", task_id
    )
    assert created.is_success is True, created.message
    workspace = Path(json.loads(created.message)["workspace_path"])
    return workspaces, workspace


@pytest.mark.asyncio
async def test_host_coding_command_requires_policy_allowlist_and_exact_state(
    os_client, monkeypatch
):
    workspaces, workspace = await create_workspace(os_client, "host-command")
    execution = HostOSCodingExecution(os_client, workspaces)
    fingerprint = await workspaces.workspace_fingerprint(workspace)

    disabled = await execution.run_coding_command(
        "host-command", ["python.exe", "--version"], fingerprint["fingerprint"]
    )
    assert disabled.is_success is False
    assert "disabled by host.os policy" in disabled.message

    os_client.config.coding_execution_backend = "host"
    denied = await execution.run_coding_command(
        "host-command", ["python.exe", "--version"], fingerprint["fingerprint"]
    )
    assert denied.is_success is False
    assert "not pre-approved" in denied.message

    fake_executable = workspace / "python.exe"
    fake_executable.write_bytes(b"not a real executable")
    hijacked_state = await workspaces.workspace_fingerprint(workspace)
    os_client.config.coding_host_allowed_commands = ["python.exe"]
    monkeypatch.setattr(
        "src.l2_interfaces.host.os.skills.coding_execution.shutil.which",
        lambda command, path=None: str(fake_executable),
    )
    hijacked = await execution.run_coding_command(
        "host-command",
        ["python.exe", "--version"],
        hijacked_state["fingerprint"],
    )
    assert hijacked.is_success is False
    assert "cannot resolve inside the agent sandbox" in hijacked.message
    fake_executable.unlink()

    os_client.config.coding_host_allowed_commands = [sys.executable]
    monkeypatch.setenv("CODING_COMMAND_SECRET_TOKEN", "must-not-leak")
    executed = await execution.run_coding_command(
        "host-command",
        [
            "python.exe",
            "-c",
            "import os; from pathlib import Path; "
            "Path('generated.txt').write_text('ok'); "
            "print(os.getenv('CODING_COMMAND_SECRET_TOKEN', 'scrubbed'))",
        ],
        fingerprint["fingerprint"],
    )
    assert executed.is_success is True, executed.message
    payload = json.loads(executed.message)
    assert payload["backend"] == "host"
    assert payload["stdout"] == "scrubbed"
    assert payload["workspace_changed"] is True
    assert (workspace / "generated.txt").read_text(encoding="utf-8") == "ok"

    stale = await execution.run_coding_command(
        "host-command", ["python.exe", "--version"], fingerprint["fingerprint"]
    )
    assert stale.is_success is False
    assert "changed since inspection" in stale.message


@pytest.mark.asyncio
async def test_container_command_is_shell_free_task_mounted_and_resource_bounded(
    os_client, monkeypatch
):
    workspaces, workspace = await create_workspace(os_client, "container-command")
    execution = HostOSCodingExecution(os_client, workspaces)
    os_client.config.coding_execution_backend = "container"
    os_client.config.coding_container_runtime = "docker"
    os_client.config.coding_container_image = "python:3.11-slim"
    monkeypatch.setattr(
        "src.l2_interfaces.host.os.skills.coding_execution.shutil.which",
        lambda command: "C:\\tools\\docker.exe" if command == "docker" else None,
    )

    command = execution._build_container_command(
        workspace, workspace, ["python", "-m", "pytest", "-q"]
    )
    assert command[0] == "C:\\tools\\docker.exe"
    assert command[1:3] == ["run", "--rm"]
    assert command[command.index("--network") + 1] == "none"
    assert command[command.index("--cap-drop") + 1] == "ALL"
    assert command[command.index("--security-opt") + 1] == (
        "no-new-privileges:true"
    )
    mount = command[command.index("--mount") + 1]
    assert f"source={workspace.resolve()}" in mount
    assert "target=/workspace" in mount
    image_index = command.index("python:3.11-slim")
    assert command[image_index + 1 :] == ["python", "-m", "pytest", "-q"]
    assert not any(item in {"cmd.exe", "powershell", "sh", "bash"} for item in command)


@pytest.mark.asyncio
async def test_coding_workspace_status_exposes_current_execution_fingerprint(os_client):
    workspaces, workspace = await create_workspace(os_client, "status-fingerprint")
    status = await workspaces.get_coding_workspace_status("status-fingerprint")
    assert status.is_success is True, status.message
    payload = json.loads(status.message)
    current = await workspaces.workspace_fingerprint(workspace)
    assert payload["head"] == current["head"]
    assert payload["workspace_fingerprint"] == current["fingerprint"]


@pytest.mark.asyncio
async def test_host_coding_command_bounds_output_and_kills_timed_out_process_tree(
    os_client,
):
    workspaces, workspace = await create_workspace(os_client, "bounded-command")
    execution = HostOSCodingExecution(os_client, workspaces)
    os_client.config.coding_execution_backend = "host"
    os_client.config.coding_host_allowed_commands = [sys.executable]
    fingerprint = await workspaces.workspace_fingerprint(workspace)
    large = await execution.run_coding_command(
        "bounded-command",
        ["python.exe", "-c", "print('x' * 100000)"],
        fingerprint["fingerprint"],
    )
    assert large.is_success is True, large.message
    large_payload = json.loads(large.message)
    assert large_payload["stdout_truncated"] is True
    assert len(large_payload["stdout"]) <= 16000

    timeout = await execution.run_coding_command(
        "bounded-command",
        [
            "python.exe",
            "-c",
            "import pathlib,subprocess,sys,time; "
            "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']); "
            "pathlib.Path('child.pid').write_text(str(p.pid)); time.sleep(60)",
        ],
        fingerprint["fingerprint"],
        timeout_seconds=1,
    )
    assert timeout.is_success is False
    assert "exceeded its 1-second timeout" in timeout.message
    child_pid = int((workspace / "child.pid").read_text(encoding="utf-8"))
    for _ in range(20):
        if not psutil.pid_exists(child_pid):
            break
        await asyncio.sleep(0.05)
    assert not psutil.pid_exists(child_pid)

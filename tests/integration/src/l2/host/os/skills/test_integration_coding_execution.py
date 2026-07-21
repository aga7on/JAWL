import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest
import psutil

from src.l2_interfaces.host.os.skills.coding_execution import HostOSCodingExecution
from src.l2_interfaces.host.os.skills.coding_workspaces import HostOSCodingWorkspaces
from src.l2_interfaces.host.os.coding_approvals import CodingApprovalStore
from src.utils.settings import (
    CodingCommandProfileConfig,
    CodingContainerProfileConfig,
)
from src.utils.event.bus import EventBus


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


@pytest.mark.asyncio
async def test_required_approval_is_exact_one_shot_and_policy_preserving(os_client):
    workspaces, workspace = await create_workspace(os_client, "approved-command")
    approvals = CodingApprovalStore(os_client.system_dir / "test-approvals.json")
    execution = HostOSCodingExecution(os_client, workspaces, approvals)
    os_client.config.coding_execution_backend = "host"
    os_client.config.coding_host_allowed_commands = [sys.executable]
    os_client.config.coding_approval_mode = "required"
    fingerprint = await workspaces.workspace_fingerprint(workspace)
    argv = ["python.exe", "-c", "print('approved')"]

    missing = await execution.run_coding_command(
        "approved-command", argv, fingerprint["fingerprint"]
    )
    assert missing.is_success is False
    assert "requires a one-shot approval" in missing.message

    requested = await execution.request_coding_command_approval(
        "approved-command", argv, fingerprint["fingerprint"]
    )
    assert requested.is_success is True, requested.message
    approval_id = json.loads(requested.message)["approval"]["id"]
    approvals.decide(approval_id, approved=True, actor="test")

    mismatch = await execution.run_coding_command(
        "approved-command",
        ["python.exe", "-c", "print('different')"],
        fingerprint["fingerprint"],
        approval_id=approval_id,
    )
    assert mismatch.is_success is False
    assert "does not match the exact" in mismatch.message

    executed = await execution.run_coding_command(
        "approved-command",
        argv,
        fingerprint["fingerprint"],
        approval_id=approval_id,
    )
    assert executed.is_success is True, executed.message
    assert json.loads(executed.message)["stdout"] == "approved"

    replay = await execution.run_coding_command(
        "approved-command",
        argv,
        fingerprint["fingerprint"],
        approval_id=approval_id,
    )
    assert replay.is_success is False
    assert "consumed" in replay.message


@pytest.mark.asyncio
async def test_approval_request_publishes_only_redacted_public_event(os_client):
    workspaces, workspace = await create_workspace(os_client, "approval-push")
    approvals = CodingApprovalStore(os_client.system_dir / "push-approvals.json")
    bus = EventBus()
    observed = []

    async def capture(**payload):
        observed.append(payload)

    from src.utils.event.registry import Events

    bus.subscribe(Events.CODING_APPROVAL_REQUESTED, capture)
    execution = HostOSCodingExecution(
        os_client, workspaces, approvals, event_bus=bus
    )
    os_client.config.coding_execution_backend = "host"
    os_client.config.coding_host_allowed_commands = [sys.executable]
    os_client.config.coding_approval_mode = "required"
    fingerprint = await workspaces.workspace_fingerprint(workspace)
    requested = await execution.request_coding_command_approval(
        "approval-push",
        ["python.exe", "--token", "do-not-push", "--version"],
        fingerprint["fingerprint"],
    )
    assert requested.is_success is True, requested.message
    await bus.stop()
    assert len(observed) == 1
    public = observed[0]["approval"]
    assert public["status"] == "pending"
    assert "[REDACTED]" in public["argv_preview"]
    assert "do-not-push" not in json.dumps(observed, ensure_ascii=False)
    assert "execution_identity" not in public


@pytest.mark.asyncio
async def test_named_toolchain_profile_uses_user_declared_exact_argv(os_client):
    workspaces, workspace = await create_workspace(os_client, "profile-command")
    approvals = CodingApprovalStore(os_client.system_dir / "profile-approvals.json")
    execution = HostOSCodingExecution(os_client, workspaces, approvals)
    os_client.config.coding_execution_backend = "host"
    os_client.config.coding_host_allowed_commands = [sys.executable]
    os_client.config.coding_approval_mode = "required"
    os_client.config.coding_command_profiles = [
        CodingCommandProfileConfig(
            name="python-smoke",
            argv=[
                "python.exe",
                "-c",
                "from pathlib import Path; Path('profile.txt').write_text('ok')",
            ],
            timeout_seconds=10,
        )
    ]
    fingerprint = await workspaces.workspace_fingerprint(workspace)
    profiles = await execution.list_coding_command_profiles()
    assert profiles.is_success is True
    assert json.loads(profiles.message)["profiles"] == [
        {
            "name": "python-smoke",
            "executable": "python.exe",
            "argument_count": 2,
            "relative_cwd": ".",
            "timeout_seconds": 10,
            "container_profile": None,
        }
    ]
    requested = await execution.request_coding_profile_approval(
        "profile-command", "python-smoke", fingerprint["fingerprint"]
    )
    assert requested.is_success is True, requested.message
    approval_id = json.loads(requested.message)["approval"]["id"]
    approvals.decide(approval_id, approved=True, actor="test")

    executed = await execution.run_coding_profile(
        "profile-command",
        "python-smoke",
        fingerprint["fingerprint"],
        approval_id=approval_id,
    )

    assert executed.is_success is True, executed.message
    assert (workspace / "profile.txt").read_text(encoding="utf-8") == "ok"
    unknown = await execution.run_coding_profile(
        "profile-command", "unknown", fingerprint["fingerprint"]
    )
    assert unknown.is_success is False
    assert "resolve exactly once" in unknown.message


@pytest.mark.asyncio
async def test_named_container_profile_is_exact_listed_and_approval_bound(
    os_client, monkeypatch
):
    workspaces, workspace = await create_workspace(os_client, "container-profile")
    approvals = CodingApprovalStore(
        os_client.system_dir / "container-profile-approvals.json"
    )
    execution = HostOSCodingExecution(os_client, workspaces, approvals)
    os_client.config.coding_execution_backend = "container"
    os_client.config.coding_container_runtime = "docker"
    os_client.config.coding_approval_mode = "required"
    os_client.config.coding_container_profiles = [
        CodingContainerProfileConfig(
            name="python-ci",
            image="python:3.12-slim",
            network="none",
            memory_mb=1024,
            cpus=1.5,
            pids=96,
        )
    ]
    os_client.config.coding_command_profiles = [
        CodingCommandProfileConfig(
            name="tests-in-ci",
            argv=["python", "-m", "pytest", "-q"],
            timeout_seconds=30,
            container_profile="python-ci",
        )
    ]
    monkeypatch.setattr(
        "src.l2_interfaces.host.os.skills.coding_execution.shutil.which",
        lambda command: sys.executable if command == "docker" else None,
    )
    captured = {}

    async def fake_run(command, cwd, timeout, env):
        captured["command"] = command
        captured["cwd"] = cwd
        captured["timeout"] = timeout
        captured["env"] = env
        return 0, "profile-ok", "", False, False

    monkeypatch.setattr(execution, "_run_bounded", fake_run)
    listed = await execution.list_coding_container_profiles()
    assert listed.is_success is True, listed.message
    listed_payload = json.loads(listed.message)
    assert listed_payload["runtime"] == "docker"
    assert listed_payload["profiles"] == [
        {
            "profile": "python-ci",
            "image": "python:3.12-slim",
            "network": "none",
            "memory_mb": 1024,
            "cpus": 1.5,
            "pids": 96,
        }
    ]
    fingerprint = await workspaces.workspace_fingerprint(workspace)
    requested = await execution.request_coding_profile_approval(
        "container-profile", "tests-in-ci", fingerprint["fingerprint"]
    )
    assert requested.is_success is True, requested.message
    request_payload = json.loads(requested.message)
    assert request_payload["container_profile"] == "python-ci"
    approval_id = request_payload["approval"]["id"]
    approvals.decide(approval_id, approved=True, actor="test")

    os_client.config.coding_container_profiles[0].image = "python:3.13-slim"
    changed_policy = await execution.run_coding_profile(
        "container-profile",
        "tests-in-ci",
        fingerprint["fingerprint"],
        approval_id=approval_id,
    )
    assert changed_policy.is_success is False
    assert "does not match the exact" in changed_policy.message

    os_client.config.coding_container_profiles[0].image = "python:3.12-slim"
    executed = await execution.run_coding_profile(
        "container-profile",
        "tests-in-ci",
        fingerprint["fingerprint"],
        approval_id=approval_id,
    )
    assert executed.is_success is True, executed.message
    payload = json.loads(executed.message)
    assert payload["container_profile"] == "python-ci"
    assert payload["stdout"] == "profile-ok"
    command = captured["command"]
    assert command[command.index("--network") + 1] == "none"
    assert command[command.index("--memory") + 1] == "1024m"
    assert command[command.index("--cpus") + 1] == "1.5"
    assert command[command.index("--pids-limit") + 1] == "96"
    assert command[command.index("python:3.12-slim") + 1 :] == [
        "python",
        "-m",
        "pytest",
        "-q",
    ]

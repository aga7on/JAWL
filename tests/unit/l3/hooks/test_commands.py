import asyncio
import json
import sys
from pathlib import Path

import pytest

from src.l3_agent.hooks.commands import DeclarativeCommandHooks
from src.l3_agent.hooks.lifecycle import HookContext, HookPhase, LifecycleHooks
from src.utils.settings import LifecycleCommandHookConfig, LifecycleHooksConfig


def context(**parameters) -> HookContext:
    return HookContext(
        phase=HookPhase.PRE_TOOL_USE,
        plan_id="plan-1",
        action_id="action-1",
        tool_name="HostOSCodingFiles.read",
        parameters=parameters,
        trace={"trace_id": "trace-1"},
    )


def config(*commands, command_timeout_seconds: float = 2) -> LifecycleHooksConfig:
    return LifecycleHooksConfig(
        enabled=True,
        command_timeout_seconds=command_timeout_seconds,
        handler_timeout_seconds=max(1, command_timeout_seconds + 0.5),
        commands=list(commands),
    )


@pytest.mark.asyncio
async def test_user_command_receives_only_bounded_metadata_and_scrubbed_environment(
    tmp_path, monkeypatch
):
    script = tmp_path / "capture.py"
    result = tmp_path / "payload.json"
    script.write_text(
        "import json, os, pathlib, sys\n"
        "payload = json.load(sys.stdin)\n"
        "payload['secret_env'] = os.environ.get('LLM_API_KEY')\n"
        "pathlib.Path(sys.argv[1]).write_text(json.dumps(payload), encoding='utf-8')\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LLM_API_KEY", "environment-secret")
    profile = LifecycleCommandHookConfig(
        name="metadata",
        phase="pre_tool_use",
        argv=[sys.executable, str(script), str(result)],
        tool_patterns=["HostOSCodingFiles.*"],
    )
    lifecycle = LifecycleHooks(timeout_seconds=3, fail_closed=True)
    adapter = DeclarativeCommandHooks(config(profile), framework_root=tmp_path)
    adapter.register(lifecycle)

    run = await lifecycle.run(context(task_id="task-1", token="parameter-secret"))

    assert run.decision.allowed is True
    payload = json.loads(result.read_text(encoding="utf-8"))
    assert payload["parameter_names"] == ["task_id", "token"]
    assert "parameter-secret" not in result.read_text(encoding="utf-8")
    assert payload["secret_env"] is None
    assert payload["trace"] == {"trace_id": "trace-1"}


@pytest.mark.asyncio
async def test_repository_can_only_select_preapproved_workspace_profile(tmp_path):
    framework = tmp_path / "framework"
    workspace = tmp_path / "workspace"
    framework.mkdir()
    workspace.mkdir()
    profile = LifecycleCommandHookConfig(
        name="repo-check",
        phase="pre_tool_use",
        argv=[
            sys.executable,
            "-c",
            "from pathlib import Path; Path('hook-ran').write_text('yes')",
        ],
        scope="repository",
        working_directory="workspace",
        tool_patterns=["HostOSCoding*"],
    )
    lifecycle = LifecycleHooks(timeout_seconds=3, fail_closed=True)
    adapter = DeclarativeCommandHooks(
        config(profile),
        framework_root=framework,
        workspace_resolver=lambda _task_id, _relative, _write: workspace,
    )
    adapter.register(lifecycle)

    skipped = await lifecycle.run(context(task_id="task-1"))
    assert skipped.decision.allowed is True
    assert not (workspace / "hook-ran").exists()

    manifest = workspace / ".jawl" / "hooks.json"
    manifest.parent.mkdir()
    manifest.write_text(
        json.dumps({"version": 1, "hooks": ["repo-check"]}), encoding="utf-8"
    )
    executed = await lifecycle.run(context(task_id="task-1"))

    assert executed.decision.allowed is True
    assert (workspace / "hook-ran").read_text(encoding="utf-8") == "yes"


@pytest.mark.asyncio
async def test_pre_command_can_deny_without_becoming_a_hook_failure(tmp_path):
    profile = LifecycleCommandHookConfig(
        name="deny-policy",
        phase="pre_tool_use",
        argv=[sys.executable, "-c", "raise SystemExit(10)"],
    )
    lifecycle = LifecycleHooks(timeout_seconds=3, fail_closed=True)
    DeclarativeCommandHooks(config(profile), framework_root=tmp_path).register(
        lifecycle
    )

    run = await lifecycle.run(context())

    assert run.decision.allowed is False
    assert "deny-policy" in run.decision.reason
    assert run.failures == ()


@pytest.mark.asyncio
async def test_command_timeout_is_bounded_and_fails_closed(tmp_path):
    child_marker = tmp_path / "child-survived"
    child_code = (
        "import pathlib, time; time.sleep(0.3); "
        f"pathlib.Path({str(child_marker)!r}).write_text('survived')"
    )
    parent_code = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        "time.sleep(30)"
    )
    profile = LifecycleCommandHookConfig(
        name="bounded-timeout",
        phase="pre_tool_use",
        argv=[sys.executable, "-c", parent_code],
    )
    lifecycle = LifecycleHooks(timeout_seconds=1, fail_closed=True)
    DeclarativeCommandHooks(
        config(profile, command_timeout_seconds=0.05), framework_root=tmp_path
    ).register(lifecycle)

    run = await lifecycle.run(context())
    await asyncio.sleep(0.4)

    assert run.decision.allowed is False
    assert run.failures and "timed out" in run.failures[0]
    assert not child_marker.exists()


@pytest.mark.asyncio
async def test_failed_command_output_is_bounded_and_redacted(tmp_path):
    profile = LifecycleCommandHookConfig(
        name="redacted-failure",
        phase="pre_tool_use",
        argv=[
            sys.executable,
            "-c",
            "print('api_key=very-secret-value'); print('x' * 10000); "
            "raise SystemExit(2)",
        ],
    )
    hook_config = config(profile)
    hook_config.max_output_chars = 500
    lifecycle = LifecycleHooks(timeout_seconds=3, fail_closed=True)
    DeclarativeCommandHooks(hook_config, framework_root=tmp_path).register(lifecycle)

    run = await lifecycle.run(context())

    assert run.decision.allowed is False
    assert "very-secret-value" not in run.failures[0]
    assert "[REDACTED]" in run.failures[0]
    assert len(run.failures[0]) < 800


def test_repository_profile_cannot_execute_from_framework_directory(tmp_path):
    profile = LifecycleCommandHookConfig(
        name="unsafe-cwd",
        phase="pre_tool_use",
        argv=[sys.executable, "-c", "pass"],
        scope="repository",
        working_directory="framework",
    )

    with pytest.raises(ValueError, match="managed task workspace"):
        DeclarativeCommandHooks(config(profile), framework_root=tmp_path)

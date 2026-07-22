import asyncio

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from src.l3_agent.swarm.spawn import SwarmManager
from src.utils.settings import SwarmConfig
from src.l3_agent.swarm.roles import Subagents
from src.l3_agent.hooks.lifecycle import HookDecision, HookPhase, LifecycleHooks
from src.l3_agent.swarm.registry import DelegationRegistry


@pytest.fixture
def mock_registry():
    # Мокаем глобальный реестр, используя РЕАЛЬНЫЕ объекты ролей
    return {
        "HostOSFiles.read_file": {"swarm": [Subagents.CODER]},
        "HostOSExecution.execute_script": {"swarm": [Subagents.CODER]},
        "DeepResearch.deep_research": {"swarm": [Subagents.WEB_RESEARCHER]},
    }


@pytest.fixture
def swarm_manager(mock_registry, tmp_path):
    config = SwarmConfig(enabled=True, subagent_model="cheap-model", max_concurrent_workers=2)
    mock_executor = AsyncMock()

    with patch("src.l3_agent.swarm.spawn._REGISTRY", mock_registry):
        with patch("src.l3_agent.swarm.spawn.SwarmPromptBuilder"):
            return SwarmManager(mock_executor, config, tmp_path)


@pytest.mark.asyncio
async def test_spawn_disabled(swarm_manager):
    swarm_manager.config.enabled = False
    res = await swarm_manager.spawn_subagent("coder", "Task")
    assert res.is_success is False
    assert "disabled" in res.message


@pytest.mark.asyncio
async def test_spawn_unknown_model(swarm_manager):
    swarm_manager.config.subagent_model = "unknown"
    res = await swarm_manager.spawn_subagent("coder", "Task")
    assert res.is_success is False
    assert "No subagent model specified" in res.message


@pytest.mark.asyncio
async def test_spawn_unknown_role(swarm_manager):
    res = await swarm_manager.spawn_subagent("hacker", "Task")
    assert res.is_success is False
    assert "unavailable" in res.message


@pytest.mark.asyncio
@patch("src.l3_agent.swarm.spawn.SubagentLoop")
async def test_spawn_success_background_task(mock_loop_class, swarm_manager):
    mock_loop_instance = MagicMock()
    mock_loop_instance.run = AsyncMock()
    mock_loop_class.return_value = mock_loop_instance

    res = await swarm_manager.spawn_subagent("coder", "Fix bugs")

    assert res.is_success is True
    assert "successfully spawned" in res.message

    assert len(swarm_manager.active_tasks) == 1

    for task in list(swarm_manager.active_tasks):
        await task

    mock_loop_instance.run.assert_awaited_once()
    records = swarm_manager.registry.list(status="completed")
    assert len(records) == 1
    assert records[0]["task_summary"] == "Fix bugs"


@pytest.mark.asyncio
async def test_spawn_can_be_denied_before_background_task(swarm_manager):
    async def deny(_context):
        return HookDecision.deny("operator policy")

    swarm_manager.hooks.subscribe(HookPhase.PRE_DELEGATION, deny)

    result = await swarm_manager.spawn_subagent("coder", "Fix bugs")

    assert result.is_success is False
    assert "operator policy" in result.message
    assert not swarm_manager.active_tasks


@pytest.mark.asyncio
@patch("src.l3_agent.swarm.spawn.SubagentLoop")
async def test_spawn_binds_and_records_parent_coding_step(
    mock_loop_class, swarm_manager
):
    timeline = []

    async def record_result(**_kwargs):
        timeline.append("plan")
        return {}

    async def publish(*_args, **_kwargs):
        timeline.append("event")

    coding_plans = MagicMock()
    coding_plans.bind_delegation = AsyncMock(
        return_value={"bound_revision": 8, "base_workspace_fingerprint": "abc"}
    )
    coding_plans.record_delegation_result = AsyncMock(side_effect=record_result)
    swarm_manager.coding_plans = coding_plans
    event_bus = MagicMock()
    event_bus.publish = AsyncMock(side_effect=publish)
    swarm_manager.event_bus = event_bus
    loop = MagicMock(run=AsyncMock(return_value="completed"))
    mock_loop_class.return_value = loop

    result = await swarm_manager.spawn_subagent(
        "coder",
        "Implement parser",
        parent_task_id="task-one",
        parent_step_id="implement",
        expected_plan_revision=7,
    )
    assert result.is_success is True, result.message
    await asyncio.gather(*list(swarm_manager.active_tasks))

    coding_plans.bind_delegation.assert_awaited_once()
    coding_plans.record_delegation_result.assert_awaited_once()
    event_bus.publish.assert_awaited_once()
    terminal_payload = event_bus.publish.call_args.kwargs
    assert terminal_payload["parent_task_id"] == "task-one"
    assert terminal_payload["parent_step_id"] == "implement"
    assert terminal_payload["parent_reconciled"] is True
    assert timeline == ["plan", "event"]
    assert "[BOUND CODING PLAN]" in mock_loop_class.call_args.kwargs[
        "task_description"
    ]
    persisted = swarm_manager.registry.list()[0]
    assert persisted["parent"] == {
        "task_id": "task-one",
        "step_id": "implement",
        "expected_revision": 7,
    }


@pytest.mark.asyncio
async def test_spawn_rejects_partial_parent_binding(swarm_manager):
    result = await swarm_manager.spawn_subagent(
        "coder", "Task", parent_task_id="task-one"
    )

    assert result.is_success is False
    assert "requires parent_task_id" in result.message


@pytest.mark.asyncio
async def test_start_reconciles_prior_session_interruption(swarm_manager):
    path = swarm_manager.registry.path
    swarm_manager.registry.create(
        "oldagent",
        "coder",
        "old task",
        {
            "task_id": "task-one",
            "step_id": "implement",
            "expected_revision": 4,
        },
    )
    recovered = DelegationRegistry(path, session_id="new-session")
    coding_plans = MagicMock()
    coding_plans.record_delegation_result = AsyncMock(return_value={})
    swarm_manager.registry = recovered
    swarm_manager.coding_plans = coding_plans

    await swarm_manager.start()

    coding_plans.record_delegation_result.assert_awaited_once_with(
        task_id="task-one",
        step_id="implement",
        delegation_id="oldagent",
        status="interrupted",
        detail="Interrupted delegated worker recovered after process restart.",
    )
    assert recovered.get("oldagent")["parent_reconciled"] is True
    assert recovered.unreconciled_interruptions() == []


@pytest.mark.asyncio
@patch("src.l3_agent.swarm.spawn.SubagentLoop")
async def test_delegation_emits_success_and_error_terminal_phases(
    mock_loop_class, swarm_manager
):
    phases = []

    async def observe(context):
        phases.append(context.phase)

    for phase in (
        HookPhase.PRE_DELEGATION,
        HookPhase.POST_DELEGATION,
        HookPhase.DELEGATION_ERROR,
    ):
        swarm_manager.hooks.subscribe(phase, observe)

    loop = MagicMock()
    loop.run = AsyncMock()
    mock_loop_class.return_value = loop
    await swarm_manager.spawn_subagent("coder", "first")
    await asyncio.gather(*list(swarm_manager.active_tasks))

    loop.run = AsyncMock(side_effect=RuntimeError("worker broke"))
    await swarm_manager.spawn_subagent("coder", "second")
    await asyncio.gather(*list(swarm_manager.active_tasks))

    assert phases == [
        HookPhase.PRE_DELEGATION,
        HookPhase.POST_DELEGATION,
        HookPhase.PRE_DELEGATION,
        HookPhase.DELEGATION_ERROR,
    ]
    assert len(swarm_manager.registry.list(status="completed")) == 1
    assert len(swarm_manager.registry.list(status="failed")) == 1


@pytest.mark.asyncio
@patch("src.l3_agent.swarm.spawn.SubagentLoop")
async def test_delegation_cancellation_emits_terminal_phase(
    mock_loop_class, swarm_manager
):
    started = asyncio.Event()
    phases = []

    async def wait_forever():
        started.set()
        await asyncio.Event().wait()

    async def observe(context):
        phases.append(context.phase)

    swarm_manager.hooks.subscribe(HookPhase.DELEGATION_CANCELLED, observe)
    loop = MagicMock()
    loop.run = wait_forever
    mock_loop_class.return_value = loop

    await swarm_manager.spawn_subagent("coder", "cancel me")
    task = next(iter(swarm_manager.active_tasks))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert HookPhase.DELEGATION_CANCELLED in phases
    assert swarm_manager.registry.list(status="cancelled")[0]["task_summary"] == (
        "cancel me"
    )


@pytest.mark.asyncio
@patch("src.l3_agent.swarm.spawn.SubagentLoop")
async def test_cancel_skill_and_shutdown_await_workers(mock_loop_class, swarm_manager):
    started = asyncio.Event()

    async def wait_forever():
        started.set()
        await asyncio.Event().wait()

    loop = MagicMock(run=wait_forever)
    mock_loop_class.return_value = loop
    result = await swarm_manager.spawn_subagent("coder", "long task")
    delegation_id = result.message.split("coder_", 1)[1].split(" ", 1)[0]
    await started.wait()

    cancelled = await swarm_manager.cancel_delegation(delegation_id)
    assert cancelled.is_success is True
    await asyncio.gather(*list(swarm_manager.active_tasks), return_exceptions=True)
    assert swarm_manager.registry.get(delegation_id)["status"] == "cancelled"

    await swarm_manager.spawn_subagent("coder", "shutdown task")
    await started.wait()
    await swarm_manager.stop()
    assert not swarm_manager.active_tasks


@pytest.mark.asyncio
@patch("src.l3_agent.swarm.spawn.SubagentLoop")
async def test_active_worker_accepts_bounded_control_message(
    mock_loop_class, swarm_manager
):
    started = asyncio.Event()

    async def wait_forever():
        started.set()
        await asyncio.Event().wait()

    mock_loop_class.return_value = MagicMock(run=wait_forever)
    spawned = await swarm_manager.spawn_subagent("coder", "long task")
    delegation_id = spawned.message.split("coder_", 1)[1].split(" ", 1)[0]
    await started.wait()

    sent = await swarm_manager.send_delegation_message(
        delegation_id, "Inspect the exact state before retrying."
    )
    provider = mock_loop_class.call_args.kwargs["control_message_provider"]

    assert sent.is_success is True
    assert provider() == ["Inspect the exact state before retrying."]
    assert provider() == []
    await swarm_manager.stop()


@pytest.mark.asyncio
async def test_wait_and_read_exact_delegation_report(swarm_manager):
    delegation_id = "report123"
    relative = "sandbox/_system/subagents/coder_report123.md"
    report_path = swarm_manager.root_dir / relative
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("verified worker report", encoding="utf-8")
    swarm_manager.registry.create(delegation_id, "coder", "task")
    swarm_manager.registry.transition(delegation_id, "running")
    swarm_manager.registry.transition(
        delegation_id, "completed", report_path=relative
    )

    waited = await swarm_manager.wait_for_delegation(delegation_id, 0)
    report = await swarm_manager.get_delegation_report(delegation_id)

    assert waited.is_success is True
    assert '"status": "completed"' in waited.message
    assert report.is_success is True
    assert "verified worker report" in report.message


def test_swarm_manager_dynamic_docstring(mock_registry, tmp_path):
    config = SwarmConfig(enabled=True, subagent_model="model")

    # Сценарий 1: Роли активны
    with patch("src.l3_agent.swarm.spawn._REGISTRY", mock_registry):
        manager1 = SwarmManager(AsyncMock(), config, tmp_path / "one")
        assert "coder" in manager1.spawn_subagent.__doc__
        assert "web_researcher" in manager1.spawn_subagent.__doc__

    # Сценарий 2: Host OS выключен (нету скиллов для coder)
    empty_registry = {"DeepResearch.deep_research": {"swarm": [Subagents.WEB_RESEARCHER]}}
    with patch("src.l3_agent.swarm.spawn._REGISTRY", empty_registry):
        manager2 = SwarmManager(AsyncMock(), config, tmp_path / "two")
        assert "coder" not in manager2.spawn_subagent.__doc__
        assert "web_researcher" in manager2.spawn_subagent.__doc__
